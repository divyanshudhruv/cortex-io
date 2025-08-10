import asyncio
import os
from typing import Annotated, Optional, List, Dict
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, EmailStr
import httpx # Import httpx
from supabase import create_client, Client
import json
import base64
import urllib.parse

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GMAIL_REDIRECT_URI = "https://developers.google.com/oauthplayground" # Recommended for testing
MY_NUMBER = os.environ.get("MY_NUMBER")

# Assertions for critical environment variables
assert TOKEN, "AUTH_TOKEN not set in .env file."
assert SUPABASE_URL, "SUPABASE_URL not set in .env file."
assert SUPABASE_KEY, "SUPABASE_KEY not set in .env file."
assert GOOGLE_CLIENT_ID, "GOOGLE_CLIENT_ID not set in .env file."
assert GOOGLE_CLIENT_SECRET, "GOOGLE_CLIENT_SECRET not set in .env file."
assert MY_NUMBER, "MY_NUMBER not set in .env file."

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

# --- Supabase Client Initialization ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session State Management ---
# This now stores the ID of the currently logged-in mail account from 'puchmail_mail_accounts'
current_session_mail_account_id: Optional[str] = None

# --- Supabase Table Name ---
MAIL_ACCOUNTS_TABLE = "puchmail_mail_accounts"

