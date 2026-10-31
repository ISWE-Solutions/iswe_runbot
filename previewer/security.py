import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """False for an empty hash: Microsoft/GitHub-only users have no password."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set (see .env.example)")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Cannot decrypt stored secret: ENCRYPTION_KEY changed?") from e


def random_secret(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)
