"""Auth routes (THIN): login (PIN + lockout), first-login (code -> forced PIN),
logout, me. JWT rides an httpOnly cookie.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.core.deps import current_user
from app.core.security import (
    check_lockout, hash_pin, issue_token, record_failed_attempt,
    record_success, verify_pin,
)
from app.db.base import get_session
from app.db.models.user import User
from app.services.team import redeem_setup_code

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    pin: str


class FirstLoginBody(BaseModel):
    username: str
    code: str
    pin: str
    display_name: str | None = None


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "zagent_token", token, httponly=True, samesite="lax",
        secure=False,  # Phase 1: plain HTTP; flip once TLS terminates ahead
        max_age=60 * 60 * 24 * 14,
    )


@router.post("/login")
def login(body: LoginBody, response: Response):
    session = get_session()
    try:
        user = session.query(User).filter_by(username=body.username).one_or_none()
        if user is None or user.pin_hash is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        check_lockout(user)
        if not verify_pin(body.pin, user.pin_hash):
            record_failed_attempt(session, user)
            raise HTTPException(status_code=401, detail="invalid credentials")
        if user.status != "active":
            raise HTTPException(status_code=401, detail="account inactive")
        record_success(session, user)
        _set_cookie(response, issue_token(user))
        return {"username": user.username, "display_name": user.display_name, "role": user.role}
    finally:
        session.close()


@router.post("/first-login")
def first_login(body: FirstLoginBody, response: Response):
    try:
        pin_hash = hash_pin(body.pin)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        user = redeem_setup_code(body.username, body.code, pin_hash)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if body.display_name:
        session = get_session()
        try:
            row = session.get(User, user.id)
            row.display_name = body.display_name
            session.commit()
        finally:
            session.close()
    _set_cookie(response, issue_token(user))
    # The trust line renders on the client's next screen: "Your sessions are
    # private to you. Lessons you approve become shared team knowledge."
    return {"username": user.username, "first_run": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("zagent_token")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "role": user.role,
    }
