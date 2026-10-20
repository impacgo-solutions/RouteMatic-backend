"""Password hashing (PBKDF2-SHA256) and JWT helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import get_settings

MIN_PASSWORD = 8
MAX_PASSWORD = 200


def _iterations() -> int:
    return get_settings().password_hash_iterations


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError("Password must be at least 8 characters.")
    if len(password) > MAX_PASSWORD:
        raise ValueError("Password must be at most 200 characters.")
    it = _iterations()
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), it)
    return f"pbkdf2_sha256${it}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when the stored hash uses a lower work factor than the current setting."""
    try:
        algo, it, _, _ = stored.split("$")
        return algo != "pbkdf2_sha256" or int(it) < _iterations()
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    s = get_settings().jwt_secret
    if len(s) < 32:
        raise RuntimeError("JWT_SECRET is missing or too short (min 32 chars). Set it in backend/.env.")
    return s


def create_token(user_id: str, version: int = 0) -> str:
    """``version`` = the account's credential version (incremented by every password change / logout)."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "tv": int(version or 0), "iat": now,
               "exp": now + timedelta(minutes=s.access_minutes),
               "iss": s.jwt_issuer, "aud": s.jwt_audience, "jti": secrets.token_hex(12)}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> str:
    return decode_claims(token)["sub"]


def decode_claims(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, _secret(), algorithms=["HS256"], issuer=s.jwt_issuer, audience=s.jwt_audience,
                      options={"require": ["exp", "iat", "sub", "iss", "aud"]})
