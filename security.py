"""Passwords, session tokens, input checks and rate limiting."""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

# scrypt settings (OWASP-recommended family; ~16 MB of memory per hash)
_N, _R, _P = 2**14, 8, 5
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, key = stored.split("$")
        if algo != "scrypt":
            return False
        expected = _unb64(key)
        test = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_MAXMEM, dklen=len(expected),
        )
        return hmac.compare_digest(test, expected)
    except (ValueError, TypeError):
        return False


# Used to spend the same time on logins for unknown emails (hides which emails exist).
DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only a hash of each session token is stored, so a leaked database can't log anyone in."""
    return hashlib.sha256(token.encode()).hexdigest()


# --- Input checks ---------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_len]


def normalize_email(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def password_problem(password) -> str | None:
    if not isinstance(password, str) or len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 200:
        return "Password is too long."
    return None


# --- Rate limiting ----------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter kept in memory (per server instance)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 20000:  # forget idle keys
                for k in [k for k, q in self._hits.items() if not q or q[-1] < now - 3600]:
                    del self._hits[k]
            hits = self._hits[key]
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True
