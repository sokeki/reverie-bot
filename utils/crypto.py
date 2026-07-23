"""Shared helper for encrypting/decrypting stored Riot sessions. Used by both
cogs/valshop.py (the Discord bot process) and dashboard/app.py (the web
process) — they're separate processes but share the same MongoDB, so this
logic needs to be identical and importable from both."""

import json
import os

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None


def _get_fernet():
    """Returns a Fernet instance, or None if not configured. Never raises —
    a missing/bad key should degrade to a clear error message, not crash
    whichever process calls this."""
    key = os.getenv("FERNET_KEY", "")
    if Fernet is None or not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def is_configured() -> bool:
    """Whether FERNET_KEY is set and valid — check this before starting a
    flow that will need to store something, so the failure is a clear
    message up front rather than a silent None deep in a DB write."""
    return _get_fernet() is not None


def encrypt_session(data: dict):
    f = _get_fernet()
    if not f:
        return None
    return f.encrypt(json.dumps(data).encode())


def decrypt_session(token) -> dict | None:
    f = _get_fernet()
    if not f or not token:
        return None
    try:
        return json.loads(f.decrypt(bytes(token)).decode())
    except Exception:
        return None