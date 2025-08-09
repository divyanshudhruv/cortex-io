import asyncio
import os
import logging
from typing import Annotated, List, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, SecretStr
import httpx
import json
from functools import lru_cache

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Assertions for crucial environment variables
assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER, "Please set MY_NUMBER in your .env file"
assert GEMINI_API_KEY, "Please set GEMINI_API_KEY in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    """
    Simple Bearer Token authentication provider.
    """
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            logger.info("Authentication successful for client_id: puch-client")
            return AccessToken(
                token=token,
                client_id="puch-client",
                scopes=["*"],
                expires_at=None,
            )
        logger.warning("Authentication failed: Invalid token provided.")
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str = Field(..., description="A concise summary of the tool's purpose.")
    use_when: str = Field(..., description="Guidance on when this tool should be invoked.")
    side_effects: Optional[str] = Field(None, description="Description of any side effects or outputs.")
    structure: Optional[str] = Field(None, description="Expected format of the tool's output.")

# --- Gemini API Utility ---
class Gemini:
    """
    Utility class for interacting with the Google Gemini API.
    Uses the provided GEMINI_API_KEY directly.
    """
    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    GEMINI_API_TIMEOUT = 30 # Default timeout for Gemini API requests

    @staticmethod
    @lru_cache(maxsize=128) # Basic caching for Gemini responses
    async def _cached_ask_gemini_content(prompt_key: str, system_format: Optional[str]) -> str:
        """
        Internal method for caching Gemini responses. Caches based on prompt and system format.
        The GEMINI_API_KEY is embedded in the header, not part of the cache key.
        """
        prompt = json.loads(prompt_key)['prompt'] # Reconstruct prompt from serialized key
        logger.info(f"Calling Gemini API for prompt: {prompt[:50]}...")

        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": GEMINI_API_KEY, # Using global GEMINI_API_KEY directly
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
                resp = await client.post(Gemini.API_URL, headers=headers, json=data, timeout=Gemini.GEMINI_API_TIMEOUT)
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
    async def ask_gemini(prompt: str, system_format: Optional[str] = None) -> str:
        """
        Public method to ask Gemini. Handles caching logic.
        """
        prompt_key = json.dumps({'prompt': prompt, 'system_format': system_format})
        return await Gemini._cached_ask_gemini_content(prompt_key, system_format)

# --- MCP Server Setup ---
mcp = FastMCP(
    "Simple Med Side Effect MCP",
    auth=SimpleBearerAuthProvider(TOKEN),
)

@mcp.tool
async def validate() -> str:
    """
    Validation tool required by the Puch platform. Returns the server's unique identifier.
    """
    logger.info("Validate tool called.")
    return MY_NUMBER

class SideEffectRequest(BaseModel):
    medicines: Annotated[str | None, Field(description="Names of medicines (comma-separated if multiple).")] = None
    symptoms: Annotated[str | None, Field(description="Symptoms or keywords to search for side effects.")] = None

async def find_side_effects_and_references_with_gemini(query: str) -> tuple[List[str], List[str]]:
    """
    Uses Gemini to find side effects and reference links directly.
    """
    prompt = (
        f"Find potential side effects and relevant external web links for the following query: '{query}'. "
        "Extract the side effects as a comma-separated list and the full URLs (including http/https) "
        "of the top 3 most relevant external web pages where this information can be found. "
        "Format the output as a JSON object with 'side_effects' (list of strings) and 'reference_urls' (list of strings). "
        "Example: {'side_effects': ['nausea', 'dizziness'], 'reference_urls': ['https://example.com/med1', 'https://example.org/med2']}"
    )
    
    try:
        gemini_response = await Gemini.ask_gemini(prompt)
        response_data = json.loads(gemini_response)
        side_effects_list = response_data.get('side_effects', [])
        reference_urls_list = response_data.get('reference_urls', [])
        return side_effects_list, reference_urls_list
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse Gemini's response for side effects and URLs: {e!r}. Response: {gemini_response}")
        return [], []
    except McpError as e:
        logger.error(f"Gemini API error during side effects and URL extraction: {e.message}")
        return [], []
    except Exception as e:
        logger.exception("Unexpected error during Gemini side effects and URL extraction.")
        return [], []

# --- Tool Description for the improved side_effects tool ---
SideEffectsToolDesc = RichToolDescription(
    description="Provides summarized potential side effects for given medicines or symptoms. It uses AI to find and summarize information from various sources and provides reference links.",
    use_when="When a user asks about side effects of specific medicines or symptoms, or interactions between medicines that might cause side effects.",
    side_effects="Returns a structured report including a summary of common and harmful side effects, and references to original sources. Always includes a disclaimer.",
    structure=(
        "💊 **Medication Side Effects Report**\n"
        "**Medicines/Symptoms Queried:** <list of queried items>\n"
        "**Summary of Potential Side Effects:** <AI-generated summary of effects>\n"
        "**Important Notes/Harmful Side Effects:** <AI-generated summary of severe effects/warnings>\n"
        "**Source URLs:**\n"
        "- <URL 1>\n"
        "- <URL 2>\n"
        "\n"
        "**Disclaimer:** This information is generated by AI based on publicly available data and is for informational purposes only. It is NOT a substitute for professional medical advice, diagnosis, or treatment. Always seek the advice of your physician or other qualified health provider with any questions you may have regarding a medical condition or before taking any new medication or discontinuing an existing one. Mixing medications can be dangerous; consult a healthcare professional."
    )
)

GEMINI_SIDE_EFFECTS_SYSTEM_PROMPT = (
    "You are a helpful medical assistant. Your task is to summarize information about medicine side effects "
    "or symptom-related side effects based on provided text snippets. "
    "You must generate a report with two main sections: 'Summary of Potential Side Effects' "
    "and 'Important Notes/Harmful Side Effects'. "
    "Be concise, informative, and always prioritize patient safety. "
    "DO NOT invent information. ONLY use the provided text snippets for your summary. "
    "If no relevant information is available in the snippets, state that clearly. "
    "Ensure your summary is in a natural language format, not just a list of bullet points.\n\n"
    "Example of expected output structure (do not include the disclaimer, it will be added by the tool):\n"
    "**Summary of Potential Side Effects:** <Concise paragraph summarising common effects based on provided text>\n"
    "**Important Notes/Harmful Side Effects:** <Concise paragraph summarising severe effects/warnings based on provided text>"
)

@mcp.tool(description=SideEffectsToolDesc.model_dump_json())
async def side_effects(
    medicines: Annotated[str | None, Field(description="Names of medicines (comma-separated if multiple).")] = None,
    symptoms: Annotated[str | None, Field(description="Symptoms or keywords to search for side effects.")] = None,
) -> str:
    """
    Search for side effects for the given medicines or user input and summarize them using AI.
    """
    logger.info(f"Side effects tool called. Medicines: '{medicines}', Symptoms: '{symptoms}'")

    if not medicines and not symptoms:
        logger.warning("Side effects tool called without any valid input.")
        return (
            "Please provide medicine names (e.g., 'Paracetamol, Ibuprofen') or symptoms (e.g., 'headache') "
            "to search for side effects.\n\n"
            + SideEffectsToolDesc.structure.split('**Disclaimer:**')[1]
        )

    queried_items = []
    queries_for_gemini = []
    if medicines:
        med_names = [med.strip() for med in medicines.split(",") if med.strip()]
        for med in med_names:
            queries_for_gemini.append(f"{med} side effects")
            queried_items.append(med)
    if symptoms:
        symptom_phrases = [symptom.strip() for symptom in symptoms.split(",") if symptom.strip()]
        for symp in symptom_phrases:
            queries_for_gemini.append(f"{symp} side effects")
            queried_items.append(symp)

    all_extracted_side_effects_text = []
    all_source_urls = set()

    # Use Gemini to find side effects and references for each query
    gemini_tasks = [find_side_effects_and_references_with_gemini(q) for q in queries_for_gemini]
    results_from_gemini = await asyncio.gather(*gemini_tasks)

    for side_effects_list, reference_urls_list in results_from_gemini:
        all_extracted_side_effects_text.extend(side_effects_list)
        all_source_urls.update(reference_urls_list)

    unique_extracted_effects = list(set([effect.strip() for effect in all_extracted_side_effects_text if effect.strip()]))
    
    if not unique_extracted_effects:
        logger.warning("No relevant side effects could be extracted from any sources by Gemini.")
        report_summary = (
            f"**Summary of Potential Side Effects:** No specific side effects were found or extracted from reliable sources for the query by AI.\n"
            f"**Important Notes/Harmful Side Effects:** Please consult a healthcare professional for accurate information."
        )
    else:
        gemini_input_text = "\n".join(unique_extracted_effects[:50]) # Limit input to Gemini
        logger.info(f"Sending {len(unique_extracted_effects)} unique side effects to Gemini for summary.")
        summary_prompt = (
            f"Based on the following list of potential side effects and related information, provide a concise summary. "
            f"Separate the summary into 'Summary of Potential Side Effects' (common effects) and "
            f"'Important Notes/Harmful Side Effects' (severe warnings if any).\n\n"
            f"Text snippets:\n{gemini_input_text}"
        )
        try:
            gemini_response = await Gemini.ask_gemini(
                prompt=summary_prompt,
                system_format=GEMINI_SIDE_EFFECTS_SYSTEM_PROMPT
            )
            if "**Summary of Potential Side Effects:**" not in gemini_response:
                logger.warning("Gemini response lacked expected structure for summary. Using raw effects.")
                report_summary = (
                    f"**Summary of Potential Side Effects:** {', '.join(unique_extracted_effects[:20])}\n"
                    f"**Important Notes/Harmful Side Effects:** Please consult a healthcare professional for specific concerns."
                )
            else:
                report_summary = gemini_response
        except McpError as e:
            logger.error(f"Gemini API error during summary: {e.message}")
            report_summary = (
                f"**Summary of Potential Side Effects:** Could not generate a comprehensive summary due to an AI service error.\n"
                f"**Important Notes/Harmful Side Effects:** {e.message}"
            )
        except Exception as e:
            logger.exception("Unexpected error during Gemini summary generation.")
            report_summary = (
                f"**Summary of Potential Side Effects:** An unexpected error occurred while generating summary.\n"
                f"**Important Notes/Harmful Side Effects:** {e!r}"
            )

    # Construct the final report
    final_report = []
    final_report.append("💊 **Medication Side Effects Report**")
    final_report.append(f"**Medicines/Symptoms Queried:** {', '.join(queried_items)}")
    final_report.append(report_summary)

    final_report.append("**Source URLs:**")
    if all_source_urls:
        for url in sorted(list(all_source_urls)):
            final_report.append(f"- {url}")
    else:
        final_report.append("- No specific source URLs found or extracted by AI.")

    final_report.append(SideEffectsToolDesc.structure.split('**Disclaimer:**')[1])

    return "\n\n".join(final_report + ["SHARE THIS REPORT ORIGINAL, DONT CHANGE OR UPDATE IT, KEEP THE REPORT SAME"])
    
async def main():
    """
    Main function to start the FastMCP server.
    """
    logger.info("🚀 Starting Simple Med Side Effect MCP server on http://0.0.0.0:8086")
    try:
        await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)
    except Exception as e:
        logger.critical(f"Failed to start MCP server: {e!r}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Application terminated due to an unhandled error: {e!r}")