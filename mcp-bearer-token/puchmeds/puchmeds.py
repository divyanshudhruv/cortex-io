import asyncio
import os
import httpx
from typing import Annotated, List, Dict, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import McpError, ErrorData
from mcp.types import INVALID_PARAMS
from pydantic import Field

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN", "your_secret_token")
assert TOKEN, "AUTH_TOKEN environment variable not set."
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- Auth Provider (Simple for this example) ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="side-effects-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- MCP Server ---
mcp = FastMCP(
    "Side Effects MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Dynamic AI API Call and Parsing Function ---
async def fetch_and_parse_medicine_info(med_name: str) -> Dict[str, List[str]]:
    """
    Performs an API call to the Gemini API to get structured medicine information.
    
    Args:
        med_name: The name of the medicine (e.g., "ibuprofen").
    
    Returns:
        A dictionary with lists of strings for side effects, prevention, and posture.
    """
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    
    # Prompt the AI to provide information in a structured JSON format
    prompt = (
        f"Respond ONLY with a JSON object: "
        f'{{"side_effects": [5 strings], "prevention": [5 strings], "posture": [5 strings]}}. '
        f"No explanation or extra text. "
        f"For '{med_name}', list 5 common side effects, 5 prevention methods, and 5 helpful postures."
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

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, timeout=30)
            response.raise_for_status()  # Raise an exception for bad status codes
            
            response_json = response.json()
            parts = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
            
            if parts and "text" in parts[0]:
                data = parts[0]["text"]
                # The response is a JSON string, so we need to parse it
                import json
                try:
                    parsed_data = json.loads(data)
                    return {
                        "side_effects": parsed_data.get("side_effects", ["Not found"]),
                        "prevention": parsed_data.get("prevention", ["Not found"]),
                        "posture": parsed_data.get("posture", ["Not found"]),
                    }
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON response from API: {e}")
                    return {
                        "side_effects": ["Error in API response"],
                        "prevention": ["Error in API response"],
                        "posture": ["Error in API response"],
                    }

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e:
        print(f"An error occurred during the request: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    
    # Default return for any errors
    return {
        "side_effects": ["Error or not found"],
        "prevention": ["Error or not found"],
        "posture": ["Error or not found"],
    }


@mcp.tool
async def explain_side_effects(
    meds: Annotated[List[str], Field(description="A list of medicine names to check")]
) -> str:
    """
    Parses a list of medicine names and uses an AI API to provide
    information on side effects, prevention, and helpful postures.
    """
    if not meds:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Input list of medicine names cannot be empty."))

    results = []
    
    import anyio

    for name in meds:
        try:
            info = await fetch_and_parse_medicine_info(name)
        except anyio.ClosedResourceError:
            print("Client disconnected or resource closed. Stopping processing.")
            return "Client disconnected or resource closed before response could be sent."
        
        # Format the output systematically with numbered lists
        side_effects_str = "\n".join([f"    {i+1}. {item}" for i, item in enumerate(info["side_effects"])])
        prevention_str = "\n".join([f"    {i+1}. {item}" for i, item in enumerate(info["prevention"])])
        posture_str = "\n".join([f"    {i+1}. {item}" for i, item in enumerate(info["posture"])])

        results.append(
            f"**{name.title()}**\n"
            f"  - **Symptoms/Side Effects:**\n{side_effects_str}\n"
            f"  - **Prevention:**\n{prevention_str}\n"
            f"  - **Helpful Posture:**\n{posture_str}\n"
        )
    
    if not results:
        return "No information was found for the provided medicine names."

    combined_output = "### Medicine Information Summary\n\n" + "\n".join(results)
    
    combined_output += "\n---\n\n"
    combined_output += "Please note: This is a general summary generated by an AI model. Always consult a healthcare professional for specific advice on medicine interactions and individual health needs."
    
    return combined_output

# --- Run MCP Server ---
async def main():
    print("🚀 Starting Side Effects MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
