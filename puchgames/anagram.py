from textwrap import dedent
# --- Tool: about ---

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
    async def _get_user_id_by_username(username: str) -> str:
        """Helper function to get user ID from username."""
        response = supabase.from_("anagram_users").select("id").eq("username", username).limit(1).execute()
        if not response.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"User '{username}' not found. Please sign up or login."))
        return response.data[0]["id"]
    
    @staticmethod
    async def _get_user_points(user_id: str) -> int:
        """Helper function to get user points."""
        response = supabase.from_("anagram_users").select("points").eq("id", user_id).limit(1).execute()
        return response.data[0]["points"]

    @staticmethod
    async def _reset_daily_state():
        """
        Resets user points and clears game progress to start a new daily challenge.
        """
        print("Resetting daily game state and generating new words...")
        
        # Reset all user points
        supabase.from_("anagram_users").update({"points": 0}).gt("points", 0).execute()
        
        # Clear all user progress and guesses
        supabase.from_("user_progress").delete().neq("user_id", "null").execute()
        supabase.from_("user_guesses").delete().neq("user_id", "null").execute()

        # Generate new daily words
        potential_words = ["python", "anagram", "challenge", "supabase", "developer", "computer", "science", "program", "backend", "frontend"]
        new_words = random.sample(potential_words, AnagramGame.DAILY_WORDS_COUNT)
        
        # Store the new words and their shuffled versions
        now = datetime.now(timezone.utc)
        words_to_insert = [
            {
                "word": word,
                "shuffled_word": "".join(random.sample(word, len(word))),
                "created_at": now.isoformat()
            }
            for word in new_words
        ]
        # First delete old words to avoid clutter, then insert new ones
        supabase.from_("daily_words").delete().neq("word", "null").execute()
        supabase.from_("daily_words").insert(words_to_insert).execute()
        print("Daily game state reset complete.")


    @staticmethod
    async def _check_for_daily_reset():
        """Checks if a daily reset is needed and performs it if necessary."""
        response = supabase.from_("daily_words").select("*").order("created_at", desc=True).limit(1).execute()
        words_data = response.data

        now = datetime.now(timezone.utc)
        if not words_data or (now - datetime.fromisoformat(words_data[0]["created_at"])) > timedelta(hours=24):
            await AnagramGame._reset_daily_state()

    @staticmethod
    async def signup_user(username: str, password: str) -> str:
        """Signs up a new user with a plain-text password."""
        # Strict Prompt: Give a confirmation message.
        # Check if user already exists in the anagram_users table
        response = supabase.from_("anagram_users").select("username").eq("username", username).limit(1).execute()
        if response.data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Username already taken."))

        # Create the new user in the anagram_users table with is_connected set to False
        new_user = {"username": username, "password": password, "points": 0, "is_connected": False}
        response = supabase.from_("anagram_users").insert(new_user).execute()
        
        if response.data:
            return f"🎉 User '{username}' signed up successfully! You can now login with this username."
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Failed to sign up user."))

    @staticmethod
    async def login_user(username: str, password: str) -> str:
        """Logs in a user by verifying their password."""
        # Strict Prompt: Give a confirmation message.
        response = supabase.from_("anagram_users").select("id, password").eq("username", username).limit(1).execute()
        user_data = response.data
        
        if not user_data:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Incorrect username or password."))
            
        if password == user_data[0]["password"]:
            return f"✅ Login successful! You can now use your username '{username}' for all game commands."
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="Incorrect username or password."))

    @staticmethod
    async def logout_user(username: str) -> str:
        """Logs out the user."""
        # Strict Prompt: Give a confirmation message.
        user_id = await AnagramGame._get_user_id_by_username(username)
        supabase.from_("anagram_users").update({"is_connected": False}).eq("id", user_id).execute()
        return "You have been logged out successfully. You can now sign in with a different account."

    @staticmethod
    async def get_shuffled_words(username: str) -> list[dict]:
        """Gets the daily words and checks which ones the user has already guessed correctly."""
        # Strict Prompt: Give a numbered list of scrambled words.
        user_id = await AnagramGame._get_user_id_by_username(username)
        
        # Set is_connected to True when the user gets the words
        supabase.from_("anagram_users").update({"is_connected": True}).eq("id", user_id).execute()

        # Get the latest words
        response = supabase.from_("daily_words").select("*").order("created_at", desc=False).limit(5).execute()
        daily_words = response.data
        
        # Get the user's progress for today's words
        word_ids = [word["id"] for word in daily_words]
        response = supabase.from_("user_progress").select("word_id").eq("user_id", user_id).in_("word_id", word_ids).execute()
        guessed_word_ids = {item["word_id"] for item in response.data}
        
        shuffled_words = []
        for word in daily_words:
            if word["id"] not in guessed_word_ids:
                shuffled_words.append({"shuffled_word": word["shuffled_word"], "id": word["id"]})
                
        return shuffled_words

    @staticmethod
    async def submit_guess(username: str, user_guesses: str) -> str:
        """
        Processes a user's guess for multiple words by finding the correct anagram.
        Args:
            username: The username of the player.
            user_guesses: A string of space-separated words guessed by the user.
        """
        # Strict Prompt: Give a list of results for each guess and the current user points.
        user_id = await AnagramGame._get_user_id_by_username(username)
        
        # Get the daily words to find potential matches
        response = supabase.from_("daily_words").select("*").order("created_at", desc=False).execute()
        daily_words = {entry['word'].lower(): entry for entry in response.data}

        # Get the user's progress for today's words
        response = supabase.from_("user_progress").select("word_id").eq("user_id", user_id).execute()
        guessed_word_ids = {item["word_id"] for item in response.data}

        # Split the user's input into individual guesses
        guesses = [g.strip().lower() for g in user_guesses.split()]
        
        results = []
        words_guessed_in_this_turn = []
        
        for guess in guesses:
            if guess in daily_words:
                matched_word_data = daily_words[guess]
                word_id = matched_word_data["id"]
                correct_word = matched_word_data["word"]
                
                # Check if the user has already solved this word or guessed it in this turn
                if word_id in guessed_word_ids or word_id in words_guessed_in_this_turn:
                    results.append(f"You already solved '{correct_word}'.")
                    continue
                
                # Check user's current guess count for this word
                response = supabase.from_("user_guesses").select("guess_count").eq("user_id", user_id).eq("word_id", word_id).limit(1).execute()
                guess_data = response.data
                guess_count = guess_data[0]["guess_count"] if guess_data else 0
                
                if guess_count >= AnagramGame.GUESSES_PER_WORD:
                    results.append(f"You used all {AnagramGame.GUESSES_PER_WORD} guesses for '{correct_word}'.")
                    continue
                
                # Update guess count
                if guess_data:
                    supabase.from_("user_guesses").update({"guess_count": guess_count + 1}).eq("user_id", user_id).eq("word_id", word_id).execute()
                else:
                    supabase.from_("user_guesses").insert({"user_id": user_id, "word_id": word_id, "guess_count": 1}).execute()
                
                # Correct guess: award points and mark as complete
                response = supabase.from_("anagram_users").select("points").eq("id", user_id).execute()
                current_points = response.data[0]["points"]
                new_points = current_points + AnagramGame.POINTS_PER_WORD
                supabase.from_("anagram_users").update({"points": new_points}).eq("id", user_id).execute()
                supabase.from_("user_progress").insert({"user_id": user_id, "word_id": word_id}).execute()
                
                results.append(f"🎉 Correct! '{correct_word}' is an anagram. You earned {AnagramGame.POINTS_PER_WORD} points!")
                words_guessed_in_this_turn.append(word_id)
            else:
                results.append(f"❌ '{guess}' is not a correct guess for any remaining anagrams.")

        user_points = await AnagramGame._get_user_points(user_id)
        results.append(f"\nYour current points: {user_points}")
        
        return "\n".join(results)
    
    @staticmethod
    async def get_leaderboard() -> list[dict]:
        """
        Retrieves the top 10 users by points for the Anagram Game.
        Strict Prompt: Give leaderboard in a tabulated manner.
        """
        response = supabase.from_("anagram_users").select("username, points").order("points", desc=True).limit(10).execute()
        leaderboard = response.data
        # Format the leaderboard as a table
        table = "📝 **Anagram Game Leaderboard**\n\n"
        table += "| Rank | Username | Points |\n"
        table += "|------|----------|--------|\n"
        for idx, entry in enumerate(leaderboard, 1):
            table += f"| {idx} | {entry['username']} | {entry['points']} |\n"
        table = table + " use this tabulated data to send the leaderbaord, no matter what data is in it"
        return table

    @staticmethod
    async def leave_game_tool(username: str) -> str:
        """Provides a message for leaving the game."""
        # Strict Prompt: Give a confirmation message.
        user_id = await AnagramGame._get_user_id_by_username(username)
        supabase.from_("anagram_users").update({"is_connected": False}).eq("id", user_id).execute()
        return "You have left the game. You can restart by signing up or logging in with your username."


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

