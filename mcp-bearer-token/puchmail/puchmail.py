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
import json
from urllib.parse import urlencode, quote

# --- Load environment variables ---
# This server requires an .env file with the following variables:
# AUTH_TOKEN: A secret token for the MCP server itself.
# MY_NUMBER: An identifier for the `validate` tool.
# SUPABASE_URL: The URL of your Supabase project (e.g., https://<project-ref>.supabase.co).
# SUPABASE_ANON_KEY: Your Supabase project's public "anon" key.
# SUPABASE_REDIRECT_URI: The URL Supabase should redirect to after a successful sign-in.
#                       This is a crucial part of the OAuth flow.
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_REDIRECT_URI = os.environ.get("SUPABASE_REDIRECT_URI")

# Assert that all required environment variables are set
assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER, "Please set MY_NUMBER in your .env file"
assert SUPABASE_URL, "Please set SUPABASE_URL in your .env file"
assert SUPABASE_ANON_KEY, "Please set SUPABASE_ANON_KEY in your .env file"
assert SUPABASE_REDIRECT_URI, "Please set SUPABASE_REDIRECT_URI in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    """
    Handles authentication for the MCP server itself.
    This is separate from the Google OAuth flow.
    """
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
# Global dictionary to hold the current user's authenticated session.
# This is where we'll store the Google access token and email after sign-in.
current_user: dict = {"email": None, "access_token": None, "provider_token": None}

# --- Supabase Google Auth & Gmail helpers ---
class SupabaseGoogleAuthManager:
    """
    A collection of static methods to manage the OAuth flow via Supabase
    and interact with the Gmail and People APIs.
    """
    @staticmethod
    def get_signin_url() -> str:
        """
        Constructs the Google sign-in URL using the Supabase authorization endpoint.
        The `scopes` and `queryParams` are configured to allow sending emails,
        reading emails, and to force a Gmail account selection.
        """
        # Scopes required for Gmail API
        gmail_scopes = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly openid email"
        
        # Query parameters to force account selection and Gmail login
        query_params = {
            "prompt": "select_account",
            "hd": "gmail.com",
            "access_type": "offline",
            "include_granted_scopes": "true"
        }
        
        params = {
            "provider": "google",
            "redirect_to": SUPABASE_REDIRECT_URI,
            "scopes": gmail_scopes,
            "queryParams": json.dumps(query_params)
        }
        
        # Properly encode query parameters to avoid unsafe characters
        base_url = f"{SUPABASE_URL}/auth/v1/authorize"
        query = urlencode(params, quote_via=quote)
        print(f"Google sign-in URL: {base_url}?{query}")
        return f"{base_url}?{query}"

    @staticmethod
    async def exchange_code_for_tokens(redirect_url: str) -> Optional[dict]:
        """
        Takes the redirect URL from the browser, extracts the authorization code,
        and exchanges it for an access token via Supabase.
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(redirect_url)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        if not code:
            return None
            
        token_url = f"{SUPABASE_URL}/auth/v1/token?grant_type=authorization_code"
        headers = {
            "apikey": SUPABASE_ANON_KEY,
            "Content-Type": "application/json"
        }
        data = {
            "code": code,
            "redirect_uri": SUPABASE_REDIRECT_URI
        }
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(token_url, headers=headers, json=data, timeout=10)
                resp.raise_for_status() # Raise an exception for HTTP errors
                return resp.json()
            except httpx.HTTPStatusError as e:
                print(f"HTTP error during token exchange: {e.response.text}")
                return None
            except httpx.RequestError as e:
                print(f"Request error during token exchange: {e}")
                return None

    @staticmethod
    async def get_user_email(access_token: str) -> Optional[str]:
        """
        Uses the Google People API to get the user's email address
        from the provided access token.
        """
        url = "https://people.googleapis.com/v1/people/me?personFields=emailAddresses"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                emails = data.get("emailAddresses", [])
                if emails:
                    return emails[0].get("value")
                return None
            except httpx.HTTPStatusError as e:
                print(f"HTTP error getting email: {e.response.text}")
                return None
            except httpx.RequestError as e:
                print(f"Request error getting email: {e}")
                return None

    @staticmethod
    async def send_email(access_token: str, to: str, subject: str, body: str) -> str:
        """
        Sends an email using the Gmail API via a POST request.
        The message is constructed and base64-encoded as required by the API.
        """
        import base64
        from email.mime.text import MIMEText
        
        # Sanitize body to avoid potentially harmful content and for security
        def sanitize(text):
            safe = text.replace('\n', ' ').replace('\r', ' ')
            safe = safe.replace('<', '').replace('>', '').replace('&', 'and')
            safe = safe[:1000] # limit length for safety
            return safe
        
        safe_body = sanitize(body)
        message = MIMEText(safe_body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        payload = {"raw": raw}
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=10)
                resp.raise_for_status()
                return "✉️ Email sent successfully!"
            except httpx.HTTPStatusError as e:
                return f"❌ Failed to send email: {e.response.text}"
            except httpx.RequestError as e:
                return f"❌ Request error sending email: {e}"

    @staticmethod
    async def get_top_emails(access_token: str, max_results: int = 5) -> List[str]:
        """
        Fetches the top N emails from the user's inbox using the Gmail API.
        It retrieves message metadata (From, Subject, Date, Snippet) to display a summary.
        """
        def sanitize(text):
            safe = text.replace('\n', ' ').replace('\r', ' ')
            safe = safe.replace('<', '').replace('>', '').replace('&', 'and')
            safe = safe[:500]
            return safe
        
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={max_results}&labelIds=INBOX"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                messages = resp.json().get("messages", [])
                
                if not messages:
                    return ["📭 No emails found."]
                
                emails = []
                for msg in messages:
                    msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date"
                    msg_resp = await client.get(msg_url, headers=headers, timeout=10)
                    msg_resp.raise_for_status()
                    
                    msg_data = msg_resp.json()
                    headers_list = msg_data.get("payload", {}).get("headers", [])
                    headers_dict = {h["name"]: h["value"] for h in headers_list}
                    
                    subject = sanitize(headers_dict.get("Subject", "(No Subject)"))
                    sender = sanitize(headers_dict.get("From", "(Unknown Sender)"))
                    date = sanitize(headers_dict.get("Date", ""))
                    snippet = sanitize(msg_data.get("snippet", ""))
                    
                    emails.append(f"📧 *From*: {sender}\n*Subject*: {subject}\n*Date*: {date}\n*Snippet*: {snippet}")
                    
                return emails
            except httpx.HTTPStatusError as e:
                return [f"❌ Failed to fetch emails: {e.response.text}"]
            except httpx.RequestError as e:
                return [f"❌ Request error fetching emails: {e}"]

