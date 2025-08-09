import asyncio
import os
import logging
from typing import Annotated, List, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP, Context
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, SecretStr, ValidationError
import httpx
from functools import lru_cache
import json

# --- Configure Logging ---
# It's good practice to set up logging early for better visibility.
# FastMCP integrates with Python's standard logging.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration Management with Pydantic Settings ---
# Use pydantic-settings for robust environment variable loading and validation.
# This makes configuration explicit, type-safe, and easier to manage.
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    logger.error("pydantic-settings not found. Please install it: pip install 'pydantic-settings<2.0'")
    # Fallback to direct os.getenv if pydantic-settings isn't installed for basic functionality
    # but strongly recommend installing it.
    class BaseSettings:
        pass

class AppSettings(BaseSettings):
    """
    Application settings loaded from environment variables.
    Pydantic-settings automatically looks for these in .env files and actual env vars.
    """
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

    # TOKEN: Use SecretStr for sensitive information.
    # It masks the value when printed or logged.
    AUTH_TOKEN: SecretStr = Field(
        ..., description="Bearer token for MCP server authentication."
    )
    MY_NUMBER: str = Field(
        ..., description="A unique identifier for the MCP server, e.g., a phone number."
    )
    # GEMINI_API_KEY: Also sensitive, use SecretStr.
    GEMINI_API_KEY: SecretStr = Field(
        ..., description="API key for accessing the Google Gemini API."
    )
    # Optional: Configure the server host and port
    SERVER_HOST: str = Field("0.0.0.0", description="Host address for the MCP server.")
    SERVER_PORT: int = Field(8086, description="Port for the MCP server.")
    GEMINI_API_TIMEOUT: int = Field(30, description="Timeout for Gemini API requests in seconds.")
    # Add a simple cache for Gemini responses (optional, but good for performance)
    ENABLE_GEMINI_CACHE: bool = Field(True, description="Enable caching for Gemini API responses.")
    GEMINI_CACHE_MAX_SIZE: int = Field(128, description="Maximum size for the Gemini response cache.")

try:
    settings = AppSettings()
    logger.info("Application settings loaded successfully.")
except ValidationError as e:
    logger.critical(f"Configuration error: {e.errors()}")
    # Exit if critical configuration is missing
    raise SystemExit("Exiting due to missing or invalid environment variables.")

# --- Auth Provider ---
class ImprovedBearerAuthProvider(BearerAuthProvider):
    """
    BearerAuthProvider that uses the configured AUTH_TOKEN.
    """
    def __init__(self, token: SecretStr):
        # RSAKeyPair.generate() is fine for this simple bearer token check;
        # if using actual JWTs signed by this server, you'd manage keys differently.
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token.get_secret_value() # Get the actual string value

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            logger.info(f"Authentication successful for client_id: puchmeds-client")
            return AccessToken(
                token=token,
                client_id="puchmeds-client",
                scopes=["*"],
                expires_at=None,
            )
        logger.warning("Authentication failed: Invalid token provided.")
        return None

# --- Rich Tool Description model ---
# No changes needed here, it's already well-defined.
class RichToolDescription(BaseModel):
    description: str = Field(..., description="A concise summary of the tool's purpose.")
    use_when: str = Field(..., description="Guidance on when this tool should be invoked.")
    side_effects: Optional[str] = Field(None, description="Description of any side effects or outputs.")
    structure: Optional[str] = Field(None, description="Expected format of the tool's output.")

