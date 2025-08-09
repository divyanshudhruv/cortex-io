import asyncio
from typing import Annotated, Optional, List
import os
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, Field

import uuid
from supabase import create_client, Client

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert SUPABASE_URL and SUPABASE_KEY, "Please set SUPABASE_URL and SUPABASE_KEY in your .env file"

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
                client_id="puchkeep-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: Optional[str] = None
    structure: str

# --- Supabase connection ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session state ---
current_user: dict = {"username": None, "user_id": None}
# --- PuchKeep Manager ---
class PuchKeepManager:
    def signup(self, username: str, password: str) -> str:
        existing = supabase.table("users").select("*").eq("username", username).execute()
        if existing.data:
            return "🛑 Username already exists. Please choose another one."
        res = supabase.table("users").insert({"username": username, "password": password}).execute()
        if res.data:
            return f"🆕 Signup successful! Welcome, {username}."
        return "❌ Failed to create account. Please try again."

    def login(self, username: str, password: str) -> str:
        global current_user
        user = supabase.table("users").select("*").eq("username", username).eq("password", password).execute()
        if not user.data:
            return "🚫 Invalid username or password. Please try again."
        current_user["username"] = username
        current_user["user_id"] = user.data[0]["id"]
        return f"🔑 Logged in as {username}. Welcome back!"

    def logout(self) -> str:
        global current_user
        if not current_user["username"]:
            return "⚠️ You are not logged in."
        name = current_user["username"]
        current_user["username"] = None
        current_user["user_id"] = None
        return f"🚪 Logged out from {name}. See you next time!"

    def add_memory(self, memory: str, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to save a memory."
        existing = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if existing.data:
            return f"⚠️ Memory name '{name_of_memory}' already exists. Please use a different name."
        memory_id = str(uuid.uuid4())
        res = supabase.table("puchkeep").insert({
            "id": memory_id,
            "user_id": current_user["user_id"],
            "memory": memory,
            "name_of_memory": name_of_memory
        }).execute()
        if res.data:
            return f"💾 Memory saved!\nStatus: ✅\nMemory Name: '{name_of_memory}'\nMessage: Your memory has been stored successfully."
        return "❌ Failed to save memory. Please try again."

    def list_memories(self) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to view your memories."
        res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).execute()
        if not res.data:
            return "📋 No memories saved yet. Start by adding a new one!"
        return (
            "📋 **Your Memories**\n" +
            "\n".join(
                f"• 📝 {m.get('name_of_memory', '(no name)')}: {m['memory']}"
                for m in res.data
            )
        )

    def get_memory(self, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to retrieve a memory."
        res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if not res.data:
            return f"🔍 **Memory Retrieved**\nMemory Name: {name_of_memory}\nStatus: ❌\nMessage: Memory not found."
        m = res.data[0]
        return (
            f"🔍 **Memory Retrieved**\n"
            f"Memory Name: {name_of_memory}\n"
            f"Memory: {m['memory']}\n"
            f"Status: ✅\n"
            f"Message: Memory found and displayed above."
        )

    def get_multiple_memories(self, memory_names: List[str]) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to use multiple memories."
        found_memories = []
        not_found = []
        for name in memory_names:
            res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name).execute()
            if res.data:
                m = res.data[0]
                found_memories.append(f"• 📝 {name}: {m['memory']}")
            else:
                not_found.append(name)
        result = ""
        if found_memories:
            result += "📚 **Multiple Memories**\n" + "\n".join(found_memories)
        if not_found:
            if result:
                result += "\n"
            result += "Not found: " + ", ".join(not_found)
        return result if result else "📚 No memories found for the given names."

    def delete_memory(self, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to delete a memory."
        res = supabase.table("puchkeep").delete().eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if res.data:
            return f"❌ **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ✅\nMessage: Memory deleted successfully."
        return f"❌ **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ❌\nMessage: Memory not found."

    def rename_memory(self, old_name: str, new_name: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to rename a memory."
        # Check if new name already exists
        existing = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", new_name).execute()
        if existing.data:
            return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory name '{new_name}' already exists."
        # Update the memory name
        res = supabase.table("puchkeep").update({"name_of_memory": new_name}).eq("user_id", current_user["user_id"]).eq("name_of_memory", old_name).execute()
        if res.data:
            return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ✅\nMessage: Memory renamed successfully."
        return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory not found."

# --- Create manager instance ---
puchkeep_manager = PuchKeepManager()

# --- MCP Server ---
mcp = FastMCP(
    "PuchKeep MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: signup ---
SignupDesc = RichToolDescription(
    description="Sign up for a new account with a username and password. Show a confirmation message with an emoji indicating success or failure.",
    use_when="Use when a user wants to create a new account and expects a confirmation message with an emoji.",
    side_effects="Creates a new user in the database and returns a message with an emoji showing the result.",
    structure="🆕 **Signup Result**\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=SignupDesc.model_dump_json())
async def signup(
    username: Annotated[str, Field(..., description="Username")],
    password: Annotated[str, Field(..., description="Password")]
) -> str:
    return puchkeep_manager.signup(username, password)

# --- Tool: login ---
LoginDesc = RichToolDescription(
    description="Log in to your account using username and password. Show a message with an emoji indicating login success or failure.",
    use_when="Use when a user wants to log in and expects a confirmation message with an emoji.",
    side_effects="Sets the current session user and returns a message with an emoji showing the result.",
    structure="🔑 **Login Result**\nStatus: <success/failure emoji>\nMessage: <login confirmation message>\n"
)
@mcp.tool(description=LoginDesc.model_dump_json())
async def login(
    username: Annotated[str, Field(..., description="Username")],
    password: Annotated[str, Field(..., description="Password")]
) -> str:
    return puchkeep_manager.login(username, password)

# --- Tool: logout ---
LogoutDesc = RichToolDescription(
    description="Log out from your account. Show a confirmation message with an emoji.",
    use_when="Use when a user wants to log out and expects a confirmation message with an emoji.",
    side_effects="Clears the current session user and returns a message with an emoji.",
    structure="🚪 **Logout Result**\nStatus: <success/failure emoji>\nMessage: <logout confirmation message>\n"
)
@mcp.tool(description=LogoutDesc.model_dump_json())
async def logout() -> str:
    return puchkeep_manager.logout()

# --- Tool: save_memory ---
SaveMemoryDesc = RichToolDescription(
    description="Save a new memory with a unique name. Show a confirmation message with an emoji and the name of the saved memory.",
    use_when="Use when a user wants to store a new memory and expects a confirmation message with an emoji.",
    side_effects="Adds a new memory to the user's collection and returns a message with an emoji.",
    structure="💾 **Memory Saved**\nStatus: <success/failure emoji>\nMemory Name: <name_of_memory>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=SaveMemoryDesc.model_dump_json())
async def save_memory(
    memory: Annotated[str, Field(description="Memory text")],
    name_of_memory: Annotated[str, Field(description="Unique name for this memory")]
) -> str:
    return puchkeep_manager.add_memory(memory, name_of_memory)

# --- Tool: list_memories ---
ListMemoriesDesc = RichToolDescription(
    description="List all your memories as bulleted points, each with an emoji. Show a message with the actual list or a note if empty.",
    use_when="Use when a user wants to see all their saved memories and expects a list with emojis.",
    side_effects="Returns a list of all memories saved by the user, each with an emoji.",
    structure="📋 **Your Memories**\n<list of memories, each as: '• 📝 <name>: <memory>' or a message if empty>\n"
)
@mcp.tool(description=ListMemoriesDesc.model_dump_json())
async def list_memories() -> str:
    return puchkeep_manager.list_memories()

# --- Tool: get_memory ---
GetMemoryDesc = RichToolDescription(
    description="Get a memory by its name. Show the memory content with an emoji and a confirmation message.",
    use_when="Use when a user wants to retrieve a specific memory and expects the memory text with an emoji.",
    side_effects="Returns the memory text if found, with an emoji and confirmation.",
    structure="🔍 **Memory Retrieved**\nMemory Name: <name_of_memory>\nMemory: <memory>\nStatus: <found/not found emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=GetMemoryDesc.model_dump_json())
async def get_memory(
    name_of_memory: Annotated[str, Field(description="Memory name")]
) -> str:
    return puchkeep_manager.get_memory(name_of_memory)

# --- Tool: delete_memory ---
DeleteMemoryDesc = RichToolDescription(
    description="Delete a memory by its name. This is irreversible. Show a confirmation message with an emoji indicating deletion.",
    use_when="Use when a user wants to remove a memory and expects a confirmation message with an emoji.",
    side_effects="Deletes the memory from the user's collection and returns a message with an emoji.",
    structure="❌ **Memory Deleted**\nMemory Name: <name_of_memory>\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=DeleteMemoryDesc.model_dump_json())
async def delete_memory(
    name_of_memory: Annotated[str, Field(description="Memory name")]
) -> str:
    return puchkeep_manager.delete_memory(name_of_memory)

# --- Tool: rename_memory ---
RenameMemoryDesc = RichToolDescription(
    description="Rename a memory. Show a confirmation message with an emoji and the old and new names.",
    use_when="Use when a user wants to change the name of a memory and expects a confirmation message with an emoji.",
    side_effects="Updates the memory's name in the database and returns a message with an emoji.",
    structure="✏️ **Memory Renamed**\nOld Name: <old_name>\nNew Name: <new_name>\nStatus: <success/failure emoji>\nMessage: <confirmation message>\n"
)
@mcp.tool(description=RenameMemoryDesc.model_dump_json())
async def rename_memory(
    old_name: Annotated[str, Field(description="Current memory name")],
    new_name: Annotated[str, Field(description="New memory name")]
) -> str:
    return puchkeep_manager.rename_memory(old_name, new_name)

# --- Tool: help ---
HelpDesc = RichToolDescription(
    description="Show the help menu with all available commands, each with an emoji. Display the help text as a message.",
    use_when="Use when a user asks for help or available commands and expects a list with emojis.",
    side_effects="Returns a help message with all commands and emojis.",
    structure="ℹ️ **Help Menu**\n<list of commands with emojis and descriptions>\n"
)
@mcp.tool(description=HelpDesc.model_dump_json())
async def puchkeep_help() -> str:
    return (
        "Commands:\n"
        "🆕 - Sign up for a new account\n"
        "🔑 - Log in to your account\n"
        "🚪 - Log out from your account\n"
        "💾 - Save a new memory\n"
        "📋 - List all your memories\n"
        "🔍 - Get a memory by its name\n"
        "❌ - Delete a memory by its name\n"
        "✏️ - Rename a memory\n"
        "📚 - Use multiple memories at once (e.g. /use memory1 memory2 memory3)"
    )

# --- Tool: use_memories ---
UseMemoriesDesc = RichToolDescription(
    description="Use multiple memories at once by providing their names. Show the retrieved memories as a list with emojis and indicate if any were not found.",
    use_when="Use when a user wants to retrieve several memories at once and expects a list with emojis and not-found notices.",
    side_effects="Returns the requested memories as a list with emojis and a message for any not found.",
    structure="📚 **Multiple Memories**\n<list of found memories: '• 📝 <name>: <memory>'>\nNot found: <comma-separated names, if any>\n"
)
@mcp.tool(description=UseMemoriesDesc.model_dump_json())
async def use_memories(
    memory_names: Annotated[List[str], Field(description="List of memory names to retrieve")]
) -> str:
    return puchkeep_manager.get_multiple_memories(memory_names)


# --- Run MCP Server ---
async def main():
    print("Starting PuchKeep MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
