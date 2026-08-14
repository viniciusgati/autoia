"""Endpoints do fluxo de CHAMADOS (atendimento) — Projeto > Épico > Chamado.

Subsistema novo, paralelo às tasks: o chamado percorre etapas de um catálogo
(`ChamadoStageType`), cada etapa expõe ferramentas de apoio (LLM com acesso ao
fonte) e um fechamento avaliado por robô. A execução fica a cargo do
`chamado-worker` (processo separado) via `pending_action` nas etapas.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import chamado_prompts
from ..config import Settings
from ..db import utcnow
from ..models import (
    CHAMADO_EM_ANDAMENTO,
    CHAMADO_STAGE_AGUARDANDO,
    CHAMADO_STAGE_ATIVA,
    CHAMADO_STAGE_EXECUTANDO,
    CHAMADO_STAGE_FECHADA,
    Chamado,
    ChamadoMessage,
    ChamadoStage,
    ChamadoStageType,
    Epic,
    Project,
)
from ..schemas import (
    ChamadoCreate,
    ChamadoMessageOut,
    ChamadoMessageResponse,
    ChamadoOut,
    ChamadoStageOut,
    ChamadoStageTypeCreate,
    ChamadoStageTypeOut,
    ChamadoUpdate,
    ChamadoWorkspaceOut,
    EpicCreate,
    EpicDetailOut,
    EpicOut,
    EpicUpdate,
    ProjectCreate,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdate,
    ToolInfoOut,
    ToolRunRequest,
)
from ..worker import chamado_runner
from .deps import get_repository_or_404, get_session, get_settings, require_auth

log = logging.getLogger("autoia.api")

projects_router = APIRouter(prefix="/api/projects", tags=["projects"])
epics_router = APIRouter(prefix="/api/epics", tags=["epics"])
stage_types_router = APIRouter(prefix="/api/chamado-stage-types", tags=["chamado-stage-types"])
chamados_router = APIRouter(prefix="/api/chamados", tags=["chamados"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "projeto não encontrado")
    return project


def _epic_or_404(session: Session, epic_id: int) -> Epic:
    epic = session.get(Epic, epic_id)
    if epic is None:
        raise HTTPException(404, "épico não encontrado")
    return epic


def _chamado_or_404(session: Session, chamado_id: int) -> Chamado:
    chamado = session.get(Chamado, chamado_id)
    if chamado is None:
        raise HTTPException(404, "chamado não encontrado")
    return chamado


def _stage_out(st: ChamadoStage) -> ChamadoStageOut:
    return ChamadoStageOut(
        id=st.id,
        chamado_id=st.chamado_id,
        stage_type_id=st.stage_type_id,
        position=st.position,
        status=st.status,
        pending_action=st.pending_action,
        decision=st.decision,
        result=st.result,
        error=st.error,
        attempt=st.attempt,
        started_at=st.started_at,
        finished_at=st.finished_at,
        stage_type_name=st.stage_type.name if st.stage_type else None,
    )


def _chamado_out(c: Chamado) -> ChamadoOut:
    return ChamadoOut(
        id=c.id,
        repository_id=c.repository_id,
        project_id=c.project_id,
        epic_id=c.epic_id,
        title=c.title,
        description=c.description,
        workflow_status=c.workflow_status,
        status=c.status,
        executor=c.executor,
        budget_limit=c.budget_limit,
        cost_spent=c.cost_spent,
        error=c.error,
        responsible_id=c.responsible_id,
        created_at=c.created_at,
        updated_at=c.updated_at,
        stages=[_stage_out(st) for st in c.stages],
    )


def _current_stage(chamado: Chamado) -> ChamadoStage | None:
    open_stages = [st for st in chamado.stages if st.status != CHAMADO_STAGE_FECHADA]
    if not open_stages:
        return None
    return max(open_stages, key=lambda st: st.position)


def _project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        repository_id=p.repository_id,
        name=p.name,
        description=p.description,
        status=p.status,
        summary=p.summary,
        generating=chamado_runner.is_content_generating("project", p.id),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _epic_out(e: Epic) -> EpicOut:
    return EpicOut(
        id=e.id,
        project_id=e.project_id,
        name=e.name,
        description=e.description,
        status=e.status,
        scope=e.scope,
        summary=e.summary,
        generating=(
            chamado_runner.is_content_generating("epic_scope", e.id)
            or chamado_runner.is_content_generating("epic_summary", e.id)
        ),
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


def _tools_for(stage: ChamadoStage | None) -> list[ToolInfoOut]:
    if stage is None or stage.stage_type is None:
        return []
    tools: list[ToolInfoOut] = []
    for key in stage.stage_type.allowed_tools or []:
        preset = chamado_prompts.TOOL_PRESETS.get(key)
        if preset:
            tools.append(ToolInfoOut(key=key, label=preset["label"], description=preset["description"]))
    return tools


def _resolve_stage_type(session: Session, repo_id: int, name: str) -> ChamadoStageType | None:
    return (
        session.query(ChamadoStageType)
        .filter(ChamadoStageType.repository_id == repo_id, ChamadoStageType.name == name)
        .first()
    ) or (
        session.query(ChamadoStageType)
        .filter(ChamadoStageType.repository_id.is_(None), ChamadoStageType.name == name)
        .first()
    )


def _initial_stage_type(session: Session, repo_id: int, override_id: int | None = None) -> ChamadoStageType:
    if override_id is not None:
        st = session.get(ChamadoStageType, override_id)
        if st is None or (st.repository_id not in (None, repo_id)):
            raise HTTPException(400, "tipo de etapa inicial inválido para este projeto")
        return st
    st = (
        session.query(ChamadoStageType)
        .filter(
            or_(
                ChamadoStageType.repository_id == repo_id,
                ChamadoStageType.repository_id.is_(None),
            ),
            ChamadoStageType.is_initial.is_(True),
        )
        .order_by(ChamadoStageType.repository_id.is_(None))
        .first()
    )
    if st is None:
        raise HTTPException(400, "nenhum tipo de etapa inicial cadastrado para este projeto")
    return st


def _message_out(m: ChamadoMessage) -> ChamadoMessageOut:
    return ChamadoMessageOut(
        id=m.id,
        chamado_id=m.chamado_id,
        stage_id=m.stage_id,
        seq=m.seq,
        ts=m.ts,
        kind=m.kind,
        payload=m.payload,
        cost=m.cost,
    )


# ── Projetos ─────────────────────────────────────────────────────────────────

@projects_router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    data: ProjectCreate,
    session: Session = Depends(get_session),
    _user=Depends(require_auth),
):
    get_repository_or_404(session, data.repository_id)
    project = Project(
        repository_id=data.repository_id,
        name=data.name,
        description=data.description,
        status=data.status,
    )
    session.add(project)
    session.commit()
    return _project_out(project)


@projects_router.get("", response_model=list[ProjectOut])
def list_projects(
    repository_id: int | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(Project)
    if repository_id is not None:
        q = q.filter(Project.repository_id == repository_id)
    return [_project_out(p) for p in q.order_by(Project.id.desc()).limit(200).all()]


@projects_router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(project_id: int, session: Session = Depends(get_session)):
    project = _project_or_404(session, project_id)
    return ProjectDetailOut(
        id=project.id,
        repository_id=project.repository_id,
        name=project.name,
        description=project.description,
        status=project.status,
        summary=project.summary,
        generating=chamado_runner.is_content_generating("project", project.id),
        created_at=project.created_at,
        updated_at=project.updated_at,
        epics=[_epic_out(e) for e in project.epics],
        chamado_count=len(project.chamados),
    )


@projects_router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    data: ProjectUpdate,
    session: Session = Depends(get_session),
):
    project = _project_or_404(session, project_id)
    if data.name is not None:
        project.name = data.name
    if data.description is not None:
        project.description = data.description
    if data.status is not None:
        project.status = data.status
    session.commit()
    return _project_out(project)


@projects_router.delete("/{project_id}")
def delete_project(project_id: int, session: Session = Depends(get_session)):
    project = _project_or_404(session, project_id)
    for chamado in project.chamados:
        chamado.project_id = None
    session.delete(project)  # épicos em cascata
    session.commit()
    return {"ok": True}


@projects_router.post("/{project_id}/summary/regenerate")
def regenerate_project_summary(
    project_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    request: Request = None,
):
    project = _project_or_404(session, project_id)
    started = chamado_runner.start_content_generation(
        settings, request.app.state.Session, "project", project.id
    )
    return {"started": started}


# ── Épicos ───────────────────────────────────────────────────────────────────

@epics_router.post("", response_model=EpicOut, status_code=201)
def create_epic(
    data: EpicCreate,
    session: Session = Depends(get_session),
    _user=Depends(require_auth),
):
    _project_or_404(session, data.project_id)
    epic = Epic(
        project_id=data.project_id,
        name=data.name,
        description=data.description,
        status=data.status,
    )
    session.add(epic)
    session.commit()
    return _epic_out(epic)


@epics_router.get("", response_model=list[EpicOut])
def list_epics(
    project_id: int | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(Epic)
    if project_id is not None:
        q = q.filter(Epic.project_id == project_id)
    return [_epic_out(e) for e in q.order_by(Epic.id.desc()).limit(200).all()]


@epics_router.get("/{epic_id}", response_model=EpicDetailOut)
def get_epic(epic_id: int, session: Session = Depends(get_session)):
    epic = _epic_or_404(session, epic_id)
    data = _epic_out(epic)
    return EpicDetailOut(**data.model_dump(), chamado_count=len(epic.chamados))


@epics_router.patch("/{epic_id}", response_model=EpicOut)
def update_epic(
    epic_id: int,
    data: EpicUpdate,
    session: Session = Depends(get_session),
):
    epic = _epic_or_404(session, epic_id)
    if data.name is not None:
        epic.name = data.name
    if data.description is not None:
        epic.description = data.description
    if data.status is not None:
        epic.status = data.status
    session.commit()
    return _epic_out(epic)


@epics_router.delete("/{epic_id}")
def delete_epic(epic_id: int, session: Session = Depends(get_session)):
    epic = _epic_or_404(session, epic_id)
    for chamado in epic.chamados:
        chamado.epic_id = None
    session.delete(epic)
    session.commit()
    return {"ok": True}


@epics_router.post("/{epic_id}/scope/regenerate")
def regenerate_epic_scope(
    epic_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    request: Request = None,
):
    epic = _epic_or_404(session, epic_id)
    started = chamado_runner.start_content_generation(
        settings, request.app.state.Session, "epic_scope", epic.id
    )
    return {"started": started}


@epics_router.post("/{epic_id}/summary/regenerate")
def regenerate_epic_summary(
    epic_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    request: Request = None,
):
    epic = _epic_or_404(session, epic_id)
    started = chamado_runner.start_content_generation(
        settings, request.app.state.Session, "epic_summary", epic.id
    )
    return {"started": started}


# ── Catálogo de tipos de etapa ──────────────────────────────────────────────

@stage_types_router.get("", response_model=list[ChamadoStageTypeOut])
def list_stage_types(
    repository_id: int | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(ChamadoStageType)
    if repository_id is not None:
        q = q.filter(
            or_(
                ChamadoStageType.repository_id == repository_id,
                ChamadoStageType.repository_id.is_(None),
            )
        )
    return q.order_by(ChamadoStageType.id).all()


@stage_types_router.post("", response_model=ChamadoStageTypeOut, status_code=201)
def create_stage_type(
    data: ChamadoStageTypeCreate,
    session: Session = Depends(get_session),
    _user=Depends(require_auth),
):
    if data.repository_id is not None:
        get_repository_or_404(session, data.repository_id)
    exists = (
        session.query(ChamadoStageType)
        .filter(
            ChamadoStageType.repository_id.is_(data.repository_id),
            ChamadoStageType.name == data.name,
        )
        .first()
    )
    if exists:
        raise HTTPException(400, "tipo de etapa já existe neste escopo")
    st = ChamadoStageType(**data.model_dump())
    session.add(st)
    session.commit()
    return st


# ── Chamados ────────────────────────────────────────────────────────────────

@chamados_router.get("/worker/status")
def chamado_worker_status(
    settings: Settings = Depends(get_settings),
    _user=Depends(require_auth),
):
    hb = os.path.join(settings.workspace_dir, chamado_runner.CHAMADO_HEARTBEAT_FILE)
    try:
        mtime = os.path.getmtime(hb)
        age = time.time() - mtime
    except OSError:
        return {"alive": False, "last_heartbeat_sec": None}
    return {"alive": age < 15, "last_heartbeat_sec": round(age, 1)}


@chamados_router.post("", response_model=ChamadoOut, status_code=201)
def create_chamado(
    data: ChamadoCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _user=Depends(require_auth),
):
    get_repository_or_404(session, data.repository_id)
    project_id = data.project_id
    epic = None
    if data.epic_id is not None:
        epic = session.get(Epic, data.epic_id)
        if epic is None:
            raise HTTPException(404, "épico não encontrado")
        if epic.project.repository_id != data.repository_id:
            raise HTTPException(400, "épico não pertence a este projeto/repositório")
        project_id = epic.project_id
    if project_id is not None:
        project = _project_or_404(session, project_id)
        if project.repository_id != data.repository_id:
            raise HTTPException(400, "projeto não pertence a este repositório")

    initial = _initial_stage_type(session, data.repository_id, data.initial_stage_type_id)
    chamado = Chamado(
        repository_id=data.repository_id,
        project_id=project_id,
        epic_id=epic.id if epic else data.epic_id,
        title=data.title,
        description=data.description,
        executor=data.executor,
        budget_limit=data.budget_limit if data.budget_limit is not None else settings.task_budget,
        workflow_status=initial.name,
        status=CHAMADO_EM_ANDAMENTO,
    )
    chamado.stages.append(
        ChamadoStage(
            stage_type_id=initial.id,
            position=0,
            status=CHAMADO_STAGE_ATIVA,
        )
    )
    session.add(chamado)
    session.commit()
    return _chamado_out(chamado)


@chamados_router.get("", response_model=list[ChamadoOut])
def list_chamados(
    repository_id: int | None = None,
    project_id: int | None = None,
    epic_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(Chamado)
    if repository_id is not None:
        q = q.filter(Chamado.repository_id == repository_id)
    if project_id is not None:
        q = q.filter(Chamado.project_id == project_id)
    if epic_id is not None:
        q = q.filter(Chamado.epic_id == epic_id)
    if status is not None:
        q = q.filter(Chamado.status == status)
    chamados = q.order_by(Chamado.id.desc()).limit(200).all()
    return [_chamado_out(c) for c in chamados]


@chamados_router.get("/{chamado_id}", response_model=ChamadoOut)
def get_chamado(chamado_id: int, session: Session = Depends(get_session)):
    return _chamado_out(_chamado_or_404(session, chamado_id))


@chamados_router.patch("/{chamado_id}", response_model=ChamadoOut)
def update_chamado(
    chamado_id: int,
    data: ChamadoUpdate,
    session: Session = Depends(get_session),
):
    chamado = _chamado_or_404(session, chamado_id)
    if data.title is not None:
        chamado.title = data.title
    if data.description is not None:
        chamado.description = data.description
    if data.executor is not None:
        chamado.executor = data.executor
    if data.project_id is not None:
        project = _project_or_404(session, data.project_id)
        if project.repository_id != chamado.repository_id:
            raise HTTPException(400, "projeto não pertence a este repositório")
        chamado.project_id = project.id
        if chamado.epic_id is not None:
            epic = session.get(Epic, chamado.epic_id)
            if epic is not None and epic.project_id != project.id:
                chamado.epic_id = None
    if data.epic_id is not None:
        epic = session.get(Epic, data.epic_id)
        if epic is None:
            raise HTTPException(404, "épico não encontrado")
        if epic.project.repository_id != chamado.repository_id:
            raise HTTPException(400, "épico não pertence a este repositório")
        chamado.epic_id = epic.id
        chamado.project_id = epic.project_id
    session.commit()
    return _chamado_out(chamado)


@chamados_router.delete("/{chamado_id}")
def delete_chamado(chamado_id: int, session: Session = Depends(get_session)):
    chamado = _chamado_or_404(session, chamado_id)
    busy = [
        st
        for st in chamado.stages
        if st.status in (CHAMADO_STAGE_AGUARDANDO, CHAMADO_STAGE_EXECUTANDO)
    ]
    if busy:
        raise HTTPException(400, "chamado com ação em andamento — aguarde concluir")
    session.delete(chamado)
    session.commit()
    return {"ok": True}


@chamados_router.get("/{chamado_id}/messages", response_model=list[ChamadoMessageOut])
def list_messages(chamado_id: int, session: Session = Depends(get_session)):
    chamado = _chamado_or_404(session, chamado_id)
    msgs = (
        session.query(ChamadoMessage)
        .filter(ChamadoMessage.chamado_id == chamado_id)
        .order_by(ChamadoMessage.seq)
        .all()
    )
    return [_message_out(m) for m in msgs]


@chamados_router.get("/{chamado_id}/workspace", response_model=ChamadoWorkspaceOut)
def chamado_workspace(chamado_id: int, session: Session = Depends(get_session)):
    chamado = _chamado_or_404(session, chamado_id)
    stage = _current_stage(chamado)
    msgs = (
        session.query(ChamadoMessage)
        .filter(ChamadoMessage.chamado_id == chamado_id)
        .order_by(ChamadoMessage.seq)
        .all()
    )
    return ChamadoWorkspaceOut(
        chamado=_chamado_out(chamado),
        stages=[_stage_out(st) for st in chamado.stages],
        messages=[_message_out(m) for m in msgs],
        current_stage=_stage_out(stage) if stage else None,
        tools=_tools_for(stage),
        close_options=list(stage.stage_type.close_options or []) if stage and stage.stage_type else [],
    )


@chamados_router.post("/{chamado_id}/tools/{tool}", response_model=ChamadoMessageResponse)
def run_tool(
    chamado_id: int,
    tool: str,
    data: ToolRunRequest,
    session: Session = Depends(get_session),
):
    """Encaminha o pedido do usuário para o worker rodar a ferramenta da etapa."""
    chamado = _chamado_or_404(session, chamado_id)
    stage = _current_stage(chamado)
    if stage is None:
        raise HTTPException(400, "chamado sem etapa ativa")
    if stage.status == CHAMADO_STAGE_EXECUTANDO:
        raise HTTPException(400, "uma ação já está em andamento nesta etapa")
    allowed = list(stage.stage_type.allowed_tools or [])
    if tool not in allowed or tool not in chamado_prompts.TOOL_PRESETS:
        raise HTTPException(400, f"ferramenta '{tool}' não disponível nesta etapa")
    max_seq = (
        session.query(ChamadoMessage.seq)
        .filter(ChamadoMessage.stage_id == stage.id)
        .order_by(ChamadoMessage.seq.desc())
        .first()
    ) or 0
    session.add(
        ChamadoMessage(
            chamado_id=chamado.id,
            stage_id=stage.id,
            seq=max_seq + 1,
            kind="user",
            payload={"tool": tool, "text": data.text},
        )
    )
    stage.status = CHAMADO_STAGE_AGUARDANDO
    stage.pending_action = f"tool:{tool}"
    stage.error = None
    chamado.status = CHAMADO_EM_ANDAMENTO
    session.commit()
    return ChamadoMessageResponse(ok=True, message="ferramenta encaminhada para execução")


@chamados_router.post("/{chamado_id}/close", response_model=ChamadoMessageResponse)
def close_stage(
    chamado_id: int,
    session: Session = Depends(get_session),
):
    """Dispara a avaliação de fechamento da etapa atual (robô decide a transição)."""
    chamado = _chamado_or_404(session, chamado_id)
    stage = _current_stage(chamado)
    if stage is None:
        raise HTTPException(400, "chamado sem etapa ativa")
    if stage.status == CHAMADO_STAGE_EXECUTANDO:
        raise HTTPException(400, "uma ação já está em andamento nesta etapa")
    if not (stage.stage_type.close_options or []):
        raise HTTPException(400, "esta etapa não define fechamento (close_options vazio)")
    stage.status = CHAMADO_STAGE_AGUARDANDO
    stage.pending_action = "evaluate"
    stage.error = None
    chamado.status = CHAMADO_EM_ANDAMENTO
    session.commit()
    return ChamadoMessageResponse(ok=True, message="avaliação de fechamento encaminhada")


@chamados_router.post("/{chamado_id}/cancel-action", response_model=ChamadoMessageResponse)
def cancel_action(
    chamado_id: int,
    session: Session = Depends(get_session),
):
    """Desfaz uma ação pendente (etapa `aguardando`): limpa o `pending_action` e
    devolve a etapa a `ativa`, sem executar. Para ação `executando` (já reclamada
    pelo worker) o fluxo correto é reiniciar o `chamado-worker` (recupera órfãos)."""
    chamado = _chamado_or_404(session, chamado_id)
    stage = _current_stage(chamado)
    if stage is None:
        raise HTTPException(400, "chamado sem etapa ativa")
    if stage.status == CHAMADO_STAGE_EXECUTANDO:
        raise HTTPException(400, "a ação já foi reclamada pelo worker; reinicie o chamado-worker para recuperar órfãos")
    if stage.status != CHAMADO_STAGE_AGUARDANDO or not stage.pending_action:
        raise HTTPException(400, "nenhuma ação pendente para cancelar")
    stage.status = CHAMADO_STAGE_ATIVA
    stage.pending_action = None
    stage.error = None
    session.commit()
    return ChamadoMessageResponse(ok=True, message="ação pendente cancelada")


@chamados_router.post("/{chamado_id}/cancel", response_model=ChamadoOut)
def cancel_chamado(
    chamado_id: int,
    session: Session = Depends(get_session),
):
    """Cancelamento MANUAL do chamado pelo usuário (sem avaliação de robô): fecha a
    etapa atual e marca o chamado como `cancelado`."""
    chamado = _chamado_or_404(session, chamado_id)
    if chamado.status in ("cancelado", "concluido", "respondido"):
        raise HTTPException(400, f"chamado já encerrado como {chamado.status}")
    stage = _current_stage(chamado)
    if stage is not None:
        if stage.status == CHAMADO_STAGE_EXECUTANDO:
            raise HTTPException(400, "ação em execução no worker; aguarde ou reinicie o chamado-worker")
        stage.status = CHAMADO_STAGE_FECHADA
        stage.pending_action = None
        stage.decision = "cancelado_manualmente"
        stage.result = "Cancelado manualmente pelo usuário."
        stage.finished_at = utcnow()
    chamado.status = "cancelado"
    chamado.workflow_status = f"{stage.stage_type.name if stage and stage.stage_type else '?'} (cancelado)"
    chamado.error = None
    session.commit()
    return _chamado_out(chamado)
