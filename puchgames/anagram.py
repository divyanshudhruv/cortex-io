import asyncio
import os
import random
from typing import Annotated, Optional, Dict
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INVALID_PARAMS
from wonderwords import RandomWord
import nltk
from collections import Counter

# --- Download NLTK word list (only needed once) ---
# This ensures the 'words' corpus is available for validation.
try:
    nltk.data.find('corpora/words')
except LookupError:
    print("Downloading NLTK 'words' corpus...")
    nltk.download('words')
    print("NLTK 'words' corpus downloaded.")

from nltk.corpus import words as nltk_words
# Create a set of lowercased English words for efficient lookup
ENGLISH_WORDS = set(w.lower() for w in nltk_words.words() if w.isalpha())

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
# Assert that the token is loaded, otherwise the server cannot run.
assert TOKEN, "AUTH_TOKEN environment variable not set!"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    """
    A simple bearer token authentication provider for FastMCP.
    Uses a pre-defined token for authentication.
    """
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """
        Loads an access token if the provided token matches the internal token.
        """
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="anagram-client",
                scopes=["*"], # All scopes are allowed for this client
                expires_at=None, # Token does not expire
            )
        return None # Return None if token is invalid

# --- In-memory Game Storage ---
# Stores current game states, keyed by game_id (default for single-player).
games: Dict[str, Dict] = {}
# Initialize RandomWord for generating anagrams.
rw = RandomWord()

# --- Helper Functions for Game Management ---

def get_game(game_id: str = "default") -> Dict:
    """
    Retrieves the game state for a given game ID.
    If the game ID doesn't exist, it initializes a new, inactive game state.
    """
    if game_id not in games:
        games[game_id] = {
            "word": "",
            "scrambled": "",
            "tries_left": 0,
            "game_over": True
        }
    return games[game_id]

def get_random_word_from_library(length: int = 6) -> str:
    """
    Generates a random English word of a specified length using the wonderwords library.
    It attempts to find a suitable word and validates it against NLTK's English word list.
    Raises an McpError if a suitable word cannot be found after several attempts.
    """
    max_attempts = 10 # Number of times to try finding a word
    for attempt in range(max_attempts):
        try:
            # Try to get a word with specific length and part of speech for better quality
            word = rw.word(
                word_min_length=length,
                word_max_length=length,
                include_parts_of_speech=["nouns", "adjectives", "verbs"],
                average_length=length # Helps wonderwords focus on the target length
            ).lower()
            
            # Validate that the generated word is indeed of the desired length and in English_WORDS
            if len(word) == length and word in ENGLISH_WORDS:
                return word
        except Exception:
            # wonderwords might raise exceptions if it can't find a word
            pass # Continue to next attempt

    # If all attempts fail, try to get any random word and validate it
    try:
        word = rw.word(include_parts_of_speech=["nouns", "adjectives", "verbs"]).lower()
        if word in ENGLISH_WORDS:
            print(f"⚠️ Warning: Could not find a word of exact length {length} after {max_attempts} attempts. Using '{word}' instead.")
            return word
    except Exception as e:
        # If even a general word cannot be found
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"❌ Failed to generate any random word: {e}. The word list might be exhausted or too restrictive."))
    
    # Final fallback if nothing works
    raise McpError(ErrorData(code=INVALID_PARAMS, message=f"❌ Could not generate a valid English word of length {length}. Try a different length or fewer restrictions."))


def can_form_from_letters(word: str, letters: str) -> bool:
    """
    Checks if `word` can be formed using the letters available in `letters`.
    This uses a multiset comparison (Counter).
    """
    return not (Counter(word) - Counter(letters))

# --- Game Logic Functions ---

