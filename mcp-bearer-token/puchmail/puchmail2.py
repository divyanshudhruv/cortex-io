import asyncio
from typing import Annotated
import httpx
import json
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, EmailStr

# --- Auth Configuration ---
import os
from dotenv import load_dotenv
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"

# --- In-memory storage for SendGrid credentials ---
sendgrid_credentials = {
    "api_key": None,
    "sender_email": None
}

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

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

# --- MCP Server Setup ---
mcp = FastMCP(
    "Mail Sender MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by the framework) ---
@mcp.tool
async def validate() -> str:
    """A required validation tool."""
    return "Validation OK"

# --- Tool: submit_sendgrid_credentials ---
SubmitCredsDescription = RichToolDescription(
    description="Submit your SendGrid API key and sender email for sending emails.",
    use_when="Call this before sending any emails.",
    side_effects="Stores the credentials in memory for this session.",
)

@mcp.tool(description=SubmitCredsDescription.model_dump_json())
async def submit_sendgrid_credentials(
    api_key: Annotated[str, Field(description="Your SendGrid API key.")],
    sender_email: Annotated[EmailStr, Field(description="The sender email address to use.")]
) -> str:
    """
    Stores the SendGrid API key and sender email for this session.
    """
    sendgrid_credentials["api_key"] = api_key
    sendgrid_credentials["sender_email"] = sender_email
    return "✅ SendGrid credentials submitted successfully."

# --- Tool: send_email ---
SendEmailDescription = RichToolDescription(
    description="Sends an email using the SendGrid API from the configured sender.",
    use_when="Use this to send an email for notifications, reports, or automated messages.",
    side_effects="Sends an email from the sender's account to the specified recipients.",
)

@mcp.tool(description=SendEmailDescription.model_dump_json())
async def send_email(
    to: Annotated[list[EmailStr], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject line of the email.")],
    body: Annotated[str, Field(description="The body content of the email.")]
) -> str:
    """
    Sends an email to one or more recipients using the SendGrid API.
    """
    api_key = sendgrid_credentials.get("api_key")
    sender_email = sendgrid_credentials.get("sender_email")
    if not api_key or not sender_email:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="SendGrid credentials not set. Please call submit_sendgrid_credentials first."))

    url = "https://api.sendgrid.com/v3/mail/send"
    payload = {
        "personalizations": [{"to": [{"email": email} for email in to]}],
        "from": {"email": sender_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}]
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=json.dumps(payload))
            response.raise_for_status()
        return f"✅ Email sent successfully to: {', '.join(to)}"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            error_message = "Authentication failed. Check your SendGrid API key."
        else:
            error_message = f"Failed to send email. Status code: {e.response.status_code}, Response: {e.response.text}"
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=error_message))
    except Exception as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred: {e}"))

# --- Run MCP Server ---
async def main():
    print(f"🚀 Starting Mail Sender MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
