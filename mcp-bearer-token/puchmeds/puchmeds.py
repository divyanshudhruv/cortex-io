import asyncio
import os
from typing import Annotated, List, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field
import httpx
import markdownify
import readabilipy
from bs4 import BeautifulSoup

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")

assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER, "Please set MY_NUMBER in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puchmeds-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: Optional[str] = None
    structure: Optional[str] = None

# --- Fetch Utility Class ---
class Fetch:
    USER_AGENT = "PuchMeds/1.0 (Autonomous)"

    @classmethod
    async def fetch_url(
        cls,
        url: str,
        user_agent: str,
        force_raw: bool = False,
    ) -> str:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    url,
                    follow_redirects=True,
                    headers={"User-Agent": user_agent},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}"))
            if response.status_code >= 400:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url} - status code {response.status_code}"))
            page_raw = response.text
        content_type = response.headers.get("content-type", "")
        is_page_html = "text/html" in content_type
        if is_page_html and not force_raw:
            return cls.extract_content_from_html(page_raw)
        return page_raw

    @staticmethod
    def extract_content_from_html(html: str) -> str:
        ret = readabilipy.simple_json.simple_json_from_html_string(html, use_readability=True)
        if not ret or not ret.get("content"):
            return "<error>Page failed to be simplified from HTML</error>"
        content = markdownify.markdownify(ret["content"], heading_style=markdownify.ATX)
        return content

    @staticmethod
    async def google_search_links(query: str, num_results: int = 3) -> List[str]:
        ddg_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        links = []
        async with httpx.AsyncClient() as client:
            resp = await client.get(ddg_url, headers={"User-Agent": Fetch.USER_AGENT})
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

    @classmethod
    async def fetch_summary_from_search(cls, query: str) -> str:
        links = await cls.google_search_links(query)
        for link in links:
            try:
                content = await cls.fetch_url(link, cls.USER_AGENT)
                # Take first 500 chars as summary for brevity
                summary = content.strip().replace('\n', ' ')
                if summary:
                    return f"**Source:** {link}\n{summary[:500]}{'...' if len(summary) > 500 else ''}"
            except Exception:
                continue
        return "No summary found."

# --- MCP Server ---
mcp = FastMCP(
    "PuchMeds MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

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
    )
)
@mcp.tool(description=MedInteractionsDesc.model_dump_json())
async def med_interactions(
    medicines: Annotated[List[str], Field(description="List of medicine names to check for interactions")]
) -> str:
    if not medicines or len(medicines) < 2:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Please provide at least two medicines to check interactions."))
    med_list = ", ".join(medicines)
    query = f"interaction and side effects of {' and '.join(medicines)} taken together"
    try:
        summary = await Fetch.fetch_summary_from_search(query)
    except Exception:
        summary = "No specific information found about interactions between these medicines."
    # Try to get individual side effects for each medicine
    side_effects_summaries = []
    for med in medicines:
        se_query = f"side effects of {med}"
        try:
            se_summary = await Fetch.fetch_summary_from_search(se_query)
        except Exception:
            se_summary = "No information found."
        side_effects_summaries.append(f"**{med}:**\n{se_summary}")
    # Collect references (top 2 links)
    try:
        links = await Fetch.google_search_links(query, num_results=2)
        references = "\n".join(f"- {link}" for link in links)
    except Exception:
        references = "- <error>Reference search failed.</error>"
    disclaimer = (
        "\n\n**Important Disclaimer:**\n"
        "I am an AI and cannot provide medical advice. Mixing medications can be dangerous. "
        "Please consult a doctor or pharmacist to ensure the safety of combining these medicines."
    )
    return (
        f"💊 **Medicine Interaction Report**\n"
        f"Medicines: {med_list}\n\n"
        f"**Summary of Interactions & Symptoms:**\n{summary}\n\n"
        f"**Harmful Side Effects:**\n" +
        "\n\n".join(side_effects_summaries) +
        f"\n\n**References:**\n{references}"
        f"{disclaimer}"
    )


# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchMeds MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())