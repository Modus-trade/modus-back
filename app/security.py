import os
import secrets
from datetime import timedelta
from typing import Annotated
import jwt
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from .database import get_db
from .models import User, now

passwords = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")
SECRET = os.getenv("JWT_SECRET") or secrets.token_hex(32)
DB = Annotated[Session, Depends(get_db)]


def current_user(request: Request, db: DB) -> User:
    try:
        payload = jwt.decode(
            request.cookies.get("session", ""), SECRET, algorithms=["HS256"]
        )
        user = db.get(User, int(payload["sub"]))
        if not user or not user.is_active:
            raise ValueError()
        return user
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Войдите в систему")


CurrentUser = Annotated[User, Depends(current_user)]


def admin(user: CurrentUser) -> User:
    if user.role != "ADMIN":
        raise HTTPException(403, "Доступ только для администратора")
    return user


Admin = Annotated[User, Depends(admin)]


def token(user: User) -> str:
    return jwt.encode(
        {"sub": str(user.id), "exp": now() + timedelta(hours=12)},
        SECRET,
        algorithm="HS256",
    )
