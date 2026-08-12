"""Fixtures compartilhadas dos testes."""

from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

from app.config import Settings

# Regras de veredicto embutidas no script do kimi fake (processo separado).
VERDICT_RULES = {
    "ready_pass": (
        "if 'VEREDICTO' in prompt.upper() and 'READY' in prompt:\n"
        "    v = 'READY\\nSUMMARY: historia ok'\n"
        "elif 'VEREDICTO' in prompt.upper() and 'PASS' in prompt:\n"
        "    v = 'PASS\\nSUMMARY: testes ok'\n"
        "else:\n    v = None"
    ),
    "fail": (
        "if 'VEREDICTO' in prompt.upper():\n    v = 'FAIL\\nSUMMARY: testes falharam'\nelse:\n    v = None"
    ),
    "needs_work": (
        "if 'VEREDICTO' in prompt.upper():\n    v = 'NEEDS_WORK\\nSUMMARY: historia ambigua'\nelse:\n    v = None"
    ),
    "pm_retry": (
        "if 'DECISÃO' in prompt:\n    v = 'DECISÃO: retry 3\\nMOTIVO: corrigível'\nelse:\n    v = None"
    ),
    "pm_retry_post": (
        "if 'DECISÃO' in prompt:\n    v = 'DECISÃO: retry 6\\nMOTIVO: re-testar na main'\nelse:\n    v = None"
    ),
    "pm_continue": (
        "if 'DECISÃO' in prompt:\n    v = 'DECISÃO: continuar\\nMOTIVO: progresso real'\nelse:\n    v = None"
    ),
    "pm_escalate": (
        "if 'DECISÃO' in prompt:\n    v = 'DECISÃO: escalar\\nMOTIVO: precisa de humano'\nelse:\n    v = None"
    ),
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "workspaces"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        kimi_bin="kimi",  # não roda de verdade nos testes de API
        run_timeout=30,
        max_identical_calls=3,
        max_attempts=3,
        task_budget=1.0,
        cost_per_interaction=0.01,
        pm_budget_topup=5.0,
        max_pm_decisions=0,  # PM desligado por padrão; testes de PM ativam explicitamente
        step_mission=False,  # missão LLM desligada por padrão; testes ativam explicitamente
        auth_enabled=False,  # suíte antiga sem sessão; testes de auth ativam explicitamente
    )


@pytest.fixture
def fake_kimi(tmp_path):
    """Fábrica de binário kimi fake: emite linhas JSONL e opcionalmente escreve
    autoia_verdict.txt conforme a regra (chave de VERDICT_RULES)."""

    def _make(
        lines: list[dict],
        verdict: str | None = None,
        write_file: str | None = None,
        write_content: str | None = None,
    ) -> str:
        counter = len(list(tmp_path.glob("fake_kimi_*")))
        script = tmp_path / f"fake_kimi_{counter}"
        body = ",\n".join(json.dumps(line) for line in lines)
        verdict_code = ""
        if verdict:
            rule = VERDICT_RULES[verdict]
        verdict_code = ""
        if verdict:
            rule = VERDICT_RULES[verdict]
            verdict_code = (
                "import os\n"
                "prompt = ''\n"
                "if '-p' in sys.argv:\n"
                "    prompt = sys.argv[sys.argv.index('-p') + 1]\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'run':\n"
                "    prompt = sys.argv[2]\n"
                + rule
                + "\nif v:\n    with open('autoia_verdict.txt', 'w') as f:\n        f.write(v)\n"
            )
        content = write_content if write_content is not None else "conteudo\n"
        write_code = (
            f"with open({write_file!r}, 'w') as f:\n    f.write({content!r})\n"
            if write_file
            else ""
        )
        script.write_text(
            f"#!/usr/bin/env python3\n"
            f"import sys, json\n"
            f"for line in [\n{body}\n]:\n"
            f"    print(json.dumps(line))\n"
            f"    sys.stdout.flush()\n"
            + write_code
            + verdict_code
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    return _make


@pytest.fixture
def flow(settings, bare_repo):
    """App + worker session factory + tarefa iniciada no repo, pronta para o worker."""
    from fastapi.testclient import TestClient

    from app.db import make_engine, make_session_factory
    from app.main import create_app

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    response = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    ).json()
    client.post(f"/api/tasks/{task['id']}/start")
    return {"settings": settings, "session_factory": session_factory, "task": task, "client": client}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


@pytest.fixture
def bare_repo(tmp_path) -> str:
    """Repo git local (bare) com um commit inicial — remote para os testes."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-b", "main")
    _git(src, "config", "user.email", "t@test")
    _git(src, "config", "user.name", "Test")
    (src / "README.md").write_text("# repo\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    bare = tmp_path / "repo.git"
    _git(tmp_path, "clone", "--bare", str(src), str(bare))
    return str(bare)

