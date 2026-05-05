"""Web chat authentication — JWT tokens + bcrypt password hashing."""
import os
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, HTTPException, Request
from jose import JWTError, jwt
import bcrypt

from taghdev.models.user import User
from taghdev.models.base import async_session
from sqlalchemy import select

# Configuration
WEB_JWT_SECRET = os.environ.get("WEB_CHAT_JWT_SECRET", "")
if not WEB_JWT_SECRET or WEB_JWT_SECRET == "change-me-in-production":
    raise RuntimeError(
        "WEB_CHAT_JWT_SECRET environment variable is not set or uses the insecure default. "
        "Generate a strong secret (e.g. `openssl rand -hex 32`) and set it in your environment."
    )
WEB_JWT_ALGORITHM = "HS256"
WEB_JWT_EXPIRE_HOURS = 24 * 30  # 30 days


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_web_token(user_id: int) -> str:
    """Create a JWT token for a web user."""
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(hours=WEB_JWT_EXPIRE_HOURS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, WEB_JWT_SECRET, algorithm=WEB_JWT_ALGORITHM)


async def verify_web_token(token: str) -> User | None:
    """Decode and verify a JWT token, return User if valid."""
    try:
        payload = jwt.decode(token, WEB_JWT_SECRET, algorithms=[WEB_JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            return None
    except JWTError:
        return None

    # Load user from DB and verify is_allowed
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        if user and user.is_allowed:
            return user
    return None


async def web_user_dep(
    request: Request,
    web_token: str | None = Cookie(default=None),
) -> User:
    """FastAPI dependency — validates token from cookie, returns User or raises 401.

    If token is invalid or user not allowed, raises HTTPException 401.
    """
    if not web_token:
        raise HTTPException(status_code=401, detail="No authentication token")

    user = await verify_web_token(web_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user