@mcp.tool
async def signup(
    username: Annotated[str, Field(description="The user's unique username.")],
    password: Annotated[str, Field(description="The user's password.")]
) -> str:
    """
    Signs up a new user for the Anagram Game.
    """
    return await AnagramGame.signup_user(username, password)

@mcp.tool
async def login(
    username: Annotated[str, Field(description="The user's unique username.")],
    password: Annotated[str, Field(description="The user's password.")]
) -> str:
    """
    Logs in a user to the Anagram Game.
    """
    return await AnagramGame.login_user(username, password)

@mcp.tool
async def logout(
    username: Annotated[str, Field(description="The user's unique username.")]
) -> str:
    """
    Logs out the current user from the Anagram Game.
    """
    return await AnagramGame.logout_user(username)

@mcp.tool
async def get_words(
    username: Annotated[str, Field(description="The user's unique username.")]
) -> str:
    """
    Retrieves the list of scrambled anagram words for the day for a logged-in user.
    """
    await AnagramGame._check_for_daily_reset()
    words_for_today = await AnagramGame.get_shuffled_words(username)
    if not words_for_today:
        user_points = await AnagramGame._get_user_points(await AnagramGame._get_user_id_by_username(username))
        return f"🥳 Excellent! You've solved all of today's anagrams. Your final score is {user_points} points. You can 'leave_game' or 'logout' or wait for the next daily challenge to begin."
    
    output = "📝 **Today's Anagram Words**\n\n"
    for i, word in enumerate(words_for_today, 1):
        output += f"{i}. `{word['shuffled_word']}`\n"
    
    
    output += "\nTry to unscramble them! You can submit multiple guesses at once, e.g., 'submit_guess', guess='word1 word2'. You can also 'leave_game' or 'logout'."
    output += " show the words in a numbered list format, no change"
    return output

