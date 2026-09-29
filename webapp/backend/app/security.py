import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.low_level import Type
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .config import settings
from .database import get_db
from .errors import APIError
from .models import Session, User, now


hasher = PasswordHasher(type=Type.ID)
COOKIE_NAME = "presentation_session"


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return hasher.verify(encoded, password)
    except VerifyMismatchError:
        return False


def create_session(db: DBSession, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(Session(id=hashlib.sha256(raw.encode()).hexdigest(), user_id=user.id, expires_at=now() + timedelta(days=settings.session_days)))
    db.commit()
    return raw


def current_user(
    raw: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: DBSession = Depends(get_db),
) -> User:
    if not raw:
        raise APIError(401, "not_authenticated", "Требуется вход")
    key = hashlib.sha256(raw.encode()).hexdigest()
    session = db.scalar(select(Session).where(Session.id == key, Session.expires_at > now()))
    if not session:
        raise APIError(401, "not_authenticated", "Сессия истекла")
    configured_password = settings.allowed_clients.get(session.user.username)
    if configured_password is None or not verify_password(session.user.password_hash, configured_password):
        raise APIError(401, "not_authenticated", "Доступ отозван")
    return session.user
