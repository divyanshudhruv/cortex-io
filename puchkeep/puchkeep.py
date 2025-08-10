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
from pydantic import BaseModel, Field, AnyUrl
import httpx
import markdownify
import readabilipy
import uuid
from supabase import create_client, Client
from bs4 import BeautifulSoup # Added for Fetch class
import aiofiles # For asynchronous file operations
import shutil # For creating directories if needed

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER, "Please set MY_NUMBER in your .env file"
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
                client_id="puch-client",
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

# --- Fetch Utility Class (No changes as it's a general utility) ---
class Fetch:
    USER_AGENT = "Puch/1.0 (Autonomous)"

    @classmethod
    async def fetch_url(
        cls,
        url: str,
        user_agent: str,
        force_raw: bool = False,
    ) -> tuple[str, str]:
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
            return cls.extract_content_from_html(page_raw), ""
        return (
            page_raw,
            f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
        )

   

# --- Supabase connection ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session state ---
current_user: Dict[str, Optional[str]] = {"username": None, "user_id": None}

# --- Supabase Table Names ---
USERS_TABLE = "puchkeep_users"
MEMORIES_TABLE = "puchkeep_memories"

# --- File Storage Directory ---
STORAGE_BASE_DIR = "user_storage"
os.makedirs(STORAGE_BASE_DIR, exist_ok=True) # Ensure the base storage directory exists


