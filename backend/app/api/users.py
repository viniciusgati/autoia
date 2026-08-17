"""Gestão de usuários — restrita a admin global (página `/users` no frontend).

O admin gerencia os usuários via API ou pela tela de Usuários: listar, criar e
editar (nome, e-mail, senha, papel e ativação). O bootstrap (`/api/auth/register`)
cria apenas o primeiro usuário; todo o restante passa por aqui.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from .auth import hash_password
from .deps import get_session, require_admin

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(
    session: Session = Depends(get_session),
    _admin: User | None = Depends(require_admin),
):
    return session.query(User).order_by(User.id).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    data: UserCreate,
    session: Session = Depends(get_session),
    _admin: User | None = Depends(require_admin),
):
    if session.query(User).filter(User.email == data.email).first():
        raise HTTPException(409, f"e-mail '{data.email}' já cadastrado")
    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    data: UserUpdate,
    session: Session = Depends(get_session),
    _admin: User | None = Depends(require_admin),
):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "usuário não encontrado")
    fields = data.model_dump(exclude_unset=True)
    if "password" in fields:
        fields["password_hash"] = hash_password(fields.pop("password"))
    for key, value in fields.items():
        setattr(user, key, value)
    session.commit()
    session.refresh(user)
    return user
