"""Testes da detecção de ecossistema do projeto (project.py)."""

from __future__ import annotations

from app.worker import project


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
