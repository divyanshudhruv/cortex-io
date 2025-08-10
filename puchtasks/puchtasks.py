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
import urllib.parse

# --- Load environment variables from .env file ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID_2")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET_2")
# The redirect URI for the OAuth flow. This must match what is configured in your Google Cloud Console.
# For manual code copy, developers.google.com/oauthplayground is a common choice.
GOOGLE_KEEP_REDIRECT_URI = "https://developers.google.com/oauthplayground"
MY_NUMBER = os.environ.get("MY_NUMBER")

# Assert that all required environment variables are set. The program will exit if they're not.
assert TOKEN and SUPABASE_URL and SUPABASE_KEY
assert GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
assert MY_NUMBER

# --- Google Keep API constants ---
GOOGLE_KEEP_API = "https://keep.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
# The scope for the Google Keep API. This grants access to manage (read, write, delete) notes.
SCOPE = ["https://www.googleapis.com/auth/keep"]

# --- Auth Provider for FastMCP ---
# This is a simple bearer token provider for the FastMCP server.
# It checks if the incoming token matches the one from the environment variables.
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puchkeep-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Supabase connection ---
# Initializes the Supabase client using the URL and key from the environment.
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session state for currently active user ---
# This dictionary holds the user's session state. It can be easily extended for multi-user support.
current_keep_user: dict = {"user_id": None, "email": None}

# --- Google Keep Manager Class ---
# This class encapsulates all the logic for interacting with Supabase and the Google Keep API.
class PuchKeepManager:
    # --- Supabase Interactions ---
    def _upsert_user_entry(self, provider: str, email: str, access_token: str, refresh_token: str) -> str:
        """
        Inserts or updates a user entry in the 'puchkeep' Supabase table.
        This handles both new signups and updating existing users' tokens.
        """
        existing = supabase.table("puchkeep").select("*").eq("email", email).execute()

        if existing.data:
            res = supabase.table("puchkeep").update({
                "access_token": access_token,
                "refresh_token": refresh_token
            }).eq("email", email).execute()
            if res.data:
                return f"🔄 Google Keep credentials updated for {email}."
        else:
            res = supabase.table("puchkeep").insert({
                "provider": provider,
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token
            }).execute()
            if res.data:
                return f"🆕 Google Keep signup successful! Welcome, {email}."

        return "❌ Failed to create or update Google Keep account. Please try again."

    def login(self, email: str, access_token: str) -> str:
        """
        Logs a user into the current session and sets the global state.
        """
        global current_keep_user
        user = supabase.table("puchkeep").select("*").eq("email", email).eq("access_token", access_token).execute()

        if not user.data:
            return "🚫 Invalid email or access token for Google Keep. Please try again."

        current_keep_user["user_id"] = user.data[0]["user_id"]
        current_keep_user["email"] = user.data[0]["email"]
        return f"🔑 Logged in to Google Keep as {email}."

    def logout(self) -> str:
        """
        Logs the current user out by clearing the global session state.
        """
        global current_keep_user
        if not current_keep_user["user_id"]:
            return "⚠️ You are not logged in to Google Keep."
        current_keep_user["user_id"] = None
        current_keep_user["email"] = None
        return "🚪 Logged out from Google Keep account."

    def get_credentials(self) -> tuple[str, str, str, Optional[str]]:
        """
        Retrieves the logged-in user's credentials from Supabase based on the session state.
        Raises an error if no user is logged in.
        """
        if not current_keep_user["user_id"]:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Not logged in to Google Keep. Please login first."))

        res = supabase.table("puchkeep").select("*").eq("user_id", current_keep_user["user_id"]).execute()

        if not res.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Google Keep credentials not found."))

        user_data = res.data[0]
        return user_data["provider"], user_data["email"], user_data["access_token"], user_data.get("refresh_token")

    async def _refresh_access_token(self, refresh_token: str, email: str) -> str:
        """
        Refreshes the Google Keep access token using the refresh token.
        It also updates the token in the Supabase database.
        """
        url = TOKEN_URL
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

            supabase.table("puchkeep").update({"access_token": new_access_token}).eq("email", email).execute()

            return new_access_token

    async def _get_valid_access_token(self) -> tuple[str, str]:
        """
        Retrieves the user's access token, checking for validity and refreshing it if necessary.
        """
        provider, email, access_token, refresh_token = self.get_credentials()

        async with httpx.AsyncClient() as client:
            try:
                res = await client.get("https://www.googleapis.com/oauth2/v1/tokeninfo", params={"access_token": access_token})
                res.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400 and refresh_token:
                    access_token = await self._refresh_access_token(refresh_token, email)
                else:
                    raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Google Keep token error: {e.response.text}"))
        return access_token, email

    async def add_note(self, title: str, content: str) -> dict:
        """Adds a new note to Google Keep via the API."""
        try:
            access_token, _ = await self._get_valid_access_token()
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            payload = {"title": title, "textContent": content}

            async with httpx.AsyncClient() as client:
                r = await client.post(f"{GOOGLE_KEEP_API}/notes", headers=headers, json=payload)
                r.raise_for_status()
                return r.json()
        except McpError:
            raise
        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to add note: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred: {e}"))

    async def list_notes(self) -> dict:
        """Lists all notes from Google Keep via the API."""
        try:
            access_token, _ = await self._get_valid_access_token()
            headers = {"Authorization": f"Bearer {access_token}"}

            async with httpx.AsyncClient() as client:
                r = await client.get(f"{GOOGLE_KEEP_API}/notes", headers=headers)
                r.raise_for_status()
                return r.json()
        except McpError:
            raise
        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to list notes: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred: {e}"))

    async def delete_note(self, note_id: str) -> dict:
        """Deletes a specific note from Google Keep via the API."""
        try:
            access_token, _ = await self._get_valid_access_token()
            headers = {"Authorization": f"Bearer {access_token}"}

            async with httpx.AsyncClient() as client:
                r = await client.delete(f"{GOOGLE_KEEP_API}/notes/{note_id}", headers=headers)
                r.raise_for_status()
                return {"status": "deleted" if r.status_code == 200 else r.text}
        except McpError:
            raise
        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to delete note: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred: {e}"))