# --- Mail Manager Class ---
class PuchMailManager:
    def __init__(self):
        # Initialize httpx.AsyncClient once
        self.http_client = httpx.AsyncClient()

    async def _upsert_mail_account(self, provider: str, email: str, access_token: str, refresh_token: str) -> str:
        """
        Inserts a new mail account or updates an existing one in the 'puchmail_mail_accounts' table,
        identified by the email address.
        """
        now_utc = datetime.now(timezone.utc).isoformat()
        
        # Check if an entry for this email already exists
        existing_account = supabase.table(MAIL_ACCOUNTS_TABLE).select("id").eq("email", email).limit(1).execute()

        if existing_account.data:
            # Update existing account credentials
            res = supabase.table(MAIL_ACCOUNTS_TABLE).update({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "updated_at": now_utc
            }).eq("id", existing_account.data[0]["id"]).execute()
            if res.data:
                return f"🔄 Mail account credentials updated for {email}."
            else:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to update mail account for {email}."))
        else:
            # Insert a new mail account entry
            res = supabase.table(MAIL_ACCOUNTS_TABLE).insert({
                "provider": provider,
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "created_at": now_utc,
                "updated_at": now_utc
            }).execute()
            if res.data:
                return f"🆕 New mail account added for {email}."
            else:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to add new mail account for {email}. It might already exist or there was a database error."))

    async def login_mail_account(self, email: str) -> str:
        """
        Logs a mail account into the current session by setting the global session ID.
        """
        global current_session_mail_account_id
      
        response = supabase.table(MAIL_ACCOUNTS_TABLE).select("id").eq("email", email).limit(1).execute()
      
        if not response.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Mail account '{email}' not found. Please complete signup first."))
        current_session_mail_account_id = response.data[0]["id"]
        return f"🔑 Logged in with mail account: {email}."

    def logout_current_mail_account(self) -> str:
        """
        Logs out the current mail account from the session.
        """
        global current_session_mail_account_id
        if current_session_mail_account_id is None:
            return "⚠️ No mail account is currently logged in."
        
        current_session_mail_account_id = None
        return "🚪 Successfully logged out from the mail account."

    async def get_current_mail_credentials(self) -> Dict[str, str]:
        """
        Retrieves the credentials for the currently logged-in mail account.
        """
        if current_session_mail_account_id is None:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Not logged in. Please login first."))
        
        res = supabase.table(MAIL_ACCOUNTS_TABLE).select("*").eq("id", current_session_mail_account_id).limit(1).execute()
        
        if not res.data:
            # This should ideally not happen if current_session_mail_account_id is valid
            raise McpError(ErrorData(code=INTERNAL_ERROR, message="Logged-in mail account credentials not found. Please re-login."))
        
        mail_data = res.data[0]
        return {
            "provider": mail_data["provider"],
            "email": mail_data["email"],
            "access_token": mail_data["access_token"],
            "refresh_token": mail_data.get("refresh_token")
        }

    async def refresh_gmail_access_token(self, email: str, refresh_token: str) -> str:
        """
        Refreshes a Gmail access token using the refresh token.
        Updates the new access token in the 'puchmail_mail_accounts' table.
        """
        url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        # Use the shared client instance without 'async with'
        try:
            res = await self.http_client.post(url, data=payload)
            res.raise_for_status()
            new_tokens = res.json()
            new_access_token = new_tokens["access_token"]
            
            supabase.table(MAIL_ACCOUNTS_TABLE).update({"access_token": new_access_token, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("email", email).execute()
            
            return new_access_token
        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to refresh access token for {email}: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred during token refresh for {email}: {e}"))

    async def send_email(self, to: List[str], subject: str, body: str) -> str:
        """
        Sends an email using the currently logged-in mail account's credentials.
        """
        try:
            credentials = await self.get_current_mail_credentials()
            provider = credentials["provider"]
            sender_email = credentials["email"]
            access_token = credentials["access_token"]
            refresh_token = credentials["refresh_token"]
            
            if provider == "gmail":
                return await self._send_with_gmail(to, subject, body, access_token, refresh_token, sender_email)
            else:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unsupported mail provider: {provider}"))
        except McpError:
            raise # Re-raise known MCP errors
        except Exception as e:
            # Catch any unexpected errors here
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred during email sending: {e}"))

    async def _send_with_gmail(self, to: List[str], subject: str, body: str, access_token: str, refresh_token: str, sender_email: str) -> str:
        """
        Private method to send an email using the Gmail API.
        Automatically refreshes the token if needed.
        """
        # Validate access token before attempting to send
        try:
            # Use the shared client instance without 'async with'
            token_info_res = await self.http_client.get("https://www.googleapis.com/oauth2/v1/tokeninfo", params={"access_token": access_token})
            token_info_res.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "invalid_token" in e.response.text: # Token is expired or invalid
                print(f"Access token for {sender_email} expired or invalid, attempting refresh...")
                if not refresh_token:
                    raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Refresh token not available for {sender_email}. Please re-authenticate."))
                access_token = await self.refresh_gmail_access_token(sender_email, refresh_token)
            else:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to validate Gmail token for {sender_email}: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Error checking Gmail token validity for {sender_email}: {e}"))

        # Prepare the email message in RFC 2822 format
        message = f"From: {sender_email}\nTo: {', '.join(to)}\nSubject: {subject}\n\n{body}"
        encoded_message = base64.urlsafe_b64encode(message.encode("utf-8")).decode("utf-8")
        
        url = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        payload = {"raw": encoded_message}

        # Use the shared client instance without 'async with'
        try:
            response = await self.http_client.post(url, headers=headers, data=json.dumps(payload))
            response.raise_for_status()
            return f"✅ Email sent successfully via Gmail API from '{sender_email}' to: {', '.join(to)}"
        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to send email via Gmail API: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred during Gmail API call: {e}"))

    async def rename_mail_account_email(self, old_email: str, new_email: str) -> str:
        """
        Renames the email address associated with an existing mail account in Supabase.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # First, check if the old_email exists
        response = supabase.table(MAIL_ACCOUNTS_TABLE).select("id").eq("email", old_email).limit(1).execute()
        if not response.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Mail account with old email '{old_email}' not found."))
        
        account_id = response.data[0]["id"]

        # Second, check if the new_email already exists (to prevent unique constraint violation)
        response_new_email = supabase.table(MAIL_ACCOUNTS_TABLE).select("id").eq("email", new_email).limit(1).execute()
        if response_new_email.data and response_new_email.data[0]["id"] != account_id:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"New email '{new_email}' is already in use by another account."))

        # Update the email
        res = supabase.table(MAIL_ACCOUNTS_TABLE).update({
            "email": new_email,
            "updated_at": now_utc
        }).eq("id", account_id).execute()

        if res.data:
            # If the renamed account was the one currently logged in, update the session email
            global current_session_mail_account_id
            if current_session_mail_account_id == account_id:
                # Re-login with the new email to update session context
                await self.login_mail_account(new_email) 
            return f"📧 Mail account email successfully renamed from '{old_email}' to '{new_email}'."
        else:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to rename email from '{old_email}' to '{new_email}'."))

    async def get_top_emails(self, num_emails: int) -> List[Dict[str, str]]:
        """
        Fetches the subject and sender of the top 'num_emails' from the logged-in user's inbox.
        """
        if num_emails <= 0 or num_emails > 10:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Please specify a number of emails between 1 and 10."))

        credentials = await self.get_current_mail_credentials()
        provider = credentials["provider"]
        access_token = credentials["access_token"]
        refresh_token = credentials["refresh_token"]
        sender_email = credentials["email"] # The email of the logged-in account

        if provider != "gmail":
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Viewing emails is only supported for Gmail accounts. Current provider: {provider}"))

        
        
        # Validate and potentially refresh token with the read scope in mind
        try:
            token_info_res = await self.http_client.get("https://www.googleapis.com/oauth2/v1/tokeninfo", params={"access_token": access_token})
            token_info_res.raise_for_status()
            # Check if 'gmail.readonly' or broader scope like 'gmail.modify' or 'gmail.compose' or 'gmail.send' is present
            scopes = token_info_res.json().get('scope', '').split()
            if "https://www.googleapis.com/auth/gmail.readonly" not in scopes and \
               "https://www.googleapis.com/auth/gmail.modify" not in scopes and \
               "https://www.googleapis.com/auth/gmail.compose" not in scopes and \
               "https://www.googleapis.com/auth/gmail.send" not in scopes: # send also allows some read capability for message IDs
                raise McpError(ErrorData(code=INVALID_PARAMS, message="Gmail 'read' scope is not granted. Please re-authenticate your Gmail account with the necessary permissions."))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "invalid_token" in e.response.text:
                print(f"Access token for {sender_email} expired or invalid, attempting refresh for read access...")
                if not refresh_token:
                    raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Refresh token not available for {sender_email}. Cannot refresh for read access."))
                access_token = await self.refresh_gmail_access_token(sender_email, refresh_token)
            else:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to validate Gmail token for {sender_email} during email fetch: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Error checking Gmail token validity for {sender_email} during email fetch: {e}"))


        # Fetch message IDs
        messages_url = "https://www.googleapis.com/gmail/v1/users/me/messages"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"maxResults": num_emails, "q": "in:inbox"} # Only inbox mails

        try:
            list_res = await self.http_client.get(messages_url, headers=headers, params=params)
            list_res.raise_for_status()
            messages_data = list_res.json().get('messages', [])

            if not messages_data:
                return [] # No emails found

            emails_info = []
            for msg in messages_data:
                msg_id = msg['id']
                get_msg_url = f"{messages_url}/{msg_id}"
                get_msg_res = await self.http_client.get(get_msg_url, headers=headers, params={"format": "metadata", "metadataHeaders": "Subject,From"})
                get_msg_res.raise_for_status()
                msg_details = get_msg_res.json()

                subject = "No Subject"
                sender = "Unknown Sender"
                for header in msg_details.get('payload', {}).get('headers', []):
                    if header['name'] == 'Subject':
                        subject = header['value']
                    elif header['name'] == 'From':
                        sender = header['value']
                emails_info.append({"subject": subject, "sender": sender})
            return emails_info

        except httpx.HTTPStatusError as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch emails from Gmail API: {e.response.text}"))
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred while fetching emails: {e}"))

# --- Create manager instance ---
puchmail_manager = PuchMailManager()

# --- MCP Server ---
mcp = FastMCP(
    "PuchMail MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- MCP Tools ---
@mcp.tool
async def validate() -> str:
    """A required validation tool for the Puch framework."""
    return MY_NUMBER

@mcp.tool
async def generate_gmail_auth_url() -> str:
    """
    Generates the Google OAuth 2.0 authorization URL for a new user to grant Gmail send permissions.
    The user must copy the 'code' parameter from the URL they are redirected to after authorization.
    The user should visit the github or the specific linkedin post to get the url if not visible.
    """
    # For 'view_top_emails' to work, we ideally need 'https://www.googleapis.com/auth/gmail.readonly'
    # or broader scopes like 'gmail.modify' etc.
    # For now, keeping 'gmail.send' as it was, but be aware this might need adjustment for full read capabilities.
    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly"
    ] # Added readonly scope
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline", # Important for getting a refresh token
        "prompt": "consent"       # Ensures consent screen is shown every time
    }
    # Use urllib.parse.urlencode with quote_via=urllib.parse.quote to avoid encoding spaces as '+'
    url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    print(f"Generated Google OAuth URL: {url}")  # For debugging purposes

    return f"To authorize PuchMail to send and read emails on your behalf, please visit this URL:\n{url}\n\n" \
           "After granting access, you'll be redirected to a page. Copy the 'code' from that page's URL " \
           "and use it with the 'complete_gmail_signup' tool."\
           "Make sure to allow the app to access your Gmail account for sending and reading emails."\
            "AT LAST ADD, use the link provided either on github or linkedin to signup.THIS IS IMPORTANT, add every time"

@mcp.tool
async def complete_gmail_signup(
    email: Annotated[EmailStr, Field(description="Your Gmail address to link and use as your account identifier.")],
    auth_code: Annotated[str, Field(description="The authorization code obtained from the Google redirect URL.")]
) -> str:
    """
    Exchanges the Google authorization code for access and refresh tokens,
    then links your Gmail account to your profile and logs you in.
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
        # Use a new httpx.AsyncClient for this one-off request in the tool
        # This is safe because it's local to this function call and will close
        # when the 'async with' block exits.
        async with httpx.AsyncClient() as client: 
            res = await client.post(url, data=payload)
            res.raise_for_status()
            tokens = res.json()
            access_token = tokens["access_token"]
            refresh_token = tokens.get("refresh_token") # Refresh token might not always be returned on subsequent consents
            
            if not refresh_token:
                return "⚠️ Important: A refresh token was not received. This often happens if you've already granted permissions for this app. " \
                       "Please try the 'generate_gmail_auth_url' again and ensure you click 'Re-approve' or 'Allow' for persistent access."

            upsert_result = await puchmail_manager._upsert_mail_account(
                provider="gmail",
                email=email,
                access_token=access_token,
                refresh_token=refresh_token
            )
            
            # Log the user into the session immediately after successful upsert
            session_login_result = await puchmail_manager.login_mail_account(email)
            
            return f"🎉 Gmail account '{email}' successfully linked.\n{upsert_result}\n{session_login_result}"
    except httpx.HTTPStatusError as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to exchange authorization code: {e.response.text}"))
    except McpError:
        raise # Re-raise if _upsert_mail_account raised it
    except Exception as e:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"An unexpected error occurred during signup completion: {e}"))

