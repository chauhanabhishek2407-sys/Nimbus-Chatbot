"""All settings in one place. Values come from environment variables or a .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# --- App -------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "Nimbus").strip() or "Nimbus"
ALLOW_SIGNUPS = _bool("ALLOW_SIGNUPS", True)
SESSION_DAYS = _int("SESSION_DAYS", 30)

# --- Gen AI model -------------------------------------------------------------
# "auto" uses Claude if ANTHROPIC_API_KEY is set, else Gemini if GEMINI_API_KEY is set,
# else demo mode (sample replies, so you can try the app before you have a key).
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5").strip()
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
CLAUDE_WEB_SEARCH_TOOL = os.getenv("CLAUDE_WEB_SEARCH_TOOL", "web_search_20250305").strip()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")

# Let the model search the web for up-to-date answers with sources (costs extra).
WEB_SEARCH = _bool("WEB_SEARCH", False)

MAX_OUTPUT_TOKENS = _int("MAX_OUTPUT_TOKENS", 4096)
# Gemini counts its "thinking" in the output limit, so it gets more room.
GEMINI_MAX_OUTPUT_TOKENS = _int("GEMINI_MAX_OUTPUT_TOKENS", 8192)
EXTRA_INSTRUCTIONS = os.getenv("SYSTEM_PROMPT", "").strip()

# --- Limits (protect your API bill) -------------------------------------------------
MAX_MESSAGE_CHARS = _int("MAX_MESSAGE_CHARS", 8000)
HISTORY_MAX_MESSAGES = _int("HISTORY_MAX_MESSAGES", 30)
HISTORY_CHAR_BUDGET = _int("HISTORY_CHAR_BUDGET", 60000)
CHAT_LIMIT_PER_10_MIN = _int("CHAT_LIMIT_PER_10_MIN", 30)

# --- Database ------------------------------------------------------------------
# Empty = local SQLite file (fine on your computer).
# In the cloud, set a PostgreSQL URL: postgresql://user:password@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("SQLITE_PATH", "chat.db")
DB_POOL_SIZE = _int("DB_POOL_SIZE", 5)

# Demo mode typing speed (seconds per word).
DEMO_DELAY = float(os.getenv("DEMO_DELAY", "0.025"))
