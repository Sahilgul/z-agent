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
    remember: bool = False


class FirstLoginBody(BaseModel):
    username: str
    code: str
    pin: str
    display_name: str | None = None


# M-82: "remember me" actually changes cookie persistence. When remember
# is false the cookie is a session cookie (no max_age -> expires when the
# browser closes); when true it persists for 14 days. The old code always
# set max_age=14d, so the checkbox was dead.
_REMEMBER_MAX_AGE = 60 * 60 * 24 * 14


def _set_cookie(response: Response, token: str, remember: bool) -> None:
    response.set_cookie(
        "collegium_token", token, httponly=True, samesite="lax",
        secure=False,  # plain HTTP; flip once TLS terminates ahead
        max_age=_REMEMBER_MAX_AGE if remember else None,
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
        _set_cookie(response, issue_token(user), body.remember)
        return {"username": user.username, "display_name": user.display_name, "role": user.role}
    finally:
        session.close()


@router.post("/first-login")
def first_login(body: FirstLoginBody, response: Response):
    try:
        pin_hash = hash_pin(body.pin)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # M-31: the 8-digit setup code was brute-forceable — first-login had NO
    # rate limiting. Reuse the login lockout: check lockout before the attempt,
    # then record a failed attempt on a bad code so MAX_FAILED_ATTEMPTS locks
    # the account for LOCKOUT_MINUTES (same posture as the PIN login).
    pre_session = get_session()
    try:
        pre = pre_session.query(User).filter_by(username=body.username).one_or_none()
        if pre is not None:
            check_lockout(pre)
    finally:
        pre_session.close()
    try:
        user = redeem_setup_code(body.username, body.code, pin_hash)
    except ValueError as exc:
        fail_session = get_session()
        try:
            row = fail_session.query(User).filter_by(username=body.username).one_or_none()
            if row is not None:
                record_failed_attempt(fail_session, row)
        finally:
            fail_session.close()
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if body.display_name:
        session = get_session()
        try:
            row = session.get(User, user.id)
            row.display_name = body.display_name
            session.commit()
        finally:
            session.close()
    # M-82: first-login has no "remember me" checkbox; preserve the old
    # 14-day persistent cookie so initial setup behavior is unchanged.
    _set_cookie(response, issue_token(user), remember=True)
    # The trust line renders on the client's next screen: "Your sessions are
    # private to you. Lessons you approve become shared team knowledge."
    return {"username": user.username, "first_run": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("collegium_token")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "role": user.role,
    }
