"""
Authentifizierung: Passwort-Hashing, Session-Token-Verwaltung, Credential-Storage in SQLite.
"""
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional
from app.database import get_db

# In-Memory Token-Store: {token: expires_at}
# Kein Neustart-Problem — Tokens sind kurzlebig (8h), nach Neustart müssen User sich neu einloggen.
_sessions: dict[str, float] = {}
SESSION_TTL = 8 * 3600  # 8 Stunden


# ── Passwort-Hashing (PBKDF2-SHA256) ─────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, dk_hex = stored_hash.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# ── Credentials in DB ────────────────────────────────────────
def get_credentials() -> Optional[tuple[str, str]]:
    """Gibt (username, password_hash) zurück oder None wenn noch nicht eingerichtet."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'admin_credentials'"
            ).fetchone()
            if row:
                data = json.loads(row["value"])
                return data["username"], data["password_hash"]
    except Exception:
        pass
    return None


def set_credentials(username: str, password: str) -> None:
    data = json.dumps({
        "username": username,
        "password_hash": hash_password(password),
    })
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_credentials', ?)",
            (data,),
        )
        conn.commit()


def is_setup_done() -> bool:
    return get_credentials() is not None


# ── Session-Tokens ────────────────────────────────────────────
def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    _cleanup_sessions()
    return token


def validate_session(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None or time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True


def revoke_session(token: Optional[str]) -> None:
    _sessions.pop(token, None)


def _cleanup_sessions() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if now > exp]
    for t in expired:
        del _sessions[t]
