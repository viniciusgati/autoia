"""Testes da detecção de ecossistema do projeto (project.py)."""

from __future__ import annotations

import subprocess

from app.worker import gitops, project


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def test_detect_python(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "calc.py").write_text("def soma(a, b): return a + b\n")
    info = project.detect_project(str(tmp_path))
    assert "python" in info.lower()
    assert "pytest" in info


def test_detect_node(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
    info = project.detect_project(str(tmp_path))
    assert "node/npm" in info.lower()
    assert "npm test" in info


def test_detect_multiple(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pytest.ini").write_text("")
    info = project.detect_project(str(tmp_path))
    assert "node/npm" in info.lower()
    assert "python" in info.lower()


def test_detect_empty(tmp_path):
    assert project.detect_project(str(tmp_path)) == ""
    assert project.detect_project(str(tmp_path / "nao_existe")) == ""


def test_build_prompt_includes_project_info(settings):
    from app.db import make_engine, make_session_factory
    from app.main import create_app
    from app.models import Robot, Task
    from app.prompts import build_prompt

    app = create_app(settings)
    sf = make_session_factory(make_engine(settings.database_url))
    with sf() as s:
        robot = s.query(Robot).filter(Robot.name == "tester").one()
        task = Task(title="t", description="d")
        prompt = build_prompt(robot, task, "", "main", project_info="Linguagem/ecossistema: python")
        assert "Linguagem/ecossistema" in prompt
        assert "Evidência" in prompt
        assert "VERIFICA e REPORTA" in prompt


def test_build_agents_md_includes_project_info():
    md = project.build_agents_md("Linguagem/ecossistema: python")
    assert "Tecnologia deste projeto" in md
    assert "python" in md
    assert "Não introduza outra linguagem" in md


def test_build_agents_md_includes_database_rule():
    md = project.build_agents_md("")
    assert "## Banco de dados" in md
    assert "PostgreSQL" in md
    assert "NÃO use SQLite" in md


def test_build_agents_md_custom_database_rule():
    md = project.build_agents_md("", db_rule="- Use MySQL obrigatoriamente.")
    assert "MySQL" in md
    assert "NÃO use SQLite" not in md


def test_build_agents_md_fallback_without_stack():
    md = project.build_agents_md("")
    assert "Nenhum ecossistema específico" in md
    assert "Não introduza outra linguagem" in md


def test_ensure_agents_md_written_and_untracked(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    project.ensure_agents_md(dest, "Linguagem/ecossistema: python")

    md_path = tmp_path / "clone" / "AGENTS.md"
    assert md_path.exists()
    md = md_path.read_text()
    assert "python" in md
    assert "PostgreSQL" in md  # regra de banco padrão entra no arquivo gerado
    # não versionado: ausente do índice e invisível no status, mesmo após add -A
    assert _git(dest, "ls-files", "AGENTS.md").stdout.strip() == ""
    assert _git(dest, "status", "--porcelain").stdout.strip() == ""
    _git(dest, "add", "-A")
    assert _git(dest, "status", "--porcelain").stdout.strip() == ""


def test_ensure_agents_md_custom_database_rule(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    project.ensure_agents_md(dest, "info", db_rule="- Use MySQL obrigatoriamente.")

    md = (tmp_path / "clone" / "AGENTS.md").read_text()
    assert "MySQL" in md
    assert "NÃO use SQLite" not in md


def test_ensure_agents_md_idempotent(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    project.ensure_agents_md(dest, "info")
    project.ensure_agents_md(dest, "info")

    exclude = (tmp_path / "clone" / ".git" / "info" / "exclude").read_text()
    assert exclude.count("AGENTS.md") == 1


def test_ensure_agents_md_preserves_tracked(bare_repo, tmp_path):
    """AGENTS.md já versionado pelo repositório prevalece (não é sobrescrito)."""
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)
    (tmp_path / "clone" / "AGENTS.md").write_text("# AGENTS.md do próprio repo\n")
    _git(dest, "config", "user.email", "t@test")
    _git(dest, "config", "user.name", "Test")
    _git(dest, "add", "AGENTS.md")
    _git(dest, "commit", "-m", "agents")

    project.ensure_agents_md(dest, "Linguagem/ecossistema: python")

    assert (tmp_path / "clone" / "AGENTS.md").read_text() == "# AGENTS.md do próprio repo\n"