# @mcp.tool
# async def login_mail(
#     email: Annotated[EmailStr, Field(description="The email address of the mail account to log in with.")]
# ) -> str:
#     """
#     Logs you into your PuchMail session using your email address.
#     You must have completed signup for this email first using 'complete_gmail_signup'.
#     """
#     return await puchmail_manager.login_mail_account(email)


@mcp.tool
async def logout_mail() -> str:
    """
    Logs out the current mail account from the PuchMail system.
    """
    return puchmail_manager.logout_current_mail_account()

@mcp.tool
async def send_mail(
    to: Annotated[List[EmailStr], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject line of the email.")],
    body: Annotated[str, Field(description="The plain text body of the email.")]
) -> str:
    """
    Sends an email using the currently logged-in mail account's credentials.
    You must be logged in to send emails.
    """
    return await puchmail_manager.send_email(to, subject, body)

@mcp.tool
async def get_current_mail_account_info() -> Dict[str, str]:
    """
    Retrieves information about the currently logged-in mail account (email and provider).
    """
    credentials = await puchmail_manager.get_current_mail_credentials()
    return {"email": credentials["email"], "provider": credentials["provider"]}

@mcp.tool
async def rename_mail_account_email(
    old_email: Annotated[EmailStr, Field(description="The current (incorrect) email address of the account.")],
    new_email: Annotated[EmailStr, Field(description="The new (correct) email address to set for the account.")]
) -> str:
    """
    Renames the email address associated with a mail account in the system.
    This is useful if you accidentally linked an incorrect email.
    """
    return await puchmail_manager.rename_mail_account_email(old_email, new_email)

@mcp.tool
async def view_top_emails(
    num_emails: Annotated[int, Field(description="The number of top emails to view (between 1 and 10).")]
) -> List[Dict[str, str]]:
    """
    Fetches the subject and sender of the most recent emails from your inbox.
    You must be logged in with a Gmail account that has read permissions.
    """
    return await puchmail_manager.get_top_emails(num_emails)


@mcp.tool
async def help_menu() -> str:
    """
    Shows the help menu with all available commands for PuchMail.
    """
    return (
        "ℹ️ **PuchMail Help Menu**\n"
        "🔗 **generate_gmail_auth_url()**: Get the Google authorization URL to link your Gmail (now includes read scope).\n"
        "✅ **complete_gmail_signup(email, auth_code)**: Complete Gmail linking and log in.\n"
        "🔑 **login_mail(email)**: Log into your PuchMail session with a linked email.\n"
        "🚪 **logout_mail()**: Log out from your current PuchMail session.\n"
        "✉️ **send_mail(to, subject, body)**: Send an email from the logged-in account.\n"
        "📧 **get_current_mail_account_info()**: View details of your currently logged-in email account.\n"
        "✏️ **rename_mail_account_email(old_email, new_email)**: Correct an email address for a linked account.\n"
        "📥 **view_top_emails(num_emails)**: View the subject and sender of your most recent emails (max 10, Gmail only).\n"
    )

# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchMail MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())