import asyncio
import os
import httpx
import json
from typing import Annotated, List, Dict, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import McpError, ErrorData
from mcp.types import INVALID_PARAMS
from pydantic import Field
from google_search import search # Import the Google Search tool
from typing import AsyncGenerator


# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN", "your_secret_token")
assert TOKEN, "AUTH_TOKEN environment variable not set."
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- Auth Provider (Simple for this example) ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    """
    A simple Bearer token authentication provider for development purposes.
    """
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="medicine-info-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- MCP Server ---
mcp = FastMCP(
    "Medicine Info MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

async def search_and_fetch_medicine_info(
    med_name: str,
    num_strings: int
) -> Dict[str, List[str]]:
    """
    Performs a real-time internet search and then uses the results to get structured
    medicine information from the Gemini API. Includes a retry mechanism with
    exponential backoff to handle transient failures.

    Args:
        med_name: The name of the medicine (e.g., "ibuprofen").
        num_strings: Number of strings to request for each field.

    Returns:
        A dictionary with lists of strings for side effects, prevention, and posture.
    """
    
    # 1. Perform a real-time search for the medicine information
    search_query = f"{med_name} side effects, prevention, and helpful posture"
    # CORRECTED: The `search` function is asynchronous and must be awaited.
    search_results = await search(queries=[search_query])

    # Combine search snippets into a single context string
    search_context = ""
    if search_results and search_results[0].results:
        search_context = "\n".join([r.snippet for r in search_results[0].results if r.snippet])
    
    # 2. Use the search results as context for the Gemini API call
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    
    # Prompt the AI to provide information in a structured JSON format
    prompt = (
        f"Based on the following search results, respond ONLY with a JSON object: "
        f'{{"side_effects": [{num_strings} string(s)], "prevention": [{num_strings} string(s)], "posture": [{num_strings} string(s)]}}. '
        f"Use relevant emojis in each string. "
        f"No explanation or extra text. "
        f"For '{med_name}', list {num_strings} common side effect(s), {num_strings} prevention method(s), and {num_strings} helpful posture(s)."
        f"The search results are:\n\n"
        f"{search_context}"
    )

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "side_effects": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    },
                    "prevention": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    },
                    "posture": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    }
                }
            }
        }
    }
    
    # Retry mechanism with exponential backoff
    retries = 3
    base_delay = 1  # seconds
    
    for i in range(retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(api_url, json=payload, timeout=60)
                response.raise_for_status()

            response_json = response.json()
            parts = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])

            if parts and "text" in parts[0]:
                data = parts[0]["text"]
                try:
                    parsed_data = json.loads(data)
                    return {
                        "side_effects": parsed_data.get("side_effects", ["Information not found."]),
                        "prevention": parsed_data.get("prevention", ["Information not found."]),
                        "posture": parsed_data.get("posture", ["Information not found."])
                    }
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON from API: {e}")
                    raise
        
        except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
            print(f"Attempt {i+1} failed: {e}")
            if i < retries - 1:
                delay = base_delay * (2 ** i)
                print(f"Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
            else:
                print("Max retries reached. Giving up.")
                return {
                    "side_effects": ["An error occurred while fetching information."],
                    "prevention": ["An error occurred while fetching information."],
                    "posture": ["An error occurred while fetching information."]
                }
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            return {
                "side_effects": ["An unexpected error occurred."],
                "prevention": ["An unexpected error occurred."],
                "posture": ["An unexpected error occurred."]
            }

@mcp.tool

async def explain_side_effects(
    meds: Annotated[List[str], Field(description="A list of medicine names to check")],
    count: Annotated[int, Field(description="The number of top results to return. This must be a minimum of 5.")]
) -> AsyncGenerator[str, None]:
    """
    Parses a list of medicine names and uses an AI API to provide
    information on side effects, prevention, and helpful postures using real-time search.
    """
    if not meds:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Input list of medicine names cannot be empty."))

    import anyio

    # Ensure the count is at least 5 as requested
    num_to_return = max(count, 5)

    # Instead of building a single string and returning it, this function will now
    # yield multiple strings, allowing the MCP server to send them as separate
    # messages.
    
    yield "## Medicine Information Summary\n\n"

    for name in meds:
        try:
            info = await search_and_fetch_medicine_info(name, num_to_return)
        except anyio.ClosedResourceError:
            print("Client disconnected or resource closed. Stopping processing.")
            yield "Client disconnected or resource closed before response could be sent."
            return  # Stop the generator if the client disconnects
        
        # Format the output systematically and yield it immediately
        side_effects_str = "\n".join([f"    - {item}" for item in info["side_effects"]])
        prevention_str = "\n".join([f"    - {item}" for item in info["prevention"]])
        posture_str = "\n".join([f"    - {item}" for item in info["posture"]])
        
        message = (
            f"### {name.title()}\n"
            f"💊 **Side Effects:**\n{side_effects_str}\n"
            f"🛡️ **Prevention:**\n{prevention_str}\n"
            f"🧘 **Helpful Posture:**\n{posture_str}\n"
        )
        yield message
    
    yield "\n---\n\n"
    yield "Please note: This is a general summary generated by an AI model based on real-time search results. Always consult a healthcare professional for specific advice."


@mcp.tool(description="Shows the help menu for the Medicine Info tool.")
async def help_me() -> str:
    return (
        "ℹ️ **Medicine Info Help Menu**\n"
        "💊 - `/explain_side_effects` : Get side effects, prevention, and postures for medicines.\n"
        "🆘 - `/help_me` : Show this help menu.\n"
        "\n"
        "To use, provide a list of medicine names and the number of results you want (minimum of 5). "
        "Example: `/explain_side_effects [\"ibuprofen\", \"paracetamol\"] 5`"
    )

# --- Run MCP Server ---
async def main():
    print("🚀 Starting Medicine Info MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
