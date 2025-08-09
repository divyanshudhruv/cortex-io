import asyncio
from typing import Annotated, Optional, List
import os
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from pydantic import Field
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

# --- MCP Server ---
mcp = FastMCP(
    "PuchKeep MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Supabase connection ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Session state ---
current_user: dict = {"username": None, "user_id": None}

# --- PuchKeep Manager ---
class PuchKeepManager:
    def signup(self, username: str, password: str) -> str:
        existing = supabase.table("users").select("*").eq("username", username).execute()
        if existing.data:
            return "Username already exists."
        res = supabase.table("users").insert({"username": username, "password": password}).execute()
        if res.data:
            return "Signup successful."
        return "Failed to create account."

    def login(self, username: str, password: str) -> str:
        global current_user
        user = supabase.table("users").select("*").eq("username", username).eq("password", password).execute()
        if not user.data:
            return "Invalid username or password."
        current_user["username"] = username
        current_user["user_id"] = user.data[0]["id"]
        return f"Logged in as {username}."

    def logout(self) -> str:
        global current_user
        if not current_user["username"]:
            return "You are not logged in."
        name = current_user["username"]
        current_user["username"] = None
        current_user["user_id"] = None
        return f"Logged out from {name}."

    def add_memory(self, memory: str, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        existing = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if existing.data:
            return f"Memory name '{name_of_memory}' already exists."
        memory_id = str(uuid.uuid4())
        res = supabase.table("puchkeep").insert({
            "id": memory_id,
            "user_id": current_user["user_id"],
            "memory": memory,
            "name_of_memory": name_of_memory
        }).execute()
        if res.data:
            return f"Memory saved with name: '{name_of_memory}'."
        return "Failed to save memory."

    def list_memories(self) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).execute()
        if not res.data:
            return "No memories saved yet."
        return (
            "Your memories:\n" +
            "\n".join(
                f"- {m.get('name_of_memory', '(no name)')}: {m['memory']}"
                for m in res.data
            )
        )

    def get_memory(self, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if not res.data:
            return "Memory not found."
        m = res.data[0]
        return f"Memory '{name_of_memory}':\n{m['memory']}"

    def get_multiple_memories(self, memory_names: List[str]) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        found_memories = []
        not_found = []
        for name in memory_names:
            res = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", name).execute()
            if res.data:
                m = res.data[0]
                found_memories.append(f"- {name}: {m['memory']}")
            else:
                not_found.append(name)
        result = ""
        if found_memories:
            result += "Requested memories:\n" + "\n".join(found_memories)
        if not_found:
            if result:
                result += "\n"
            result += "Not found: " + ", ".join(not_found)
        return result if result else "No memories found."

    def delete_memory(self, name_of_memory: str) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        res = supabase.table("puchkeep").delete().eq("user_id", current_user["user_id"]).eq("name_of_memory", name_of_memory).execute()
        if res.data:
            return "Memory deleted."
        return "Memory not found."

    def rename_memory(self, old_name: str, new_name: str) -> str:
        if not current_user["user_id"]:
            return "Please login first."
        # Check if new name already exists
        existing = supabase.table("puchkeep").select("*").eq("user_id", current_user["user_id"]).eq("name_of_memory", new_name).execute()
        if existing.data:
            return f"Memory name '{new_name}' already exists."
        # Update the memory name
        res = supabase.table("puchkeep").update({"name_of_memory": new_name}).eq("user_id", current_user["user_id"]).eq("name_of_memory", old_name).execute()
        if res.data:
            return f"Memory renamed from '{old_name}' to '{new_name}'."
        return "Memory not found."

# --- Create manager instance ---
puchkeep_manager = PuchKeepManager()
# --- MCP Tools ---
@mcp.tool(
    description=(
        "Sign up for a new account.\n"
        "Example:\n"
        "signup(username='johndoe', password='mypassword')\n"
        "Output:\n"
        "- '✅ Signup successful.'\n"
        "- '❌ Username already exists.'"
    )
)
async def signup(username: Annotated[str, Field(..., description="Username")],
                 password: Annotated[str, Field(..., description="Password")]) -> str:
    result = puchkeep_manager.signup(username, password)
    if result == "Signup successful.":
        return "✅ Signup successful."
    elif result == "Username already exists.":
        return "❌ Username already exists."
    else:
        return "❌ Failed to create account."

@mcp.tool(
    description=(
        "Log in to your account.\n"
        "Example:\n"
        "login(username='johndoe', password='mypassword')\n"
        "Output:\n"
        "- '🔓 Logged in as johndoe.'\n"
        "- '❌ Invalid username or password.'"
    )
)
async def login(username: Annotated[str, Field(..., description="Username")],
                password: Annotated[str, Field(..., description="Password")]) -> str:
    result = puchkeep_manager.login(username, password)
    if result.startswith("Logged in as"):
        return f"🔓 {result}"
    else:
        return "❌ Invalid username or password."

@mcp.tool(
    description=(
        "Log out from your account.\n"
        "Example:\n"
        "logout()\n"
        "Output:\n"
        "- '🚪 Logged out from johndoe.'\n"
        "- '❌ You are not logged in.'"
    )
)
async def logout() -> str:
    result = puchkeep_manager.logout()
    if result.startswith("Logged out from"):
        return f"🚪 {result}"
    else:
        return "❌ You are not logged in."

@mcp.tool(
    description=(
        "Save a new memory.\n"
        "Example:\n"
        "save_memory(memory='My first memory', name_of_memory='birthday')\n"
        "Output:\n"
        "- '💾 Memory saved with name: birthday.'\n"
        "- '❌ Memory name already exists.'"
    )
)
async def save_memory(
    memory: Annotated[str, Field(description="Memory text")],
    name_of_memory: Annotated[str, Field(description="Unique name for this memory")]
) -> str:
    result = puchkeep_manager.add_memory(memory, name_of_memory)
    if result.startswith("Memory saved"):
        return f"💾 {result}"
    elif "already exists" in result:
        return f"❌ {result}"
    else:
        return "❌ Failed to save memory."

@mcp.tool(
    description=(
        "List all your memories.\n"
        "Example:\n"
        "list_memories()\n"
        "Output:\n"
        "📜 Your memories list:\n"
        "1. name - memory\n"
        "2. name2 - memory2\n"
        "..."
    )
)
async def list_memories() -> str:
    result = puchkeep_manager.list_memories()
    if result.startswith("Your memories:"):
        lines = result.splitlines()[1:]
        formatted = "\n".join(f"{i+1}. {line[2:]}" for i, line in enumerate(lines))
        return f"📜 Your memories list:\n{formatted}"
    else:
        return "📜 No memories saved yet."

@mcp.tool(
    description=(
        "Get a memory by its name.\n"
        "Example:\n"
        "get_memory(name_of_memory='birthday')\n"
        "Output:\n"
        "🔍 Memory 'birthday': memory text\n"
        "- '❌ Memory not found.'"
    )
)
async def get_memory(name_of_memory: Annotated[str, Field(description="Memory name")]) -> str:
    result = puchkeep_manager.get_memory(name_of_memory)
    if result.startswith("Memory '"):
        name = name_of_memory
        mem = result.split(":", 1)[1].strip()
        return f"🔍 Memory '{name}': {mem}"
    else:
        return "❌ Memory not found."

@mcp.tool(
    description=(
        "Delete a memory by its name.\n"
        "Example:\n"
        "delete_memory(name_of_memory='birthday')\n"
        "Output:\n"
        "- '🗑️ Memory deleted.'\n"
        "- '❌ Memory not found.'"
    )
)
async def delete_memory(name_of_memory: Annotated[str, Field(description="Memory name")]) -> str:
    result = puchkeep_manager.delete_memory(name_of_memory)
    if result == "Memory deleted.":
        return "🗑️ Memory deleted."
    else:
        return "❌ Memory not found."

@mcp.tool(
    description=(
        "Rename a memory.\n"
        "Example:\n"
        "rename_memory(old_name='birthday', new_name='party')\n"
        "Output:\n"
        "- '✏️ Memory renamed from birthday to party.'\n"
        "- '❌ Memory name already exists.'\n"
        "- '❌ Memory not found.'"
    )
)
async def rename_memory(
    old_name: Annotated[str, Field(description="Current memory name")],
    new_name: Annotated[str, Field(description="New memory name")]
) -> str:
    result = puchkeep_manager.rename_memory(old_name, new_name)
    if result.startswith("Memory renamed"):
        return f"✏️ {result}"
    elif "already exists" in result:
        return f"❌ {result}"
    else:
        return "❌ Memory not found."

@mcp.tool(
    description=(
        "Show help menu with all commands.\n"
        "Example:\n"
        "puchkeep_help()\n"
        "Output:\n"
        "🆘 Commands:\n"
        "1. signup\n"
        "2. login\n"
        "3. logout\n"
        "4. save_memory\n"
        "5. list_memories\n"
        "6. get_memory\n"
        "7. delete_memory\n"
        "8. rename_memory\n"
        "9. use_memories"
    )
)
async def puchkeep_help() -> str:
    return (
        "🆘 Commands:\n"
        "1. signup(username, password)\n"
        "2. login(username, password)\n"
        "3. logout()\n"
        "4. save_memory(memory, name_of_memory)\n"
        "5. list_memories()\n"
        "6. get_memory(name_of_memory)\n"
        "7. delete_memory(name_of_memory)\n"
        "8. rename_memory(old_name, new_name)\n"
        "9. use_memories(memory_names)"
    )

@mcp.tool(
    description=(
        "Get multiple memories by names.\n"
        "Example:\n"
        "use_memories(memory_names=['birthday', 'vacation'])\n"
        "Output:\n"
        "📚 Requested memories:\n"
        "1. birthday - memory text\n"
        "2. vacation - memory text\n"
        "❌ Not found: name1, name2"
    )
)
async def use_memories(
    memory_names: Annotated[List[str], Field(description="List of memory names to retrieve")]
) -> str:
    result = puchkeep_manager.get_multiple_memories(memory_names)
    lines = []
    if "Requested memories:" in result:
        mems = []
        not_found = []
        for line in result.splitlines():
            if line.startswith("- "):
                mems.append(line[2:])
            elif line.startswith("Not found:"):
                not_found.append(line)
        if mems:
            lines.append("📚 Requested memories:")
            for i, m in enumerate(mems):
                name, mem = m.split(":", 1)
                lines.append(f"{i+1}. {name.strip()} - {mem.strip()}")
        if not_found:
            lines.append(f"❌ {not_found[0]}")
        return "\n".join(lines)
    elif "Not found:" in result:
        return f"❌ {result}"
    else:
        return "❌ No memories found."

# --- Run MCP Server ---
async def main():
    print("Starting PuchKeep MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