# --- Create manager instance ---
puchkeep_manager = PuchKeepManager()

# --- FastMCP Server ---
# Initializes the FastMCP server with a title and the custom auth provider.
mcp = FastMCP(
    "PuchKeep MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
# A simple validation tool for the Puch platform.
@mcp.tool
async def validate() -> str:
    """Validation tool for Puch."""
    return MY_NUMBER

# --- Tool: generate_keep_auth_url ---
@mcp.tool
async def generate_keep_auth_url() -> str:
    """
    Generates the Google OAuth 2.0 authorization URL for Google Keep.
    The user must visit this URL, authorize the app, and then copy the 'code'
    parameter from the URL they are redirected to.
    """
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_KEEP_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPE),  # FIX: Join the list of scopes into a single string
        "access_type": "offline",
        "prompt": "consent"
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(url)
    return f"Copy and open this URL in your browser to authorize Google Keep:\n{url}\n\nAfter authorizing, copy the 'code' from the redirected URL and use the 'complete_keep_signup' tool."

# --- Tool: complete_keep_signup ---
@mcp.tool
async def complete_keep_signup(
    email: Annotated[EmailStr, Field(description="Your Google account email address associated with Google Keep.")],
    auth_code: Annotated[str, Field(description="The authorization code obtained from the redirect URL after authorizing Google Keep.")]
) -> str:
    """
    Exchanges the authorization code for an access token and a refresh token for Google Keep,
    then signs up and logs in the user in a single step.
    """
    url = TOKEN_URL
    payload = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": auth_code,
        "redirect_uri": GOOGLE_KEEP_REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, data=payload)
            res.raise_for_status()
            tokens = res.json()
            access_token = tokens["access_token"]
            refresh_token = tokens.get("refresh_token")

            if not refresh_token:
                print("Warning: No refresh token received. This might require re-authentication later.")

            upsert_result = puchkeep_manager._upsert_user_entry(
                provider="google_keep",
                email=email,
                access_token=access_token,
                refresh_token=refresh_token or ""
            )
            login_result = puchkeep_manager.login(email, access_token)

            return f"{upsert_result}\n{login_result}\nGoogle Keep signup and login complete for **{email}**. You can now use Google Keep tools!"
    except httpx.HTTPStatusError as e:
        error_message = f"Failed to exchange authorization code for Google Keep: {e.response.text}"
        print(error_message)
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=error_message))
    except Exception as e:
        error_message = f"An unexpected error occurred during Google Keep signup: {e}"
        print(error_message)
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=error_message))

# --- Tool: logout_keep ---
@mcp.tool
async def logout_keep() -> str:
    """Logs out the current user from Google Keep."""
    return puchkeep_manager.logout()

# --- Tool: add_google_keep_note ---
@mcp.tool
async def add_google_keep_note(
    title: Annotated[str, Field(description="The title of the new Google Keep note.")],
    content: Annotated[str, Field(description="The text content of the new Google Keep note.")]
) -> dict:
    """
    Adds a new note to Google Keep using the currently logged-in user's credentials.
    """
    return await puchkeep_manager.add_note(title, content)

# --- Tool: list_google_keep_notes ---
@mcp.tool
async def list_google_keep_notes() -> dict:
    """
    Lists all notes from Google Keep using the currently logged-in user's credentials.
    """
    return await puchkeep_manager.list_notes()

# --- Tool: delete_google_keep_note ---
@mcp.tool
async def delete_google_keep_note(
    note_id: Annotated[str, Field(description="The ID of the Google Keep note to delete. You can get this from 'list_google_keep_notes'.")]
) -> dict:
    """
    Deletes a specific note from Google Keep using the currently logged-in user's credentials.
    """
    return await puchkeep_manager.delete_note(note_id)

# --- Tool: help_menu ---
@mcp.tool
async def help_menu() -> str:
    """
    Shows the help menu with all available commands and emojis for Google Keep.
    """
    return (
        "ℹ️ **Google Keep Help Menu**\n"
        "🔗 - Generate Google Keep OAuth authorization URL (`generate_keep_auth_url`)\n"
        "✅ - Complete signup with authorization code for Google Keep (`complete_keep_signup <your_email> <auth_code>`)\n"
        "📝 - Add a new Google Keep note (`add_google_keep_note <title> <content>`)\n"
        "📜 - List all Google Keep notes (`list_google_keep_notes`)\n"
        "🗑️ - Delete a Google Keep note (`delete_google_keep_note <note_id>`)\n"
        "🚪 - Log out from Google Keep account (`logout_keep`)\n"
    )
    
# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchKeep MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
