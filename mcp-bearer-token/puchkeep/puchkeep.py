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
import uuid
from supabase import create_client, Client

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

# --- Supabase connection ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- PuchKeep Manager (stateless, uses puch_user_id) ---
class PuchKeepManager:
    def add_memory(self, puch_user_id: str, memory: str, name_of_memory: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        existing = supabase.table("puchkeep").select("*").eq("user_id", puch_user_id).eq("name_of_memory", name_of_memory).execute()
        if existing.data:
            return f"⚠️ Memory name '{name_of_memory}' already exists. Please use a different name."
        memory_id = str(uuid.uuid4())
        res = supabase.table("puchkeep").insert({
            "id": memory_id,
            "user_id": puch_user_id,
            "memory": memory,
            "name_of_memory": name_of_memory
        }).execute()
        if res.data:
            return f"**SEND THIS DATA TO THE USER,STRICTLY**: 💾 Memory saved!\nStatus: ✅\nMemory Name: '{name_of_memory}'\nMessage: Your memory has been stored successfully."
        return "**SEND THIS DATA TO THE USER,STRICTLY**: ❌ Failed to save memory. Please try again."

    def list_memories(self, puch_user_id: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        res = supabase.table("puchkeep").select("*").eq("user_id", puch_user_id).execute()
        if not res.data:
            return "**SEND THIS DATA TO THE USER,STRICTLY**: 📋 No memories saved yet. Start by adding a new one!"
        return (
            "**SEND THIS DATA TO THE USER,STRICTLY**: "+
            "📋 **Your Memories**\n" +
            "\n".join(
                f"• 📝 {m.get('name_of_memory', '(no name)')}: {m['memory']}"
                for m in res.data
            )
        )

    def get_memory(self, puch_user_id: str, name_of_memory: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        res = supabase.table("puchkeep").select("*").eq("user_id", puch_user_id).eq("name_of_memory", name_of_memory).execute()
        if not res.data:
            return f"**SEND THIS DATA TO THE USER,STRICTLY**: 🔍 **Memory Retrieved**\nMemory Name: {name_of_memory}\nStatus: ❌\nMessage: Memory not found."
        m = res.data[0]
        return (
            f"**SEND THIS DATA TO THE USER,STRICTLY**: "
            f"🔍 **Memory Retrieved**\n"
            f"Memory Name: {name_of_memory}\n"
            f"Memory: {m['memory']}\n"
            f"Status: ✅\n"
            f"Message: Memory found and displayed above."+
            "**SEND THIS DATA TO THE USER,STRICTLY** "
        )

    def get_multiple_memories(self, puch_user_id: str, memory_names: List[str], task: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        found_memories = []
        not_found = []
        for name in memory_names:
            res = supabase.table("puchkeep").select("*").eq("user_id", puch_user_id).eq("name_of_memory", name).execute()
            if res.data:
                m = res.data[0]
                found_memories.append(f"• 📝 {name}: {m['memory']}")
            else:
                not_found.append(name)
        result = ""
        if found_memories:
            result += "**SEND THIS DATA TO THE USER,STRICTLY**: 📚 **Multiple Memories**\n" + "\n".join(found_memories)
        if not_found:
            if result:
                result += "\n"
            result += "Not found: " + ", ".join(not_found)
        return result if result else "**SEND THIS DATA TO THE USER,STRICTLY**: 📚 No memories found for the given names."

    def delete_memory(self, puch_user_id: str, name_of_memory: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        res = supabase.table("puchkeep").delete().eq("user_id", puch_user_id).eq("name_of_memory", name_of_memory).execute()
        if res.data:
            return f"❌ **SEND THIS DATA TO THE USER,STRICTLY**: **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ✅\nMessage: Memory deleted successfully."
        return f"❌ **SEND THIS DATA TO THE USER,STRICTLY**: **Memory Deleted**\nMemory Name: {name_of_memory}\nStatus: ❌\nMessage: Memory not found."

    def rename_memory(self, puch_user_id: str, old_name: str, new_name: str) -> str:
        if not puch_user_id:
            return "🔒 puch_user_id is required."
        existing = supabase.table("puchkeep").select("*").eq("user_id", puch_user_id).eq("name_of_memory", new_name).execute()
        if existing.data:
            return f"✏️ **SEND THIS DATA TO THE USER,STRICTLY**: Memory Renamed\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory name '{new_name}' already exists."
        res = supabase.table("puchkeep").update({"name_of_memory": new_name}).eq("user_id", puch_user_id).eq("name_of_memory", old_name).execute()
        if res.data:
            return f"✏️ **SEND THIS DATA TO THE USER,STRICTLY**: **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ✅\nMessage: Memory renamed successfully."
        return f"✏️ **SEND THIS DATA TO THE USER,STRICTLY**: **Memory Renamed**\nOld Name: {old_name}\nNew Name: {new_name}\nStatus: ❌\nMessage: Memory not found."

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
    return MY_NUMBER

# --- Tool: save_memory ---
@mcp.tool
async def save_memory(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    memory: Annotated[str, Field(description="Memory text")],
    name_of_memory: Annotated[str, Field(description="Unique name for this memory")]
) -> str:
    return puchkeep_manager.add_memory(puch_user_id, memory, name_of_memory)

# --- Tool: list_memories ---
@mcp.tool
async def list_memories(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")]
) -> str:
    return puchkeep_manager.list_memories(puch_user_id)

# --- Tool: get_memory ---
@mcp.tool
async def get_memory(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    name_of_memory: Annotated[str, Field(description="Memory name")]
) -> str:
    return puchkeep_manager.get_memory(puch_user_id, name_of_memory)

# --- Tool: delete_memory ---
@mcp.tool
async def delete_memory(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    name_of_memory: Annotated[str, Field(description="Memory name")]
) -> str:
    return puchkeep_manager.delete_memory(puch_user_id, name_of_memory)

# --- Tool: rename_memory ---
@mcp.tool
async def rename_memory(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    old_name: Annotated[str, Field(description="Current memory name")],
    new_name: Annotated[str, Field(description="New memory name")]
) -> str:
    return puchkeep_manager.rename_memory(puch_user_id, old_name, new_name)

# --- Tool: use_memories ---
@mcp.tool
async def use_memories(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    memory_names: Annotated[List[str], Field(description="List of memory names to retrieve")],
    task: Annotated[str, Field(description="Task to perform with the memories")]
) -> str:
    return puchkeep_manager.get_multiple_memories(puch_user_id, memory_names, task)

# --- Run MCP Server ---
async def main():
    print("🚀 Starting PuchKeep MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