# --- PuchKeep Manager ---
class PuchKeepManager:
    def signup(self, username: str, password: str) -> str:
        existing = supabase.table(USERS_TABLE).select("*").eq("username", username).execute()
        if existing.data:
            return "🛑 Username already exists. Please choose another one."
        
        res = supabase.table(USERS_TABLE).insert({"username": username, "password": password}).execute()
        if res.data:
            return f"🆕 Signup successful! Welcome, {username}."
        return "❌ Failed to create account. Please try again."

    def login(self, username: str, password: str) -> str:
        global current_user
        user = supabase.table(USERS_TABLE).select("*").eq("username", username).eq("password", password).execute()
        if not user.data:
            return "🚫 Invalid username or password. Please try again."
        
        current_user["username"] = username
        current_user["user_id"] = user.data[0]["id"]
        
        # Ensure user's storage directory exists upon login
        user_storage_path = os.path.join(STORAGE_BASE_DIR, current_user["user_id"])
        os.makedirs(user_storage_path, exist_ok=True)

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
        
        # Check for existing memory with the same name for this user
        existing = supabase.table(MEMORIES_TABLE).select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if existing.data:
            return f"⚠️ Memory name '{name_of_memory}' already exists for your account. Please use a different name."
        
        memory_id = str(uuid.uuid4())
        res = supabase.table(MEMORIES_TABLE).insert({
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
        
        res = supabase.table(MEMORIES_TABLE).select("*").eq("user_id", current_user["user_id"]).execute()
        if not res.data:
            return "📋 No memories saved yet. Start by adding a new one!"
        
        # Formatted output for the user
        memories_list = "\n".join(
            f"• 📝 {m.get('name_of_memory', '(no name)')}: {m['memory']}"
            for m in res.data
        )
        return f"📋 **Your Memories**\n{memories_list}"

    def get_memory(self, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to retrieve a memory."
        
        res = supabase.table(MEMORIES_TABLE).select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
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
        
        # Supabase allows querying with 'in' for lists
        # This is more efficient than looping and querying for each name
        response = supabase.table(MEMORIES_TABLE).select("name_of_memory, memory") \
            .eq("user_id", current_user["user_id"]) \
            .in_("name_of_memory", memory_names) \
            .execute()
            
        retrieved_map = {item["name_of_memory"]: item["memory"] for item in response.data}

        for name in memory_names:
            if name in retrieved_map:
                found_memories.append(f"• 📝 {name}: {retrieved_map[name]}")
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
        
        res = supabase.table(MEMORIES_TABLE).delete().eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        
        if res.data: # Supabase delete returns data if rows were affected
            return f"❌ **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ✅\nMessage: Memory deleted successfully."
        # If no data is returned, it means no rows were matched for deletion
        return f"❌ **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ❌\nMessage: Memory not found or you don't have permission."

    def rename_memory(self, old_name: str, new_name: str) -> str:
        if not current_user["user_id"]:
            return "🔒 Please login first to rename a memory."
        
        # Check if the new name already exists for this user
        existing = supabase.table(MEMORIES_TABLE).select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", new_name).execute()
        if existing.data:
            return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory name '{new_name}' already exists."
        
        res = supabase.table(MEMORIES_TABLE).update({"name_of_memory": new_name, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("user_id", current_user["user_id"]).eq("name_of_memory", old_name).execute()
        
        if res.data:
            return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ✅\nMessage: Memory renamed successfully."
        return f"✏️ **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory not found."

    # async def _save_list_to_file(self, user_id: str, file_name: str, content_list: List[str]) -> str:
    #     """Helper to save a list of strings to a user-specific text file."""
    #     if not user_id:
    #         return "Error: User ID is missing for file saving."

    #     user_dir = os.path.join(STORAGE_BASE_DIR, user_id)
    #     os.makedirs(user_dir, exist_ok=True) # Ensure user's directory exists

    #     file_path = os.path.join(user_dir, file_name)
        
    #     try:
    #         # Using aiofiles for asynchronous file writing
    #         async with aiofiles.open(file_path, mode='w', encoding='utf-8') as f:
    #             for item in content_list:
    #                 await f.write(item + '\n')
    #         return f"📄 File saved successfully to '{file_path}'."
    #     except Exception as e:
    #         raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to save file '{file_name}': {e}"))


# --- Create manager instance ---
puchkeep_manager = PuchKeepManager()

# --- MCP Server ---
mcp = FastMCP(
    "PuchKeep MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    """A required validation tool for the Puch framework."""
    return MY_NUMBER

# --- Tool: signup ---
SignupDesc = RichToolDescription(
    description="Sign up for a new account with a username and password. Show a confirmation message with an emoji indicating success or failure.",
    use_when="Use when a user wants to create a new account and expects a confirmation message with an emoji.",
    side_effects="Creates a new user in the database and returns a message with an emoji showing the result.",
    structure="🆕 **Signup Result**\nStatus: <success/failure emoji>\nMessage: <confirmation message>"
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
    structure="🔑 **Login Result**\nStatus: <success/failure emoji>\nMessage: <login confirmation message>"
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
    structure="🚪 **Logout Result**\nStatus: <success/failure emoji>\nMessage: <logout confirmation message>"
)
@mcp.tool(description=LogoutDesc.model_dump_json())
async def logout() -> str:
    return puchkeep_manager.logout()

# --- Tool: save_memory ---
SaveMemoryDesc = RichToolDescription(
    description="Save a new memory with a unique name. Show a confirmation message with an emoji and the name of the saved memory.",
    use_when="Use when a user wants to store a new memory and expects a confirmation message with an emoji.",
    side_effects="Adds a new memory to the user's collection and returns a message with an emoji.",
    structure="💾 **Memory Saved**\nStatus: <success/failure emoji>\nMemory Name: <name_of_memory>\nMessage: <confirmation message>"
)
@mcp.tool(description=SaveMemoryDesc.model_dump_json())
async def save_memory(
    memory: Annotated[str, Field(description="Memory text")],
    name_of_memory: Annotated[str, Field(description="Unique name for this memory")]
) -> str:
    return puchkeep_manager.add_memory(memory, name_of_memory)

# --- Tool: list_memories ---
ListMemoriesDesc = RichToolDescription(
    description="List all your memories as bullet points, each with an emoji. Show a message with the actual list or a note if empty.",
    use_when="Use when a user wants to see all their saved memories and expects a list with emojis.",
    side_effects="Returns a list of all memories saved by the user, each with an emoji and bullet points.",
    structure="📋 **Your Memories**\n<list of memories, each as: '• 📝 <name>: <memory>' or a message if empty>"
)
@mcp.tool(description=ListMemoriesDesc.model_dump_json())
async def list_memories() -> str:
    return puchkeep_manager.list_memories()

# --- Tool: get_memory ---
GetMemoryDesc = RichToolDescription(
    description="Get a memory by its name. Show the memory content with an emoji and a confirmation message.",
    use_when="Use when a user wants to retrieve a specific memory and expects the memory text with an emoji.",
    side_effects="Returns the memory text if found, with an emoji and confirmation.",
    structure="🔍 **Memory Retrieved**\nMemory Name: <name_of_memory>\nMemory: <memory>\nStatus: <found/not found emoji>\nMessage: <confirmation message>"
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
    structure="❌ **Memory Deleted**\nMemory Name: <name_of_memory>\nStatus: <success/failure emoji>\nMessage: <confirmation message>"
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
    structure="✏️ **Memory Renamed**\nOld Name: <old_name>\nNew Name: <new_name>\nStatus: <success/failure emoji>\nMessage: <confirmation message>"
)
@mcp.tool(description=RenameMemoryDesc.model_dump_json())
async def rename_memory(
    old_name: Annotated[str, Field(description="Current memory name")],
    new_name: Annotated[str, Field(description="New memory name")]
) -> str:
    return puchkeep_manager.rename_memory(old_name, new_name)

# --- Tool: use_memories ---
UseMemoriesDesc = RichToolDescription(
    description="Use multiple memories at once by providing their names. Show the retrieved memories as a list with emojis and indicate if any were not found.",
    use_when="Use when a user wants to retrieve several memories at once and expects a list with emojis and not-found notices.",
    side_effects="Returns the requested memories as a list with emojis and a message for any not found.",
    structure="📚 **Multiple Memories**\n<list of found memories: '• 📝 <name>: <memory>'>\nNot found: <comma-separated names, if any>"
)
@mcp.tool(description=UseMemoriesDesc.model_dump_json())
async def use_memories(
    memory_names: Annotated[List[str], Field(description="List of memory names to retrieve")]
) -> str:
    return puchkeep_manager.get_multiple_memories(memory_names)

# --- Tool: save_list_to_text_file ---
SaveListToFileDesc = RichToolDescription(
    description="Saves a provided list of strings into a text (.txt) file in the user's storage. Each item in the list will be saved on a new line.",
    use_when="Use when a user wants to save a list of information into a file for later retrieval or export. The output will confirm the file path.",
    side_effects="Creates or overwrites a .txt file in the user's dedicated storage directory.",
    structure="📄 **File Save Result**\nStatus: <success/failure emoji>\nMessage: <confirmation message including file path>"
)
@mcp.tool(description=SaveListToFileDesc.model_dump_json())
async def save_list_to_text_file(
    file_name: Annotated[str, Field(description="The desired name of the text file (e.g., 'my_notes.txt'). It should end with .txt")],
    content_list: Annotated[List[str], Field(description="A list of strings, where each string will be written as a new line in the file.")]
) -> str:
    if not current_user["user_id"]:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="🔒 Please login first to save files."))
    if not file_name.endswith(".txt"):
        return f"⚠️ File name '{file_name}' does not end with .txt. Please provide a valid .txt file name."
    
    return await puchkeep_manager._save_list_to_file(current_user["user_id"], file_name, content_list)

# --- Tool: help ---
HelpDesc = RichToolDescription(
    description="Show the help menu with all available commands, each with an emoji. Display the help text as a message.",
    use_when="Use when a user asks for help or available commands and expects a list with emojis.",
    side_effects="Returns a help message with all commands and emojis.",
    structure="ℹ️ **Help Menu**\nCommands:\n<list of commands with emojis and descriptions>"
)
@mcp.tool(description=HelpDesc.model_dump_json())
async def puchkeep_help() -> str:
    return (
        "**Help Menu**\n"
        "Commands:\n"
        "🆕 - `signup(username, password)`: Sign up for a new account\n"
        "🔑 - `login(username, password)`: Log in to your account\n"
        "🚪 - `logout()`: Log out from your account\n"
        "💾 - `save_memory(memory, name_of_memory)`: Save a new memory\n"
        "📋 - `list_memories()`: List all your memories\n"
        "🔍 - `get_memory(name_of_memory)`: Get a memory by its name\n"
        "❌ - `delete_memory(name_of_memory)`: Delete a memory by its name\n"
        "✏️ - `rename_memory(old_name, new_name)`: Rename a memory\n"
        "📚 - `use_memories(memory_names)`: Use multiple memories at once\n"
        "📄 - `save_list_to_text_file(file_name, content_list)`: Save a list to a .txt file\n"
    )

# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchKeep MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())