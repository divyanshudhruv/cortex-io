from textwrap import dedent
# --- Tool: about ---

import asyncio
import os
import sys
from typing import Annotated, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AccessToken
from pydantic import Field
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from dataclasses import dataclass

# --- Load environment variables ---
load_dotenv()
TOKEN = os.getenv("AUTH_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing one or more required environment variables (AUTH_TOKEN, SUPABASE_URL, SUPABASE_KEY).")

# Initialize Supabase client
supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions()
)

# --- Auth Provider ---
class SimpleJWTAuthProvider(JWTVerifier):
    """
    A simple JWT verifier that validates against a pre-configured token.
    This is for development and testing purposes.
    """
    def __init__(self, token: str):
        # Provide a dummy public key to satisfy the parent class requirement
        dummy_public_key = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnzQw==\n-----END PUBLIC KEY-----"
        super().__init__(public_key=dummy_public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if token == self.token:
            return AccessToken(token=token, client_id="puch-client", scopes=["*"], expires_at=None)
        return None

session = {
    "username": None,
}

# --- MCP Server ---
mcp = FastMCP("PuchChat MCP Server", auth=SimpleJWTAuthProvider(TOKEN))

# --- Supabase Realtime Listener ---
@dataclass
class ChatMessage:
    id: int
    created_at: str
    username: str
    message: str

async def listen_for_new_messages():
    """
    Listens for new chat messages using the Supabase Realtime listener.
    """
    channel = supabase.channel('public:puchchat').on(
        'postgres_changes', 
        {'event': 'INSERT', 'schema': 'public', 'table': 'puchchat'}, 
        lambda payload: print(f"New message received: {payload['new']['username']}: {payload['new']['message']}")
    )
    await channel.subscribe()
    print("Supabase Realtime listener started. Waiting for new messages...")

    # Keep the asyncio event loop running
    while True:
        await asyncio.sleep(3600) # Sleep for a long time to keep the loop alive

# --- Tools --- 
@mcp.tool
async def connect(username: Annotated[str, Field(description="Username to connect")]) -> str:
    """
    Connects a user to the chat, creating them if they don't exist.
    """
    username = username.strip().lower()
    
    # Use upsert to simplify logic: insert if not exists, update if exists
    response = await supabase.table("puchchat_users").upsert({
        "username": username,
        "is_connected": True,
    }).execute()
    
    # The 'upsert' method will return a proper error if something fails
    if response.error:
        return f"❌ Error connecting: {response.error.message}"

    session["username"] = username
    return f"✅ Connected as '{username}'."

@mcp.tool
async def disconnect() -> str:
    """
    Disconnects a user from the chat.
    """
    username = session.get("username")
    if not username:
        return "⚠️ You are not connected."
    
    response = await supabase.table("puchchat_users").update({
        "is_connected": False,
    }).eq("username", username).execute()
    
    if response.error:
        return f"❌ Error disconnecting '{username}': {response.error.message}"
    
    session["username"] = None
    return f"🚪 User '{username}' disconnected from chat."

@mcp.tool
async def send(message: Annotated[str, Field(description="Message to send")]) -> str:
    """
    Sends a message to the chat.
    """
    username = session.get("username")
    if not username:
        return "⚠️ You must /connect before sending messages."
    
    response = await supabase.table("puchchat").insert({
        "username": username,
        "message": message.strip(),
    }).execute()
    
    if response.error:
        return f"❌ Error sending message: {response.error.message}"
    
    return f"📨 Message sent: {message.strip()}"

@mcp.tool
async def fetch_history() -> str:
    """
    Fetches the 10 most recent messages from the chat.
    """
    username = session.get("username")
    if not username:
        return "⚠️ You must /connect before fetching messages."
    
    response = await supabase.table("puchchat").select("*").order("created_at", desc=True).limit(10).execute()
    messages = response.data or []
    
    if response.error:
        return f"❌ Error fetching messages: {response.error.message}"
    
    if not messages:
        return "🕒 No messages found."
    
    # Reverse the messages to display them in chronological order
    formatted = "\n".join([
        f"[{m['created_at'].replace('T',' ').split('+')[0]}] {m['username']}: {m['message']}"
        for m in reversed(messages)
    ])
    return f"💬 Recent messages:\n{formatted}"

@mcp.tool
async def help() -> str:
    """
    Displays a list of available commands.
    """
    return (
        "Commands:\n"
        "/connect <username> - Connect to chat\n"
        "/disconnect - Disconnect from chat\n"
        "/send <message> - Send a message (only when connected)\n"
        "/fetch_history - Fetch the 10 most recent messages\n"
        "/connected_users - Show who is connected\n"
        "/help - Show this help"
    )

@mcp.tool
async def connected_users() -> str:
    """
    Fetches a list of all currently connected users.
    """
    response = await supabase.table("puchchat_users").select("username").eq("is_connected", True).execute()
    
    if response.error:
        return f"❌ Error fetching connected users: {response.error.message}"
    
    users = [u["username"] for u in (response.data or [])]
    count = len(users)
    
    if count == 0:
        return "No users are currently connected."
    
    return f"Connected users ({count}):\n" + "\n".join(users)
@mcp.tool
async def about() -> dict[str, str]:
    server_name = "PuchChat MCP"
    server_description = dedent("""
    PuchChat is a real-time chat server for WhatsApp and Puch AI. It allows users to connect, send, and fetch messages, see connected users, and interact in real time, all with emoji-rich feedback and Supabase backend.
    """)
    return {
        "name": server_name,
        "description": server_description
    }
# --- Run Server ---
async def main():
    # Start the Supabase listener in a background task
    asyncio.create_task(listen_for_new_messages())
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"An unhandled exception occurred: {e}", file=sys.stderr)
        sys.exit(1)