# --- Gemini API Utility ---
class Gemini:
    """
    Utility class for interacting with the Google Gemini API.
    Includes basic error handling and an optional cache.
    """
    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

    @staticmethod
    @lru_cache(maxsize=settings.GEMINI_CACHE_MAX_SIZE if settings.ENABLE_GEMINI_CACHE else None)
    async def _cached_ask_gemini_content(prompt_key: str, api_key_value: str, system_format: Optional[str]) -> str:
        """
        Internal method for caching Gemini responses.
        Caches based on prompt, API key (value), and system format.
        """
        prompt = json.loads(prompt_key)['prompt'] # Reconstruct prompt from serialized key
        logger.info(f"Calling Gemini API for prompt: {prompt[:50]}...") # Log first 50 chars
        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": api_key_value,
        }
        full_prompt = f"{system_format}\n\n{prompt}" if system_format else prompt
        data = {
            "contents": [
                {
                    "parts": [
                        {"text": full_prompt}
                    ]
                }
            ]
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(Gemini.API_URL, headers=headers, json=data, timeout=settings.GEMINI_API_TIMEOUT)
                resp.raise_for_status() # Raise an exception for 4xx/5xx responses
            except httpx.HTTPStatusError as e:
                logger.error(f"Gemini API returned error status {e.response.status_code}: {e.response.text}")
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Gemini API failed: {e.response.status_code} {e.response.text}"))
            except httpx.RequestError as e:
                logger.error(f"Gemini API request failed: {e!r}")
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Gemini API network error: {e!r}"))
            except Exception as e:
                logger.error(f"An unexpected error occurred during Gemini API call: {e!r}")
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Unexpected Gemini API error: {e!r}"))

            result = resp.json()
            try:
                # Validate the structure of the Gemini response
                if not result.get("candidates") or not result["candidates"][0].get("content") or not result["candidates"][0]["content"].get("parts"):
                    logger.error(f"Gemini API response missing expected structure: {result}")
                    return "<error>Failed to parse Gemini API response: Unexpected structure.</error>"

                generated_text = result["candidates"][0]["content"]["parts"][0]["text"]
                logger.info("Gemini API call successful.")
                return generated_text
            except IndexError:
                logger.error(f"Gemini API response has no candidates or content: {result}")
                return "<error>Failed to parse Gemini API response: No generated content.</error>"
            except Exception as e:
                logger.error(f"Error parsing Gemini API response: {e!r}, Response: {result}")
                return "<error>Failed to parse Gemini API response: Malformed data.</error>"

    @staticmethod
    async def ask_gemini(prompt: str, api_key: SecretStr, system_format: Optional[str] = None) -> str:
        """
        Public method to ask Gemini. Handles caching and secret key extraction.
        """
        # lru_cache requires hashable arguments, so convert prompt to a string key
        prompt_key = json.dumps({'prompt': prompt, 'system_format': system_format})
        return await Gemini._cached_ask_gemini_content(prompt_key, api_key.get_secret_value(), system_format)


# --- MCP Server ---
# Pass the secret token to the auth provider
mcp = FastMCP(
    "PuchMeds MCP Server",
    auth=ImprovedBearerAuthProvider(settings.AUTH_TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    """
    Validation tool required by the Puch platform.
    Returns the server's unique identifier.
    """
    logger.info("Validate tool called.")
    return settings.MY_NUMBER

# --- Tool: med_interactions ---
MedInteractionsDesc = RichToolDescription(
    description="Check for harmful side effects and interactions when taking two or more medicines together. Returns a summary of symptoms, risks, and references.",
    use_when="Use when a user wants to know if taking multiple medicines together is safe, and what side effects or interactions may occur.",
    side_effects="Returns a formatted summary of possible symptoms, harmful side effects, and references for the given medicines.",
    structure=(
        "💊 **Medicine Interaction Report**\n"
        "Medicines: <list>\n"
        "Symptoms: <summary>\n"
        "Harmful Side Effects: <summary>\n"
        "References:\n"
        "- <link1>\n"
        "- <link2>\n"
        "\n"
        "Please consult a doctor or pharmacist to ensure the safety of combining these medicines."
    )
)

# Consistent system prompt for Gemini
GEMINI_MEDICAL_ASSISTANT_PROMPT = (
    "You are a medical assistant. "
    "Your primary goal is to provide **accurate, concise, and helpful** information regarding medicine interactions, "
    "while always emphasizing the need for professional medical consultation. "
    "Adhere strictly to the requested output format. "
    "**Crucially, prioritize patient safety and warn against self-medication or altering prescribed dosages.**\n\n"
    "Format your answer as follows:\n"
    "💊 **Medicine Interaction Report**\n"
    "Medicines: <list of medicines provided in the query, comma-separated>\n"
    "Symptoms: <summary of common symptoms of interactions, if any>\n"
    "Harmful Side Effects: <summary of severe or harmful side effects, if any>\n"
    "References:\n"
    "- <Relevant and reliable source URL 1, e.g., FDA, NIH, reputable medical journal>\n"
    "- <Relevant and reliable source URL 2, if available>\n"
    "\n"
    "**IMPORTANT DISCLAIMER:** This information is for general knowledge only and does not constitute medical advice. "
    "Always consult a qualified healthcare professional (doctor or pharmacist) before taking or combining any medications "
    "to ensure safety and appropriateness for your specific health conditions. Do not rely solely on this information "
    "for medical decisions."
)

@mcp.tool(description=MedInteractionsDesc.model_dump_json())
async def med_interactions(
    # Use Annotated with a more descriptive Field for clarity in generated docs
    medicines: Annotated[List[str], Field(description="A list of two or more medicine names (e.g., 'Paracetamol', 'Ibuprofen') to check for interactions.")]
) -> str:
    """
    Provides a report on potential interactions, symptoms, and side effects
    when two or more specified medicines are taken together, generated by Gemini AI.
    Always includes a disclaimer to consult a healthcare professional.
    """
    logger.info(f"med_interactions tool called with medicines: {medicines}")

    if not medicines or len(medicines) < 2:
        logger.warning("med_interactions called with insufficient number of medicines.")
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Please provide at least two medicines to check for interactions."
        ))

    med_list_str = ", ".join(medicines)
    user_prompt = (
        f"Analyze the potential interactions, common symptoms, and harmful side effects when the following "
        f"medicines are taken together: {med_list_str}. "
        "Also, provide highly relevant and reputable online references (URLs) where this information can be verified."
    )

    try:
        summary = await Gemini.ask_gemini(
            prompt=user_prompt,
            api_key=settings.GEMINI_API_KEY,
            system_format=GEMINI_MEDICAL_ASSISTANT_PROMPT
        )
        logger.info("Successfully received interaction summary from Gemini.")
    except McpError as e:
        logger.error(f"Error fetching Gemini response for med_interactions: {e.message}")
        # Re-raise the MCP error directly
        raise
    except Exception as e:
        logger.exception("An unexpected error occurred during med_interactions processing.")
        raise McpError(ErrorData(
            code=INTERNAL_ERROR,
            message=f"An unexpected error occurred while checking interactions: {e!r}"
        ))

    # Basic content validation/fallback if Gemini returns an error string
    if "<error>" in summary:
        logger.warning(f"Gemini response contained an error tag: {summary}")
        # Optionally, you could try to re-prompt or use a simpler fallback message here
        return (
            "💊 **Medicine Interaction Report**\n"
            f"Medicines: {med_list_str}\n"
            "Symptoms: Unable to retrieve detailed symptoms at this time.\n"
            "Harmful Side Effects: Unable to retrieve detailed side effects at this time.\n"
            "References:\n"
            "- No references available due to API error.\n"
            "\n"
            "**IMPORTANT DISCLAIMER:** This information is for general knowledge only and does not constitute medical advice. "
            "Always consult a qualified healthcare professional (doctor or pharmacist) before taking or combining any medications "
            "to ensure safety and appropriateness for your specific health conditions. Do not rely solely on this information "
            "for medical decisions."
        )
    return summary

# --- Run MCP Server ---
async def start_server():
    """
    Starts the FastMCP server.
    """
    logger.info(f"🚀 Starting PuchMeds MCP server on http://{settings.SERVER_HOST}:{settings.SERVER_PORT}")
    try:
        await mcp.run_async("streamable-http", host=settings.SERVER_HOST, port=settings.SERVER_PORT)
    except Exception as e:
        logger.critical(f"Failed to start MCP server: {e!r}")
        raise

if __name__ == "__main__":
    # Ensure correct event loop for asyncio if running outside of a web framework
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Application terminated due to an unhandled error: {e!r}")