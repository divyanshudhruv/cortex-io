import asyncio
import os
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INTERNAL_ERROR
from pydantic import BaseModel, Field
from typing import Annotated

import httpx
from bs4 import BeautifulSoup

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puch-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- MCP Server Setup ---
mcp = FastMCP(
    "Simple Med Side Effect MCP",
    auth=SimpleBearerAuthProvider(TOKEN),
)

@mcp.tool
async def validate() -> str:
    return MY_NUMBER

class SideEffectRequest(BaseModel):
    medicines: Annotated[str | None, Field(description="Names of medicines (comma-separated if multiple).")] = None
    symptoms: Annotated[str | None, Field(description="Symptoms or keywords to search for side effects.")] = None

async def search_side_effects(query: str, num_results: int = 3) -> list[str]:
    url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
    links = []
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"User-Agent": "Puch/1.0"})
        if resp.status_code != 200:
            return ["<error>Failed to perform search.</error>"]
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", class_="result__a", href=True):
            href = a["href"]
            if "http" in href:
                links.append(href)
            if len(links) >= num_results:
                break
    return links or ["<error>No results found.</error>"]

async def extract_side_effects_from_url(url: str) -> list[str]:
    """Try to extract side effects from a known page structure (Drugs.com, NHS, WebMD, etc)."""
    side_effects = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"User-Agent": "Puch/1.0"})
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            # Drugs.com
            if "drugs.com" in url:
                ul = soup.find("ul", class_="ddc-list-bullet")
                if ul:
                    for li in ul.find_all("li"):
                        text = li.get_text(strip=True)
                        if text:
                            side_effects.append(text)
            # NHS
            elif "nhs.uk" in url:
                for li in soup.find_all("li"):
                    text = li.get_text(strip=True)
                    if "side effect" in text.lower() or "may cause" in text.lower():
                        side_effects.append(text)
            # WebMD
            elif "webmd.com" in url:
                for li in soup.find_all("li"):
                    text = li.get_text(strip=True)
                    if text and len(text) < 120:
                        side_effects.append(text)
            # Fallback: just collect all <li> items
            if not side_effects:
                for li in soup.find_all("li"):
                    text = li.get_text(strip=True)
                    if text and len(text) < 120:
                        side_effects.append(text)
    except Exception:
        pass
    return side_effects
@mcp.tool
async def side_effects(
    medicines: Annotated[str | None, Field(description="Names of medicines (comma-separated if multiple).")] = None,
    symptoms: Annotated[str | None, Field(description="Symptoms or keywords to search for side effects.")] = None,
) -> str:
    """
    Search for side effects for the given medicines or user input.
    """
    if not medicines and not symptoms:
        return (
            "Please provide medicine names or symptoms to search for side effects.\n\n"
            "**Important Disclaimer:** I am an AI and cannot provide medical advice. This information is for general knowledge and informational purposes only, and does not constitute medical advice. It is essential to consult with a qualified healthcare professional for any health concerns or before making any decisions related to your health or treatment. Mixing medications can be dangerous, and a doctor can advise you on the appropriate course of action."
        )

    queries = []
    med_names = []
    if medicines:
        med_names = [med.strip() for med in medicines.split(",") if med.strip()]
        queries = [f"{med} side effects" for med in med_names]
    elif symptoms:
        queries = [f"{symptoms.strip()} side effects"]

    results = []
    all_side_effects = set()
    for idx, q in enumerate(queries):
        links = await search_side_effects(q)
        results.append(f"## {q}\n" + "\n".join(f"- {link}" for link in links))
        # Try to extract side effects from the first link if it's a known site
        if links:
            if "drugs.com" in links[0] or "nhs.uk" in links[0] or "webmd.com" in links[0]:
                se = await extract_side_effects_from_url(links[0])
                all_side_effects.update(se)

    # Limit to 20 unique side effects
    side_effects_list = list(all_side_effects)[:20]
    if side_effects_list:
        results.append(
            "\n### Potential Side Effects (max 20 combined):\n"
            + "\n".join(f"- {se}" for se in side_effects_list)
        )

    results.append(
        "\n**Important Disclaimer:** I am an AI and cannot provide medical advice. This information is for general knowledge and informational purposes only, and does not constitute medical advice. It is essential to consult with a qualified healthcare professional for any health concerns or before making any decisions related to your health or treatment. Mixing medications can be dangerous, and a doctor can advise you on the appropriate course of action."
    )

    return "\n\n".join(results)


async def main():
    print("🚀 Starting MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