def start_anagram_game(game_id: str = "default", length: int = 6, tries: int = 5) -> str:
    """
    Initializes and starts a new anagram game.
    A random word is chosen, scrambled, and the game state is reset.
    """
    if length < 3: # Minimum reasonable length for an anagram
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Word length must be at least 3."))
    if tries < 1: # Minimum reasonable tries
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Number of tries must be at least 1."))

    word = get_random_word_from_library(length) # Exclusively uses the library for word generation
    scrambled = ''.join(random.sample(word, len(word)))
    
    # Ensure the scrambled word is different from the original word
    while scrambled == word:
        scrambled = ''.join(random.sample(word, len(word)))
        if len(word) == 1: # Avoid infinite loop for single-letter words (though we limit to length >= 3)
            break

    games[game_id] = {
        "word": word,
        "scrambled": scrambled,
        "tries_left": tries,
        "game_over": False
    }
    return f"🔀 Your anagram: **{scrambled.upper()}**\nYou have {tries} tries to guess the word!"

def guess_anagram(guess: str, game_id: str = "default") -> str:
    """
    Processes a user's guess for the current anagram game.
    Validates the guess, updates game state, and provides feedback.
    """
    game = get_game(game_id)
    if game["game_over"]:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="🚫 No active game. Start a new one using `new_anagram()`."))

    guess = guess.lower().strip()

    # Comprehensive validation: check if it's an English word AND can be formed from scrambled letters.
    if not (guess in ENGLISH_WORDS and can_form_from_letters(guess, game["scrambled"])):
        game["tries_left"] -= 1 # Deduct a try even for invalid format guesses
        feedback = f"❌ '{guess}' is not a valid English word or cannot be formed from the letters in '{game['scrambled'].upper()}'."
        if game["tries_left"] <= 0:
            game["game_over"] = True
            return f"{feedback}\nOut of tries! The correct word was **{game['word']}**."
        else:
            return f"{feedback}\nTries left: {game['tries_left']}."


    if guess == game["word"]:
        game["game_over"] = True
        return f"✅ Correct! The word was **{game['word']}**."
    else:
        game["tries_left"] -= 1
        if game["tries_left"] <= 0:
            game["game_over"] = True
            return f"❌ Out of tries! The main word was **{game['word']}**."
        else:
            return f"🤷 Incorrect! Tries left: {game['tries_left']}."

def get_game_status(game_id: str = "default") -> str:
    """
    Returns the current status of the anagram game, including the scrambled word and remaining tries.
    """
    game = get_game(game_id)
    if game["game_over"]:
        return "ℹ️ No active game. Use `new_anagram()` to start."
    return f"🔀 Anagram: **{game['scrambled'].upper()}**\nTries left: {game['tries_left']}."

# --- MCP Server Setup ---
mcp = FastMCP(
    "Anagram MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- MCP Tools ---

@mcp.tool
async def new_anagram(
    tries: Annotated[int, "Number of tries the player gets for this game."] = 5,
    length: Annotated[int, "Desired length of the anagram word."] = 6
) -> str:
    """
    🎯 Starts a new anagram game.
    You can specify the number of `tries` and the `length` of the word.
    """
    return start_anagram_game(length=length, tries=tries)

@mcp.tool
async def guess_word(
    word: Annotated[str, "Your guess for the anagram. Must be a single word."]
) -> str:
    """
    ⌨️ Submit your guess for the anagram.
    The guess must be a valid English word that can be formed from the scrambled letters.
    """
    return guess_anagram(word)

@mcp.tool
async def get_status() -> str:
    """
    📋 Get the current status of the anagram game, including the scrambled word and remaining tries.
    """
    return get_game_status()

@mcp.tool
async def help_menu() -> str:
    """
    ℹ️ Shows a list of all available commands and their descriptions for the Anagram Solver.
    """
    return (
        "ℹ️ **Anagram Solver Help**\n"
        "🎯 - Start new game (`new_anagram(tries: int = 5, length: int = 6)`)\n"
        "⌨️ - Guess word (`guess_word(word: str)`)\n"
        "📋 - Show game status (`get_status()`)\n"
        "🔀 - Scrambled word\n"
        "✅ - Correct guess / Valid word\n"
        "❌ - Incorrect guess / Invalid word\n"
        "🤷 - Incorrect but valid word\n"
    )

# --- Main Server Run Loop ---
async def main():
    """
    Starts the FastMCP server for the Anagram Solver.
    """
    print("🚀 Starting Anagram MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())