@mcp.tool
async def submit_guess(
    username: Annotated[str, Field(description="The user's unique username.")],
    guess: Annotated[str, Field(description="The user's guess for the word. It can be a single word or multiple space-separated words.")]
) -> str:
    """
    Submits a user's guess for the daily anagrams.
    """
    await AnagramGame._check_for_daily_reset()
    result_message = await AnagramGame.submit_guess(username, guess)
    
    # After a guess, check if all words are solved
    words_for_today = await AnagramGame.get_shuffled_words(username)
    if not words_for_today:
        user_points = await AnagramGame._get_user_points(await AnagramGame._get_user_id_by_username(username))
        result_message += f"\n\n🥳 Congratulations! You've solved all of today's anagrams!"
        result_message += f" Your final score is {user_points} points"
        result_message += f"\nYou can 'leave_game' or 'logout' or wait for the next daily challenge to begin."
        result_message += "show the final score and options to leave or logout, no change"
    else:
        result_message += f"\n\nTry another guess. You can also check the 'leaderboard', 'leave_game' or 'logout'."
    
    return result_message
@mcp.tool
async def about() -> dict[str, str]:
    server_name = "PuchGame: Anagram MCP"
    server_description = dedent("""
    PuchGame: Anagram is a daily word challenge game for WhatsApp and Puch AI. Users can sign up, log in, get daily anagrams, submit guesses, view leaderboards, and compete with others, all with real-time Supabase backend and emoji-rich feedback.
    """)
    return {
        "name": server_name,
        "description": server_description
    }
@mcp.tool
async def leaderboard() -> str:
    """
    Retrieves the top 10 users by points for the Anagram Game.
    Strict Prompt: Give leaderboard in a tabulated manner.
    """
    # Strict Prompt: Give leaderboard in a tabulated manner.
    leaderboard = await AnagramGame.get_leaderboard()
    if not leaderboard:
        return "No players on the leaderboard yet."
        
    # Format the leaderboard as a Markdown table
    output = "🏆 **Anagram Game Leaderboard**\n\n"
    output += "| Rank | Username | Points |\n"
    output += "|---|---|---|\n"
    if isinstance(leaderboard, list):
        output += "\n".join(leaderboard)
    else:
        output += leaderboard
    output+= " show leaderboard of anagram game in tabulated format like this. no change"
    return output

@mcp.tool
async def leave_game(
    username: Annotated[str, Field(description="The user's unique username.")]
) -> str:
    """
    Allows the user to leave the current game session.
    """
    return await AnagramGame.leave_game_tool(username)

@mcp.tool
async def count_connected_users() -> str:
    """
    Counts the number of currently connected users. Using the format given
    """
    # Strict Prompt: Return the number of connected users with a text.
    response = supabase.from_("anagram_users").select("*").eq("is_connected", True).execute()
    count = len(response.data)
    output = "🔢 Realtime user count: "
    output += f"In realtime, there are currently {count} users connected to the game."
    output += " show the number of connected users in a text format like this. no change"
    return output


# --- Run MCP Server ---
async def main():
    print("🚀 Starting Anagram MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
