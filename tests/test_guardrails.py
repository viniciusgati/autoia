"""Testes unitários dos guardrails e do orçamento."""

from __future__ import annotations

from app import budget
from app.config import Settings
from app.guardrails import (
    check_command,
    check_tool_call,
    extract_command,
    extract_file_path,
    path_is_within,
)


def test_extract_command():
    assert extract_command('{"command":"ls -la"}') == "ls -la"
    assert extract_command('{"foo":1}') is None
    assert extract_command("not-json") is None


def test_extract_file_path():
    assert extract_file_path('{"path":"src/a.py"}') == "src/a.py"
    assert extract_file_path('{"content":"x"}') is None


def test_check_command_blocks_risky():
    patterns = [r"\brm\s+-rf\b", r"\bcurl\b", r"git\s+push\b"]
    assert check_command("rm -rf /tmp/x", patterns) is not None
    assert check_command("curl http://evil.com", patterns) is not None
    assert check_command("git push origin main", patterns) is not None


def test_check_command_allows_safe(tmp_path):
    patterns = [r"\brm\s+-rf\b", r"\bcurl\b", r"git\s+push\b"]
    assert check_command("ls -la", patterns) is None
    assert check_command("git status", patterns) is None
    assert check_command("pytest tests/", patterns) is None


def test_check_tool_call_file_outside_workspace(tmp_path):
    checkout = str(tmp_path / "checkout")
    tool_call = {"function": {"name": "Write", "arguments": '{"path":"/etc/passwd"}'}}
    violation = check_tool_call(tool_call, [], checkout)
    assert violation is not None
    assert violation.pattern == "path-outside-workspace"

    inside = {"function": {"name": "Write", "arguments": '{"path":"src/x.py"}'}}
    assert check_tool_call(inside, [], checkout) is None


def test_read_logs_fora_do_checkout_permitido(tmp_path):
    """Leitura (Read/Grep) de logs do próprio kimi e de /tmp é permitida; escrita não."""
    checkout = str(tmp_path / "checkout")
    # log de servidor em /tmp criado pelo robô
    assert (
        check_tool_call(
            {"function": {"name": "Read", "arguments": '{"path":"/tmp/server.log"}'}},
            [],
            checkout,
        )
        is None
    )
    assert (
        check_tool_call(
            {"function": {"name": "Grep", "arguments": '{"path":"/tmp/out.log"}'}},
            [],
            checkout,
        )
        is None
    )
    # escrita fora do checkout continua bloqueada, mesmo em /tmp
    assert (
        check_tool_call(
            {"function": {"name": "Write", "arguments": '{"path":"/tmp/x.py"}'}},
            [],
            checkout,
        )
        is not None
    )
    # arquivo sensível fora das raízes liberadas segue bloqueado
    assert (
        check_tool_call(
            {"function": {"name": "Read", "arguments": '{"path":"/etc/passwd"}'}},
            [],
            checkout,
        )
        is not None
    )


def test_read_sessions_kimi_permitido(tmp_path, monkeypatch):
    """output.log do próprio kimi (~/.kimi-code/sessions) é legível; o resto do home não."""
    from app import guardrails

    sessions = str(tmp_path / "kimi-code" / "sessions")
    monkeypatch.setattr(guardrails, "_READABLE_EXTRA_ROOTS", (sessions,))
    checkout = str(tmp_path / "checkout")
    log = f"{sessions}/wd_1/out.log"
    assert (
        check_tool_call({"function": {"name": "Read", "arguments": f'{{"path":"{log}"}}'}}, [], checkout)
        is None
    )
    assert (
        check_tool_call(
            {"function": {"name": "Read", "arguments": '{"path":"/home/x/.ssh/id_rsa"}'}},
            [],
            checkout,
        )
        is not None
    )


def test_path_is_within(tmp_path):
    root = str(tmp_path / "checkout")
    assert path_is_within(str(tmp_path / "checkout" / "src" / "a.py"), root)
    assert path_is_within(root, root)
    assert not path_is_within(str(tmp_path / "other" / "a.py"), root)
    # prefix engodo: checkout2 não é filho de checkout
    assert not path_is_within(str(tmp_path / "checkout2" / "a.py"), root)


def test_budget():
    assert budget.budget_exceeded(1.0, 1.0)
    assert budget.budget_exceeded(1.5, 1.0)
    assert not budget.budget_exceeded(0.5, 1.0)
    settings = Settings()
    assert budget.interaction_cost(settings) == settings.cost_per_interaction
