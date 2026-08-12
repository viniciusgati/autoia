"""Autenticação: registro (bootstrap), login, logout e sessão atual.

Hash com a stdlib (pbkdf2_hmac "sha256", 200.000 iterações + salt) e comparação
com `hmac.compare_digest` — sem dependência nova. Sessão por cookie HttpOnly/
SameSite=Lax (`autoia_session`), Secure quando https ou `AUTOIA_COOKIE_SECURE=1`,
expirando em `AUTOIA_SESSION_DAYS` (default 30).

`POST /api/auth/register` é bootstrap: só aceito com a tabela `users` vazia — o
primeiro registro vira admin global; com usuários existentes retorna 403 (a
gestão passa a ser feita por admin via `api/users.py`).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import Session as AuthSession
from ..models import User
from ..schemas import LoginRequest, RegisterRequest, UserOut
from .deps import get_session, get_settings, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "autoia_session"
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Hash da senha no formato `pbkdf2_sha256$iteracoes$salt$hash` (hex)."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verificação em tempo constante da senha contra o hash armazenado."""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def create_session(session: Session, user_id: int, settings: Settings) -> AuthSession:
    """Cria a sessão persistida (token aleatório) e retorna o registro."""
    token = secrets.token_hex(32)
    auth_session = AuthSession(
        token=token,
        user_id=user_id,
        expires_at=utcnow() + timedelta(days=settings.session_days),
    )
    session.add(auth_session)
    session.commit()
    return auth_session


def set_session_cookie(
    response: Response, token: str, settings: Settings, request: Request
) -> None:
    """Grava o cookie de sessão (HttpOnly + SameSite=Lax; Secure se https/config)."""
    secure = settings.cookie_secure or request.url.scheme == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


@router.get("/config")
def auth_config(settings: Settings = Depends(get_settings)):
    """Estado da flag de autenticação — o frontend decide Login vs. app direto."""
    return {"enabled": settings.auth_enabled}


@router.post("/register", response_model=UserOut, status_code=201)
def register(
    data: RegisterRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """Bootstrap: primeiro registro (users vazio) vira admin global + sessão."""
    if session.query(User).count() > 0:
        raise HTTPException(
            403,
            "registro é bootstrap (usuários já existem) — use a gestão de usuários",
        )
    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        role="admin",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    auth_session = create_session(session, user.id, settings)
    set_session_cookie(response, auth_session.token, settings, request)
    return user


@router.post("/login", response_model=UserOut)
def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    user = session.query(User).filter(User.email == data.email).first()
    if user is None or not verify_password(data.password, user.password_hash or ""):
        raise HTTPException(401, "e-mail ou senha inválidos")
    if not user.active:
        raise HTTPException(403, "usuário inativo — fale com um administrador")
    auth_session = create_session(session, user.id, settings)
    set_session_cookie(response, auth_session.token, settings, request)
    return user


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Apaga a sessão (se houver) e limpa o cookie no cliente."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth_session = (
            session.query(AuthSession).filter(AuthSession.token == token).first()
        )
        if auth_session is not None:
            session.delete(auth_session)
            session.commit()
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User | None = Depends(require_auth)):
    """Usuário da sessão atual (401 sem sessão válida / auth OFF)."""
    if user is None:
        raise HTTPException(401, "não autenticado — faça login")
    return user
