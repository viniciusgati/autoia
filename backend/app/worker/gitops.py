"""Operações git via subprocess (clone, branch, commit, merge, push).

Segurança: todos os caminhos de checkout são controlados pela aplicação
(workspaces/<repo_id>). O push é feito SOMENTE aqui (pelo merger), nunca pelos robôs.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

_GIT_TIMEOUT = 300


class GitError(Exception):
    def __init__(self, args: tuple, stderr: str):
        self.args_tuple = args
        self.stderr = stderr
        super().__init__(f"git {' '.join(args)} falhou: {stderr[-500:]}")


def run_git(cwd: str, *args: str, check: bool = True, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    env = {**os.environ, "LANG": "C", "LC_ALL": "C"}  # saída determinística (parse de conflito etc.)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        raise GitError(args, result.stderr)
    return result


def clone(url: str, dest: str) -> None:
    if ".." in url.split("/"):
        raise GitError(("clone", url), "url inválida")
    # dest absoluto: com dest relativo, o git resolve o caminho relativo ao cwd do
    # clone (dirname(dest)) e pode criar o checkout no lugar errado (aninhado).
    dest_abs = os.path.abspath(dest)
    run_git(os.path.dirname(dest_abs), "clone", url, dest_abs)


def resolve_default_branch(path: str, fallback: str) -> str:
    """Descobre a branch default do remote origin (ou usa o fallback)."""
    result = run_git(path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if result.returncode == 0:
        ref = result.stdout.strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    branches = run_git(path, "branch", "-r", check=False).stdout
    for candidate in (fallback, "main", "master"):
        if f"origin/{candidate}" in branches:
            return candidate
    raise GitError(("branch", "-r"), "não foi possível detectar a branch default")


def branch_exists(path: str, branch: str) -> bool:
    return run_git(path, "rev-parse", "--verify", "--quiet", branch, check=False).returncode == 0


def is_tracked(path: str, name: str) -> bool:
    """True se o caminho já está versionado no índice do checkout."""
    result = run_git(path, "ls-files", "--error-unmatch", "--", name, check=False)
    return result.returncode == 0


def ensure_task_branch(path: str, branch: str, base: str) -> None:
    """Garante a branch de trabalho da tarefa existir localmente a partir de origin/<base>.

    Se a branch já existe (ex.: retry), apenas faz checkout — preserva os commits do robô.
    """
    run_git(path, "fetch", "origin")
    if branch_exists(path, branch):
        run_git(path, "checkout", branch)
    else:
        run_git(path, "checkout", "-B", branch, f"origin/{base}")


def checkout_default(path: str, base: str) -> None:
    """Coloca o checkout na branch default, espelhando o remote (descarta sujeira local).

    Usado pelas fases pós-merge: o checkout local é apenas espelho do remote; qualquer
    mudança local não commitada (lixo de fases de teste) é descartada com reset --hard.
    """
    run_git(path, "fetch", "origin")
    run_git(path, "reset", "--hard", f"origin/{base}")
    run_git(path, "checkout", base)


def current_branch(path: str) -> str:
    return run_git(path, "branch", "--show-current").stdout.strip()


def has_uncommitted_changes(path: str) -> bool:
    result = run_git(path, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit_all(path: str, message: str) -> bool:
    """Faz commit de tudo; retorna False se não havia nada para commitar."""
    run_git(path, "add", "-A")
    diff = run_git(path, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return False
    run_git(path, "commit", "-m", message[:200])
    return True


def diff_stat(path: str, base: str, branch: str) -> str:
    return run_git(
        path, "diff", f"origin/{base}...{branch}", "--stat", check=False
    ).stdout.strip()


@dataclass
class DiffChange:
    """Uma mudança de arquivo no diff (status A/M/D, path e linhas adicionadas/removidas)."""

    status: str
    path: str
    added: int
    deleted: int


def diff_changes(path: str, base: str, branch: str) -> list[DiffChange]:
    """Lista as mudanças de `origin/<base>...<branch>` (status + numstat).

    Usa `-z` (separador NUL) para ser robusto a espaços nos paths. Renames (`R*`)
    contam como o caminho NOVO; binários têm linhas 0.
    """
    spec = f"origin/{base}...{branch}"
    name_status = run_git(path, "diff", spec, "--name-status", "-z").stdout
    numstat = run_git(path, "diff", spec, "--numstat", "-z").stdout

    # --numstat -z: cada registro "add\tdel\tpath\0" (NUL separa registros, não campos)
    lines_by_path: dict[str, tuple[int, int]] = {}
    for token in (t for t in numstat.split("\0") if t):
        parts = token.split("\t", 2)
        if len(parts) != 3:
            continue
        add_s, del_s, file_path = parts
        try:
            added = int(add_s) if add_s != "-" else 0
            deleted = int(del_s) if del_s != "-" else 0
        except ValueError:
            continue
        lines_by_path[file_path] = (added, deleted)

    # --name-status -z: pares "status\0path\0..." (rename: status\0novo\0antigo\0)
    tokens = [t for t in name_status.split("\0") if t]
    changes: list[DiffChange] = []
    i = 0
    while i < len(tokens) - 1:
        status = tokens[i]
        file_path = tokens[i + 1]
        i += 2
        if status.startswith("R"):
            i += 1  # pula o caminho antigo
        added, deleted = lines_by_path.get(file_path, (0, 0))
        changes.append(
            DiffChange(status=status[0], path=file_path, added=added, deleted=deleted)
        )
    return changes


@dataclass
class MergeResult:
    ok: bool
    conflict: bool = False
    detail: str = ""


def merge_and_push(path: str, branch: str, base: str) -> MergeResult:
    """Merge da branch de trabalho na default e push. Chamado APENAS pelo merger."""
    run_git(path, "checkout", base)
    # Tenta trazer atualizações do remote; se divergir, segue (merge vai resolver).
    run_git(path, "pull", "--ff-only", "origin", base, check=False)
    result = run_git(path, "merge", "--no-ff", branch, "-m", f"autoia: merge {branch}", check=False)
    if result.returncode != 0:
        combined = result.stdout + result.stderr
        conflict = "CONFLICT" in combined
        run_git(path, "merge", "--abort", check=False)
        run_git(path, "checkout", branch, check=False)
        return MergeResult(ok=False, conflict=conflict, detail=combined[-500:])
    run_git(path, "push", "origin", base)
    run_git(path, "checkout", branch, check=False)
    return MergeResult(ok=True, detail=result.stdout[-300:])
