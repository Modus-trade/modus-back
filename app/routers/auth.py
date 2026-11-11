import os
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select
from ..models import User
from ..schemas import Login, UserPublic
from ..security import CurrentUser, DB, passwords, token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserPublic)
def login(data: Login, response: Response, db: DB):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if (
        not user
        or not user.is_active
    ):
        raise HTTPException(401, "Неверная почта или пароль")
    # Legacy bcrypt truncates at 72 bytes. Never accept ambiguous long inputs.
    if passwords.identify(user.password_hash) == "bcrypt" and len(data.password.encode("utf-8")) > 72:
        raise HTTPException(401, "Обновите пароль через администратора")
    valid, replacement = passwords.verify_and_update(data.password, user.password_hash)
    if not valid:
        raise HTTPException(401, "Неверная почта или пароль")
    if replacement:
        user.password_hash = replacement
        db.commit()
    response.set_cookie(
        "session",
        token(user),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE") == "true",
        max_age=43200,
    )
    return user


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/me", response_model=UserPublic)
def me(user: CurrentUser):
    return user
