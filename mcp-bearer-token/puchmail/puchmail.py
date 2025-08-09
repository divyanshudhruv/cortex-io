import asyncio
import os
import uuid
from typing import Optional
from urllib.parse import urlencode
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_REDIRECT_URI = os.environ.get("SUPABASE_REDIRECT_URI")

assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert SUPABASE_URL, "Please set SUPABASE_URL in your .env file"
assert SUPABASE_REDIRECT_URI, "Please set SUPABASE_REDIRECT_URI in your .env file"

# --- Auth Provider ---
class SimpleJWTAuthProvider(JWTVerifier):
    def __init__(self, token: str):
        super().__init__(public_key="dummy", issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puch-email-client",
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

# --- MCP Server ---
mcp = FastMCP(
    "PuchMail MCP Server",
    auth=SimpleJWTAuthProvider(TOKEN),
)

# --- Tool: get_google_signin_url (Supabase) ---
GoogleSigninDesc = RichToolDescription(
    description="Get a Google sign-in URL for Supabase OAuth. User should open the link, sign in, and copy the final URL.",
    use_when="Use when a user needs to authenticate with Google via Supabase.",
    side_effects="Generates a unique OAuth state and returns a sign-in URL.",
    structure="🔗 Please open this URL in your browser and sign in with your Gmail account. After signing in, copy the final URL from your browser's address bar and paste it into the sign-in command."


)

@mcp.tool(description=GoogleSigninDesc.model_dump_json())
async def get_google_signin_url() -> str:
    state = str(uuid.uuid4())
    params = {
        "provider": "google",
        "redirect_to": SUPABASE_REDIRECT_URI,
        "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly openid email",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    base_url = f"{SUPABASE_URL}/auth/v1/authorize"
    query = urlencode(params, safe=":/ ")
    url = f"{base_url}?{query}"
    print(f"Generated Google sign-in URL: {url}")
    return (
        f"🔗 Please open this URL in your browser and sign in with your Gmail account:\n"
        f"{url}\n\n"
        f"After signing in, copy the final URL from your browser's address bar and paste it into the sign-in command.\n"
        f"URL: {url}"
    )

# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchMail MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
