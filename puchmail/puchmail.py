import asyncio
import os
from typing import Annotated, Optional, List
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, EmailStr
import httpx
from supabase import create_client, Client
import json
import base64

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Using the new variable names as you provided
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GMAIL_REDIRECT_URI = "https://developers.google.com/oauthplayground"
MY_NUMBER = os.environ.get("MY_NUMBER")

assert TOKEN and SUPABASE_URL and SUPABASE_KEY
assert GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET

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
                client_id="puchmail-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Supabase connection ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session state ---
current_mail_user: dict = {"user_id": None}

# --- Mail Manager ---
class PuchMailManager:
    # --- Supabase Interactions ---
    def _upsert_user_entry(self, provider: str, email: str, access_token: str, refresh_token: str) -> str:
        """
        Inserts or updates a user entry in Supabase.
        """
        existing = supabase.table("puchmail").select("*").eq("email", email).execute()
        
        if existing.data:
            # User exists, update their credentials
            res = supabase.table("puchmail").update({
                "access_token": access_token,
                "refresh_token": refresh_token
            }).eq("email", email).execute()
            if res.data:
                return f"🔄 Credentials updated for {email}."
        else:
            # User does not exist, insert a new entry
            res = supabase.table("puchmail").insert({
                "provider": provider,
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token
            }).execute()
            if res.data:
                return f"🆕 Signup successful for {provider}! Welcome, {email}."
        
        return "❌ Failed to create or update mail account. Please try again."

    def login(self, email: str, access_token: str) -> str:
        """
        Logs a user in and sets the current session state.
        This is now an internal method called during signup.
        """
        global current_mail_user
        user = supabase.table("puchmail").select("*").eq("email", email).eq("access_token", access_token).execute()
        
        if not user.data:
            return "🚫 Invalid email or access token. Please try again."
        
        current_mail_user["user_id"] = user.data[0]["user_id"]
        return f"🔑 Logged in as {email}."

    def logout(self) -> str:
        """
        Logs the current user out.
        """
        global current_mail_user
        if not current_mail_user["user_id"]:
            return "⚠️ You are not logged in."
        current_mail_user["user_id"] = None
        return "🚪 Logged out from mail account."

    def get_credentials(self):
        """
        Retrieves the logged-in user's credentials and provider from Supabase.
        """
        if not current_mail_user["user_id"]:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Not logged in. Please login first."))
            
        res = supabase.table("puchmail").select("*").eq("user_id", current_mail_user["user_id"]).execute()
        
        if not res.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Credentials not found."))
        
        user_data = res.data[0]
        return user_data["provider"], user_data["email"], user_data["access_token"], user_data.get("refresh_token")

    # --- Email Sending Logic ---
    async def send_email(self, to: List[str], subject: str, body: str) -> str:
        """
        Sends an email using the provider's API based on the logged-in user.
        """
        try:
            provider, sender_email, access_token, refresh_token = self.get_credentials()
            
            if provider == "gmail":
                return await self._send_with_gmail(to, subject, body, access_token, refresh_token, sender_email)
            else:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unsupported provider: {provider}"))
        except McpError:
            raise
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred during email sending: {e}"))

    async def _send_with_gmail(self, to: List[str], subject: str, body: str, access_token: str, refresh_token: str, sender_email: str) -> str:
        """
        Private method to send an email using the Gmail API.
        Automatically refreshes the token if needed.
        """
        async def refresh_access_token():
            url = "https://oauth2.googleapis.com/token"
            payload = {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
            async with httpx.AsyncClient() as client:
                res = await client.post(url, data=payload)
                res.raise_for_status()
                new_tokens = res.json()
                new_access_token = new_tokens["access_token"]
                
                # Update the Supabase record with the new access token
                supabase.table("puchmail").update({"access_token": new_access_token}).eq("email", sender_email).execute()
                
                return new_access_token

        # Check if the access token is valid (a simple check, better would be to check expiry time)
        async with httpx.AsyncClient() as client:
            try:
                res = await client.get("https://www.googleapis.com/oauth2/v1/tokeninfo", params={"access_token": access_token})
                res.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400: # Token is expired or invalid
                    access_token = await refresh_access_token()
                else:
                    raise

        # Prepare the email message in RFC 2822 format
        message = f"From: {sender_email}\nTo: {', '.join(to)}\nSubject: {subject}\n\n{body}"
        
        # Encode the message to base64url format
        encoded_message = base64.urlsafe_b64encode(message.encode("utf-8")).decode("utf-8")
        
        url = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        payload = {"raw": encoded_message}

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=json.dumps(payload))
            response.raise_for_status()
        
        return f"✅ Email sent successfully via Gmail API to: {', '.join(to)}"


# --- Create manager instance ---
puchmail_manager = PuchMailManager()

# --- MCP Server ---
mcp = FastMCP(
    "PuchMail MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

# --- Tool: generate_auth_url ---
@mcp.tool
async def generate_auth_url() -> str:
    """
    Generates the Google OAuth 2.0 authorization URL for the user to visit.
    The user must copy the 'code' parameter from the URL they are redirected to.
    Or visit GitHub for more information and the URL.
    """
    import urllib.parse
    scope = "https://www.googleapis.com/auth/gmail.send"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent"
    }
    url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    # Instead of sending as a link, just return the URL as plain text
    return f"Copy and open this URL in your browser to authorize:\n{url}\nIf failed, ask user to visit github repository to get the url"

# --- Tool: complete_signup ---
@mcp.tool
async def complete_signup(
    email: Annotated[EmailStr, Field(description="Your sender email address")],
    auth_code: Annotated[str, Field(description="The authorization code from the redirect URL")]
) -> str:
    """
    Exchanges the authorization code for an access token and a refresh token,
    then signs up and logs in the user in a single step.
    """
    url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": auth_code,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, data=payload)
            res.raise_for_status()
            tokens = res.json()
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]
            
            # Upsert the user entry
            upsert_result = puchmail_manager._upsert_user_entry(
                provider="gmail",
                email=email,
                access_token=access_token,
                refresh_token=refresh_token
            )
            # Log the user in immediately after successful upsert
            login_result = puchmail_manager.login(email, access_token)
            
            return f"{upsert_result}\n{login_result} . RETURN THAT THIS SPECIFIC USER HAD COMPLETED THE SIGNUP, NO MATTER WHAT EMAIL IS USED WHILE SIGNUP,THE EMAIL IS AUTOMATICALLY SET ACCORDINGLY."
    except httpx.HTTPStatusError as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to exchange authorization code: {e.response.text}"))

# --- Tool: logout_mail ---
@mcp.tool
async def logout_mail() -> str:
    """
    Logs out the current user.
    """
    return puchmail_manager.logout()

# --- Tool: send_email ---
@mcp.tool
async def send_email(
    to: Annotated[List[EmailStr], Field(description="List of recipient email addresses")],
    subject: Annotated[str, Field(description="Email subject")],
    body: Annotated[str, Field(description="Email body")]
) -> str:
    """
    Sends an email using the logged-in user's credentials.
    """
    return await puchmail_manager.send_email(to, subject, body)




# --- Tool: help ---
@mcp.tool
async def help_menu() -> str:
    """
    Shows the help menu with all available commands and emojis.
    """
    return (
        "ℹ️ **Help Menu**\n"
        "🔑 - Log in to your mail account\n"
        "🚪 - Log out from your mail account\n"
        "✉️ - Send an email\n"
        "🔗 - Generate Google OAuth authorization URL\n"
        "✅ - Complete signup with authorization code\n"
    )
# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchMail MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
