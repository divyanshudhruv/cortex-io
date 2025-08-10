import asyncio
from typing import Annotated, Literal
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import INVALID_PARAMS
from pydantic import BaseModel, Field

# Use supabase-py instead of the new client as it's more straightforward for this use case
from supabase import create_client, Client
import random

# --- Load environment variables ---
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"
assert SUPABASE_URL is not None, "Please set SUPABASE_URL in your .env file"
assert SUPABASE_KEY is not None, "Please set SUPABASE_KEY in your .env file"

# --- Supabase Client Initialization ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
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
    side_effects: str | None = None

# --- Game Logic and Supabase Interaction ---
class AnagramGame:
    POINTS_PER_WORD = 100
    DAILY_WORDS_COUNT = 5
    GUESSES_PER_WORD = 5

    @staticmethod
    async def signup_user(username: str, password: str) -> str:
        """Signs up a new user with a plain-text password."""
        # Check if user already exists in the anagram_users table
        response = supabase.from_("anagram_users").select("username").eq("username", username).limit(1).execute()
        if response.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Username already taken."))

        # Create the new user in the anagram_users table
        new_user = {"username": username, "password": password, "points": 0}
        response = supabase.from_("anagram_users").insert(new_user).execute()
        
        if response.data:
            return f"🎉 User '{username}' signed up successfully! Your user ID is '{response.data[0]['id']}'."
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Failed to sign up user."))

    @staticmethod
    async def login_user(username: str, password: str) -> str:
        """Logs in a user by verifying their password."""
        response = supabase.from_("anagram_users").select("id, password").eq("username", username).limit(1).execute()
        user_data = response.data
        
        if not user_data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Incorrect username or password."))
            
        if password == user_data[0]["password"]:
            return f"✅ Login successful! Your user ID is '{user_data[0]['id']}'. Use this ID for all game commands."
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Incorrect username or password."))

    @staticmethod
    async def get_daily_words():
        """
        Gets the current daily words. If the last set of words is more than 24 hours old,
        it generates a new set.
        """
        # Get the latest words
        response = supabase.from_("daily_words").select("*").order("created_at", desc=True).limit(5).execute()
        words_data = response.data

        # Check if new words need to be generated
        now = datetime.now(timezone.utc)
        if not words_data or (now - datetime.fromisoformat(words_data[0]["created_at"])) > timedelta(hours=24):
            print("Generating new daily words...")
            # Generate new words (placeholder logic)
            potential_words = ["python", "anagram", "challenge", "supabase", "developer", "computer", "science", "program", "backend", "frontend"]
            new_words = random.sample(potential_words, AnagramGame.DAILY_WORDS_COUNT)
            
            # Store the new words and their shuffled versions
            words_to_insert = [
                {
                    "word": word,
                    "shuffled_word": "".join(random.sample(word, len(word))),
                    "created_at": now.isoformat()
                }
                for word in new_words
            ]
            response = supabase.from_("daily_words").insert(words_to_insert).execute()
            return response.data
        
        return words_data

    @staticmethod
    async def get_shuffled_words(user_id: str) -> list[dict]:
        """Gets the daily words and checks which ones the user has already guessed correctly."""
        daily_words = await AnagramGame.get_daily_words()
        
        # Get the user's progress for today's words
        word_ids = [word["id"] for word in daily_words]
        response = supabase.from_("user_progress").select("word_id").eq("user_id", user_id).in_("word_id", word_ids).execute()
        guessed_word_ids = {item["word_id"] for item in response.data}
        
        shuffled_words = []
        for word in daily_words:
            if word["id"] in guessed_word_ids:
                shuffled_words.append({"shuffled_word": word["word"], "status": "✅ Guessed"})
            else:
                shuffled_words.append({"shuffled_word": word["shuffled_word"], "status": "❓ Not Guessed", "id": word["id"]})
                
        return shuffled_words

    @staticmethod
    async def submit_guess(user_id: str, word_id: str, user_guess: str) -> str:
        """Processes a user's guess for a given word."""
        # Check if the word exists and is the correct answer
        response = supabase.from_("daily_words").select("*").eq("id", word_id).limit(1).execute()
        word_data = response.data
        if not word_data:
            return "Error: Invalid word ID."
            
        correct_word = word_data[0]["word"]
        
        # Check user's current guess count for this word
        response = supabase.from_("user_guesses").select("guess_count").eq("user_id", user_id).eq("word_id", word_id).limit(1).execute()
        guess_data = response.data
        guess_count = guess_data[0]["guess_count"] if guess_data else 0
        
        # Check if the user has already solved this word
        response = supabase.from_("user_progress").select("*").eq("user_id", user_id).eq("word_id", word_id).limit(1).execute()
        if response.data:
            return f"You've already solved this word! The answer was '{correct_word}'. You won {AnagramGame.POINTS_PER_WORD} points for it."

        if guess_count >= AnagramGame.GUESSES_PER_WORD:
            return f"You have used all {AnagramGame.GUESSES_PER_WORD} guesses for this word. The correct answer was '{correct_word}'."
        
        # Update guess count
        if guess_data:
            supabase.from_("user_guesses").update({"guess_count": guess_count + 1}).eq("user_id", user_id).eq("word_id", word_id).execute()
        else:
            supabase.from_("user_guesses").insert({"user_id": user_id, "word_id": word_id, "guess_count": 1}).execute()

        if user_guess.lower() == correct_word.lower():
            # Correct guess: award points and mark as complete
            response = supabase.from_("anagram_users").select("points").eq("id", user_id).execute()
            current_points = response.data[0]["points"]
            supabase.from_("anagram_users").update({"points": current_points + AnagramGame.POINTS_PER_WORD}).eq("id", user_id).execute()
            supabase.from_("user_progress").insert({"user_id": user_id, "word_id": word_id}).execute()
            return f"🎉 Correct! You've earned {AnagramGame.POINTS_PER_WORD} points!"
        else:
            # Incorrect guess: provide feedback
            remaining_guesses = AnagramGame.GUESSES_PER_WORD - (guess_count + 1)
            return f"❌ Incorrect guess. You have {remaining_guesses} guesses remaining."
    
    @staticmethod
    async def get_leaderboard() -> list[dict]:
        """Gets the top 10 users by points."""
        response = supabase.from_("anagram_users").select("username, points").order("points", desc=True).limit(10).execute()
        return response.data

