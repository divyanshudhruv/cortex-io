import asyncio
import os
from typing import Annotated, Optional, List
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field
import httpx
import uuid
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import json
from urllib.parse import urlparse, parse_qs
import base64
from email.mime.text import MIMEText

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

# --- Session state ---
current_user: dict = {"email": None, "creds": None}

# --- Google Auth & Gmail helpers ---
class GoogleAuthManager:
    @staticmethod
    def parse_token_or_url(token_or_url: str) -> Optional[Credentials]:
        """
        Accepts a pasted OAuth2 token (JSON) or a URL containing the token.
        Returns google.oauth2.credentials.Credentials or None.
        """
        try:
            if token_or_url.strip().startswith("{"):
                creds_data = json.loads(token_or_url)
            elif "access_token" in token_or_url:
                # Parse URL fragment or query string
                parsed = urlparse(token_or_url)
                qs = parse_qs(parsed.fragment or parsed.query)
                creds_data = {
                    "token": qs.get("access_token", [""])[0],
                    "refresh_token": qs.get("refresh_token", [""])[0] if "refresh_token" in qs else "",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
                    "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
                    "scopes": ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly", "openid", "email"],
                }
            else:
                return None
            creds = Credentials(
                creds_data["token"],
                refresh_token=creds_data.get("refresh_token"),
                token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=creds_data.get("client_id", os.environ.get("GOOGLE_CLIENT_ID")),
                client_secret=creds_data.get("client_secret", os.environ.get("GOOGLE_CLIENT_SECRET")),
                scopes=creds_data.get("scopes", ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly", "openid", "email"]),
            )
            return creds
        except Exception as e:
            return None

    @staticmethod
    def get_gmail_service(creds: Credentials):
        return build("gmail", "v1", credentials=creds)

    @staticmethod
    def get_user_email(creds: Credentials) -> Optional[str]:
        try:
            service = GoogleAuthManager.get_gmail_service(creds)
            profile = service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress")
        except Exception:
            return None

    @staticmethod
    def send_email(creds: Credentials, to: str, subject: str, body: str) -> str:
        try:
            message = MIMEText(body)
            message["to"] = to
            message["subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            service = GoogleAuthManager.get_gmail_service(creds)
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return f"✅ Email sent to {to} (id: {sent.get('id')})"
        except HttpError as e:
            return f"❌ Failed to send email: {e}"
        except Exception as e:
            return f"❌ Failed to send email: {e}"

    @staticmethod
    def get_top_emails(creds: Credentials, max_results: int = 5) -> List[str]:
        try:
            service = GoogleAuthManager.get_gmail_service(creds)
            results = service.users().messages().list(userId="me", maxResults=max_results, labelIds=["INBOX"]).execute()
            messages = results.get("messages", [])
            emails = []
            for msg in messages:
                msg_data = service.users().messages().get(userId="me", id=msg["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
                subject = headers.get("Subject", "(No Subject)")
                sender = headers.get("From", "(Unknown Sender)")
                date = headers.get("Date", "")
                snippet = msg_data.get("snippet", "")
                emails.append(f"📧 *From*: {sender}\n*Subject*: {subject}\n*Date*: {date}\n*Snippet*: {snippet}")
            return emails or ["📭 No emails found."]
        except HttpError as e:
            return [f"❌ Failed to fetch emails: {e}"]
        except Exception as e:
            return [f"❌ Failed to fetch emails: {e}"]

# --- MCP Server ---
mcp = FastMCP(
    "PuchEmail MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

# --- Tool: google_signin ---
SigninDesc = RichToolDescription(
    description="Sign in with Google by pasting the OAuth2 token JSON or the redirect URL after authentication. Shows a confirmation message with the signed-in email.",
    use_when="User wants to sign in with Google to send or read emails.",
    side_effects="Stores the user's Google credentials in session.",
    structure="🔑 **Google Sign-in**\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=SigninDesc.model_dump_json())
async def google_signin(
    token_or_url: Annotated[str, Field(description="Paste the OAuth2 token JSON or the redirect URL after Google sign-in")]
) -> str:
    creds = GoogleAuthManager.parse_token_or_url(token_or_url)
    if not creds:
        return "❌ Failed to parse token or URL. Please try again."
    email = GoogleAuthManager.get_user_email(creds)
    if not email:
        return "❌ Failed to retrieve email address. Please check your token."
    current_user["email"] = email
    current_user["creds"] = creds
    return f"🔑 Signed in as {email}!"

# --- Tool: send_email ---
SendEmailDesc = RichToolDescription(
    description="Send an email to any address. User must be signed in with Google. Shows a confirmation message with an emoji.",
    use_when="User wants to send an email.",
    side_effects="Sends an email using the user's Gmail account.",
    structure="✉️ **Send Email**\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=SendEmailDesc.model_dump_json())
async def send_email(
    to: Annotated[str, Field(description="Recipient email address")],
    subject: Annotated[str, Field(description="Email subject")],
    body: Annotated[str, Field(description="Email body")]
) -> str:
    if not current_user.get("creds"):
        return "🔒 Please sign in with Google first."
    return GoogleAuthManager.send_email(current_user["creds"], to, subject, body)

# --- Tool: read_emails ---
ReadEmailsDesc = RichToolDescription(
    description="Read the top 5 emails from your inbox. User must be signed in with Google. Shows a list of emails with sender, subject, and snippet.",
    use_when="User wants to read their latest emails.",
    side_effects="Fetches the latest emails from the user's Gmail inbox.",
    structure="📬 **Top Emails**\n<list of emails with sender, subject, date, and snippet>\n"
)
@mcp.tool(description=ReadEmailsDesc.model_dump_json())
async def read_emails() -> str:
    if not current_user.get("creds"):
        return "🔒 Please sign in with Google first."
    emails = GoogleAuthManager.get_top_emails(current_user["creds"], max_results=5)
    return "\n\n".join(emails)

# --- Tool: logout ---
LogoutDesc = RichToolDescription(
    description="Log out from your Google account. Show a confirmation message with an emoji.",
    use_when="User wants to log out.",
    side_effects="Clears the current session user and credentials.",
    structure="🚪 **Logout Result**\nStatus: <success/failure emoji>\nMessage: <logout confirmation message>\n"
)
@mcp.tool(description=LogoutDesc.model_dump_json())
async def logout() -> str:
    if not current_user.get("email"):
        return "⚠️ You are not logged in."
    name = current_user["email"]
    current_user["email"] = None
    current_user["creds"] = None
    return f"🚪 Logged out from {name}. See you next time!"

# --- Tool: help ---
HelpDesc = RichToolDescription(
    description="Show the help menu with all available commands, each with an emoji. Display the help text as a message.",
    use_when="User asks for help or available commands.",
    side_effects="Returns a help message with all commands and emojis.",
    structure="ℹ️ **Help Menu**\n<list of commands with emojis and descriptions>\n"
)
@mcp.tool(description=HelpDesc.model_dump_json())
async def puchmail_help() -> str:
    return (
        "Commands:\n"
        "🔑 - Sign in with Google (paste token or URL)\n"
        "✉️ - Send an email\n"
        "📬 - Read your top 5 emails\n"
        "🚪 - Log out from your Google account\n"
        "ℹ️ - Show this help menu"
    )

# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchEmail MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())