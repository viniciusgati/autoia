"""Testes da métrica de mudança de arquitetura/deploy (arch_metric.py)."""

from __future__ import annotations

from app.worker import gitops
from app.worker.arch_metric import compute_arch_metric
from app.worker.gitops import DiffChange


def _change(path: str, status: str = "A", added: int = 1, deleted: int = 0) -> DiffChange:
    return DiffChange(status=status, path=path, added=added, deleted=deleted)


def test_ordinary_change_is_low():
    metric = compute_arch_metric([_change("hello.py"), _change("README.md", "M", 1, 1)])
    assert metric.level == "baixo"
    assert metric.score < 30


def test_dockerfile_add_is_high():
    metric = compute_arch_metric([_change("Dockerfile", added=10)])
    assert metric.level == "alto"
    assert metric.score >= 60
    assert any("Dockerfile" in r for r in metric.reasons)


def test_ci_and_compose_are_capped_high():
    changes = [
        _change(".github/workflows/ci.yml", added=20),
        _change("compose.yaml", added=30),
    ]
    metric = compute_arch_metric(changes)
    assert metric.score == 100
    assert metric.level == "alto"


def test_makefile_modify_is_medium():
    metric = compute_arch_metric([_change("Makefile", "M", 5, 3)])
    assert metric.level == "médio"
    assert metric.score >= 30 and metric.score < 60


def test_volume_adds_points():
    metric = compute_arch_metric([_change("main.py", "M", 1500, 1500)])
    assert metric.score >= 40  # volume >= 3000 linhas -> +4 pontos
    assert metric.level == "médio"
    assert any("volume" in r for r in metric.reasons)


def test_structural_change_adds_points():
    changes = [_change(f"mod{i}.py", added=5) for i in range(10)]
    metric = compute_arch_metric(changes)
    assert metric.level == "médio"
    assert any("estrutural" in r for r in metric.reasons)


def test_diff_changes_git(bare_repo, tmp_path):
    """diff_changes com git real: status A + linhas corretas, e métrica alta."""
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)
    gitops.ensure_task_branch(dest, "autoia/task-1", "main")
    (tmp_path / "clone" / "Dockerfile").write_text("FROM python:3.12\n")
    (tmp_path / "clone" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    gitops.commit_all(dest, "adiciona docker e pyproject")

    changes = gitops.diff_changes(dest, "main", "autoia/task-1")
    by_path = {c.path: c for c in changes}
    assert set(by_path) == {"Dockerfile", "pyproject.toml"}
    assert by_path["Dockerfile"].status == "A"
    assert by_path["Dockerfile"].added == 1
    assert by_path["pyproject.toml"].status == "A"
    assert by_path["pyproject.toml"].added == 2

    metric = compute_arch_metric(changes)
    assert metric.level == "alto"