# --- MCP Server Setup ---
mcp = FastMCP(
    "Anagram Game MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    """A required validation tool."""
    return MY_NUMBER

# --- Tool: anagram_game ---
AnagramGameDescription = RichToolDescription(
    description="A daily anagram game where users can sign up, log in, get words, submit guesses, and view the leaderboard.",
    use_when="Use this to play the daily anagram game, check your progress, or see the top players.",
    side_effects="Creates a user account, authenticates a user, or updates user points and game progress on correct guesses.",
)

@mcp.tool(description=AnagramGameDescription.model_dump_json())
async def anagram_game(
    command: Annotated[Literal["signup", "login", "get_words", "submit_guess", "leaderboard"], Field(description="The command to execute.")],
    username: Annotated[str | None, Field(description="The user's unique username. Required for 'signup' and 'login'.")] = None,
    password: Annotated[str | None, Field(description="The user's password. Required for 'signup' and 'login'.")] = None,
    user_id: Annotated[str | None, Field(description="The unique user ID for the player. Required for game commands after login.")]= None,
    word_id: Annotated[str | None, Field(description="The ID of the word to guess. Required for 'submit_guess'.")] = None,
    guess: Annotated[str | None, Field(description="The user's guess for the word. Required for 'submit_guess'.")] = None,
) -> str:
    """
    Handles all commands for the Anagram Game.
    """
    if command == "signup":
        if not username or not password:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`username` and `password` are required for 'signup'."))
        return await AnagramGame.signup_user(username, password)
    
    elif command == "login":
        if not username or not password:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`username` and `password` are required for 'login'."))
        return await AnagramGame.login_user(username, password)

    if not user_id:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="A `user_id` is required for this command. Please login first to get your ID."))
    
    if command == "get_words":
        words_for_today = await AnagramGame.get_shuffled_words(user_id)
        if not words_for_today:
            return "No words available for today."
        
        output = "📝 **Today's Anagram Words**\n\n"
        for word in words_for_today:
            # Check for 'id' to determine if it's a new word or a solved one
            if 'id' in word:
                output += f"- `ID: {word['id']}` - `{word['shuffled_word']}`\n"
            else:
                output += f"- `{word['shuffled_word']}` ({word['status']})\n"
        
        return output

    elif command == "submit_guess":
        if not word_id or not guess:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`word_id` and `guess` are required for 'submit_guess'."))
        return await AnagramGame.submit_guess(user_id, word_id, guess)

    elif command == "leaderboard":
        leaderboard = await AnagramGame.get_leaderboard()
        if not leaderboard:
            return "No players on the leaderboard yet."
            
        output = "🏆 **Anagram Game Leaderboard**\n\n"
        for i, player in enumerate(leaderboard):
            output += f"{i + 1}. {player['username']} - {player['points']} points\n"
        
        return output
    
    else:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid command. Use 'signup', 'login', 'get_words', 'submit_guess', or 'leaderboard'."))

# --- Run MCP Server ---
async def main():
    print("🚀 Starting Anagram MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())