# --- MCP Server ---
mcp = FastMCP(
    "PuchEmail MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    """
    A simple tool to confirm the server is running and authenticated.
    """
    return MY_NUMBER

# --- Tool: get_google_signin_url ---
SigninUrlDesc = RichToolDescription(
    description="Get the Google sign-in URL (via Supabase). User should open this URL in a browser, sign in with a Gmail account, and copy the final redirect URL back.",
    use_when="User wants to sign in with Google.",
    side_effects="Provides a URL for Google OAuth2 sign-in via Supabase, forcing Gmail login.",
    structure="🔗 **Google Sign-in URL**\nURL: <sign-in-url>\n"
)
@mcp.tool(description=SigninUrlDesc.model_dump_json())
async def get_google_signin_url() -> str:
    url = SupabaseGoogleAuthManager.get_signin_url()
    return url

# --- Tool: google_signin ---
SigninDesc = RichToolDescription(
    description="Complete Google sign-in by pasting the redirect URL after authentication. Shows a confirmation message with the signed-in email.",
    use_when="User has finished Google sign-in and has the redirect URL.",
    side_effects="Stores the user's Google credentials in session.",
    structure="🔑 **Google Sign-in**\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=SigninDesc.model_dump_json())
async def google_signin(
    redirect_url: Annotated[str, Field(description="Paste the redirect URL from your browser after Google sign-in")]
) -> str:
    tokens = await SupabaseGoogleAuthManager.exchange_code_for_tokens(redirect_url)
    if not tokens or "provider_token" not in tokens:
        return "❌ Failed to exchange code for token. Please try again. The URL you provided may have been invalid."
    access_token = tokens["provider_token"]["access_token"]
    email = await SupabaseGoogleAuthManager.get_user_email(access_token)
    if not email:
        return "❌ Failed to retrieve email address. Please check your token."
    
    # Store credentials in the global session
    current_user["email"] = email
    current_user["access_token"] = access_token
    current_user["provider_token"] = tokens["provider_token"]
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
    if not current_user.get("access_token"):
        return "🔒 Please sign in with Google first."
    return await SupabaseGoogleAuthManager.send_email(current_user["access_token"], to, subject, body)

# --- Tool: read_emails ---
ReadEmailsDesc = RichToolDescription(
    description="Read the top 5 emails from your inbox. User must be signed in with Google. Shows a list of emails with sender, subject, and snippet.",
    use_when="User wants to read their latest emails.",
    side_effects="Fetches the latest emails from the user's Gmail inbox.",
    structure="📬 **Top Emails**\n<list of emails with sender, subject, date, and snippet>\n"
)
@mcp.tool(description=ReadEmailsDesc.model_dump_json())
async def read_emails() -> str:
    if not current_user.get("access_token"):
        return "🔒 Please sign in with Google first."
    emails = await SupabaseGoogleAuthManager.get_top_emails(current_user["access_token"], max_results=5)
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
    current_user["access_token"] = None
    current_user["provider_token"] = None
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
        "🔗 - Get Google sign-in URL\n"
        "🔑 - Complete Google sign-in (paste redirect URL)\n"
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
