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


def test_bootstrap_empty_repo(tmp_path):
    """Remote vazio (sem branch): bootstrap cria README + commit inicial e faz push."""
    empty = tmp_path / "vazio.git"
    _git(tmp_path, "init", "--bare", str(empty))
    dest = str(tmp_path / "clone")
    gitops.clone(str(empty), dest)
    assert gitops.repo_is_empty(dest)

    gitops.bootstrap_empty_repo(dest, "main", "meu-repo", "autoia", "autoia@local")

    assert not gitops.repo_is_empty(dest)
    assert (tmp_path / "clone" / "README.md").exists()
    assert gitops.resolve_default_branch(dest, "main") == "main"
    # o push chegou no bare (a branch origin/main tem o README num novo clone)
    dest2 = str(tmp_path / "clone2")
    gitops.clone(str(empty), dest2)
    files = _git(dest2, "ls-tree", "-r", "--name-only", "origin/main").stdout
    assert "README.md" in files


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


def test_push_and_pull_branch(bare_repo, tmp_path):
    """Workflow ADVPL: push da branch do desenvolvedor + pull num re-clone."""
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)
    gitops.ensure_task_branch(dest, "autoia/task-1", "main")
    (tmp_path / "clone" / "hello.py").write_text("print('oi')\n")
    gitops.commit_all(dest, "[kimi] - cria hello.py")
    gitops.push_branch(dest, "autoia/task-1")

    # segundo clone: a branch foi criada do origin/main (sem o commit); o pull traz
    # o que foi publicado no remoto.
    dest2 = str(tmp_path / "clone2")
    gitops.clone(bare_repo, dest2)
    gitops.ensure_task_branch(dest2, "autoia/task-1", "main")
    assert not (tmp_path / "clone2" / "hello.py").exists()
    gitops.pull_branch(dest2, "autoia/task-1")
    assert (tmp_path / "clone2" / "hello.py").exists()


def test_pull_branch_noop_when_not_published(bare_repo, tmp_path):
    """Pull numa branch que ainda não existe no remoto não quebra (best-effort)."""
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)
    gitops.ensure_task_branch(dest, "autoia/task-1", "main")
    gitops.pull_branch(dest, "autoia/task-1")  # não deve lançar
    assert gitops.current_branch(dest) == "autoia/task-1"


def test_advpl_helpers():
    assert gitops.is_advpl_robot("developer-advpl") is True
    assert gitops.is_advpl_robot("developer") is False
    assert gitops.is_advpl_robot(None) is False
    assert gitops.advpl_commit_message("kimi", "cria função X") == "[kimi] - cria função X"
    assert gitops.advpl_commit_message("opencode", "ajusta Y") == "[opencode] - ajusta Y"
