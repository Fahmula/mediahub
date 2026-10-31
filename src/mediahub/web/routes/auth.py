"""First-run setup, login and logout."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mediahub.db.models import User
from mediahub.logging import get_logger
from mediahub.push import repository as push_repository
from mediahub.security import csrf
from mediahub.security.passwords import (
    DUMMY_HASH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
)
from mediahub.security.ratelimit import RateLimiter
from mediahub.security.sessions import (
    COOKIE_NAME,
    create_session,
    delete_session,
    session_hash,
)
from mediahub.web.deps import Ctx, Db, has_users, require_auth, session_lifetime
from mediahub.web.templating import cookie_secure, render

log = get_logger(__name__)
router = APIRouter(dependencies=[Depends(csrf.verify_csrf)])
# Logout verifies the *session* CSRF token, so the session must be loaded first.
logout_router = APIRouter(dependencies=[Depends(require_auth), Depends(csrf.verify_csrf)])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")
login_limiter = RateLimiter(limit=5, window_s=60)


def safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return "/"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _start_session(request: Request, ctx: Ctx, db: Session, user: User, dest: str) -> Response:
    token = create_session(
        db,
        user,
        lifetime=session_lifetime(ctx),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(dest, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(session_lifetime(ctx).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
    )
    response.delete_cookie(csrf.PRE_COOKIE)
    return response


@router.get("/setup")
def setup_form(request: Request, db: Db) -> Response:
    if has_users(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "pages/setup.html", {"min_length": MIN_PASSWORD_LENGTH})


@router.post("/setup")
def setup_submit(
    request: Request,
    ctx: Ctx,
    db: Db,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
) -> Response:
    if has_users(db):
        return RedirectResponse("/login", status_code=303)
    username = username.strip()
    errors: list[str] = []
    if not USERNAME_RE.match(username):
        errors.append("Username must be 3–64 letters, numbers, dots, dashes or underscores.")
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password != password_confirm:
        errors.append("Passwords don't match.")
    if errors:
        return render(
            request,
            "pages/setup.html",
            {"errors": errors, "username": username, "min_length": MIN_PASSWORD_LENGTH},
            status_code=422,
        )
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/login", status_code=303)
    log.info("admin_created", username=username)
    return _start_session(request, ctx, db, user, "/")


@router.get("/login")
def login_form(request: Request, db: Db, next: str | None = None) -> Response:
    if not has_users(db):
        return RedirectResponse("/setup", status_code=303)
    return render(request, "pages/login.html", {"next": safe_next(next)})


@router.post("/login")
def login_submit(
    request: Request,
    ctx: Ctx,
    db: Db,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str | None, Form()] = None,
) -> Response:
    ip = client_ip(request)
    dest = safe_next(next)
    if login_limiter.blocked(ip):
        log.warning("login_rate_limited", ip=ip)
        return render(
            request,
            "pages/login.html",
            {"errors": ["Too many attempts. Wait a minute and try again."], "next": dest},
            status_code=429,
        )
    user = db.scalar(select(User).where(User.username == username.strip()))
    valid = verify_password(user.password_hash if user else DUMMY_HASH, password)
    if user is None or not valid:
        login_limiter.hit(ip)
        log.warning("login_failed", ip=ip)
        return render(
            request,
            "pages/login.html",
            {"errors": ["Incorrect username or password."], "username": username, "next": dest},
            status_code=401,
        )
    login_limiter.reset(ip)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    log.info("login", username=user.username, ip=ip)
    return _start_session(request, ctx, db, user, dest)


@logout_router.post("/logout")
def logout(request: Request, db: Db) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        delete_session(db, token)
        push_repository.delete_for_session(db, session_hash(token))
    response = RedirectResponse("/login", status_code=303)
    if request.headers.get("hx-request") == "true":
        response = Response(status_code=204, headers={"HX-Redirect": "/login"})
    response.delete_cookie(COOKIE_NAME)
    return response
