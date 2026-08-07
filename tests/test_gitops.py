"""Testes de integração do gitops com git real (em diretórios temporários)."""

from __future__ import annotations

import subprocess

import pytest

from app.worker import gitops


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def test_clone_and_resolve_branch(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)
    assert (tmp_path / "clone" / "README.md").exists()
    assert gitops.resolve_default_branch(dest, "main") == "main"


def test_branch_commit_merge_push(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    gitops.ensure_task_branch(dest, "autoia/task-1", "main")
    assert gitops.current_branch(dest) == "autoia/task-1"

    (tmp_path / "clone" / "hello.py").write_text("print('oi')\n")
    assert gitops.has_uncommitted_changes(dest)
    assert gitops.commit_all(dest, "adiciona hello.py") is True
    assert not gitops.has_uncommitted_changes(dest)
    # segunda chamada sem mudanças: nada a commitar
    assert gitops.commit_all(dest, "nada") is False

    result = gitops.merge_and_push(dest, "autoia/task-1", "main")
    assert result.ok is True
    assert not result.conflict

    # verifica que o push chegou no bare e a default está na frente
    head = _git(dest, "log", "--oneline", "-1", "main").stdout
    assert "adiciona hello.py" in head or "merge" in head
    # o arquivo existe no bare (via novo clone)
    dest2 = str(tmp_path / "clone2")
    gitops.clone(bare_repo, dest2)
    assert (tmp_path / "clone2" / "hello.py").exists()


def test_merge_conflict_detected(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    # branch A muda README
    gitops.ensure_task_branch(dest, "autoia/task-1", "main")
    (tmp_path / "clone" / "README.md").write_text("A\n")
    gitops.commit_all(dest, "branch muda README")

    # branch B (a default, via outro clone) também muda README e faz merge antes
    dest_b = str(tmp_path / "clone_b")
    gitops.clone(bare_repo, dest_b)
    gitops.ensure_task_branch(dest_b, "autoia/task-b", "main")
    (tmp_path / "clone_b" / "README.md").write_text("B\n")
    gitops.commit_all(dest_b, "outro muda README")
    res_b = gitops.merge_and_push(dest_b, "autoia/task-b", "main")
    assert res_b.ok

    # agora o merge de task-1 vai conflitar
    result = gitops.merge_and_push(dest, "autoia/task-1", "main")
    assert result.ok is False
    assert result.conflict is True
