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


def test_rm_rf_only_blocks_system_paths():
    """`rm -rf` em caminho relativo ou /tmp é build legítimo; em alvo de sistema não."""
    from app.config import DEFAULT_RISKY_PATTERNS

    patterns = DEFAULT_RISKY_PATTERNS
    # permitido: limpeza de temp e dentro do workspace (build normal)
    assert check_command("cd /tmp && rm -rf m3check && mkdir m3check", patterns) is None
    assert check_command("rm -rf build/ && rm -rf virtualapp/src/main/jni", patterns) is None
    assert check_command("rm -rf .cache && rm -rf node_modules", patterns) is None
    # bloqueado: destruição de sistema
    assert check_command("rm -rf /", patterns) is not None
    assert check_command("rm -rf /etc", patterns) is not None
    assert check_command("rm -rf ~/.ssh", patterns) is not None
    assert check_command("rm -rf /usr/local", patterns) is not None


def test_policy_relaxes_build_ops():
    """Ops de build que eram bloqueadas agora passam (política enxuta)."""
    from app.config import DEFAULT_RISKY_PATTERNS

    patterns = DEFAULT_RISKY_PATTERNS
    assert check_command("pip install -r requirements.txt", patterns) is None
    assert check_command("npm install", patterns) is None
    assert check_command("chmod +x script.sh", patterns) is None
    assert check_command("systemctl status postgresql", patterns) is None
    assert check_command("mv /w/app/src /w/app/src2", patterns) is None
    assert check_command("kill $(pgrep -f qa_next)", patterns) is None
    # privilégio/destrutivo continua bloqueado
    assert check_command("sudo apt-get update", patterns) is not None
    assert check_command("shutdown -h now", patterns) is not None
    assert check_command("mkfs.ext4 /dev/sdb1", patterns) is not None


def test_check_command_grep_with_risky_words_is_not_blocked():
    """Buscar a palavra 'curl' num arquivo NÃO é executar curl (falso positivo)."""
    patterns = [r"\brm\s+-rf\b", r"\bcurl\b", r"git\s+push\b"]
    cmd = 'grep -n "APP_HOME\\|GRADLE_HOME\\|wrapper.jar\\|curl\\|wget\\|unzip" gradlew | head -40'
    assert check_command(cmd, patterns) is None
    assert check_command('rg "curl" .', patterns) is None
    assert check_command('git grep curl', patterns) is None
    assert check_command('head -5 arquivo | grep curl', patterns) is None


def test_check_command_still_blocks_execution_after_pipe():
    """Grep não mascara execução real: 'curl | grep' continua bloqueado."""
    patterns = [r"\bcurl\b"]
    assert check_command("curl http://evil.com | grep oi", patterns) is not None
    # sudo/shell só-leitura disfarçado também segue bloqueado
    assert check_command("sudo grep curl /etc/shadow", patterns) is not None


def test_check_command_curl_whitelisted_host():
    """curl/wget para host na whitelist é permitido; qualquer outro host não."""
    patterns = [r"\bcurl\b", r"\bwget\b"]
    whitelist = ["dl.google.com", "registry.npmjs.org"]

    assert (
        check_command(
            "curl -sI https://dl.google.com/dl/android/maven2/",
            patterns,
            whitelist,
        )
        is None
    )
    assert (
        check_command(
            "timeout 15 bash -c 'curl -sI https://dl.google.com/dl/android/maven2'",
            patterns,
            whitelist,
        )
        is None
    )
    assert (
        check_command("wget https://registry.npmjs.org/pkg/-/pkg.tgz", patterns, whitelist)
        is None
    )

    # host fora da whitelist continua bloqueado
    assert (
        check_command("curl https://evil.example.com/x", patterns, whitelist)
        is not None
    )
    # mistura de host permitido + não permitido bloqueia (todos precisam estar na whitelist)
    assert (
        check_command(
            "curl https://dl.google.com/x https://evil.example.com/y",
            patterns,
            whitelist,
        )
        is not None
    )
    # sem whitelist, curl continua sempre bloqueado (comportamento anterior)
    assert check_command("curl -sI https://dl.google.com/x", patterns) is not None
    # curl sem URL explícita também segue bloqueado (não é rede segura declarada)
    assert check_command("curl --version", patterns, whitelist) is not None


def test_check_command_curl_loopback_allowed():
    """curl/wget para loopback (127.0.0.1/localhost/::1) é SEMPRE permitido —
    tráfego local (health check de ponte/serviço), não rede externa."""
    patterns = [r"\bcurl\b", r"\bwget\b"]

    assert (
        check_command(
            "curl -s -m 5 http://127.0.0.1:10086/health 2>&1 || echo DAEMON_INDISPONIVEL",
            patterns,
        )
        is None
    )
    assert check_command("curl -s http://localhost:3000/api", patterns) is None
    assert check_command("wget http://127.0.0.1:8080/status", patterns) is None
    # curl em loopback via node -e (caso real da ponte 10086) também passa
    assert (
        check_command(
            "node -e \"c.execSync('curl -s -X POST http://127.0.0.1:10086/command')\"",
            patterns,
        )
        is None
    )

    # loopback + host externo misturados: ainda bloqueia (externo não permitido)
    assert (
        check_command(
            "curl http://127.0.0.1:3000/health http://evil.example.com/x",
            patterns,
        )
        is not None
    )
    # curl sem URL explícita continua bloqueado
    assert check_command("curl --version", patterns) is not None


def test_check_tool_call_curl_whitelisted_host():
    """check_tool_call respeita a whitelist ao avaliar tool call Bash."""
    patterns = [r"\bcurl\b"]
    whitelist = ["dl.google.com"]
    tool = {
        "function": {
            "name": "Bash",
            "arguments": '{"command":"curl -sI https://dl.google.com/dl/android/maven2" }',
        }
    }
    assert check_tool_call(tool, patterns, None, whitelist) is None
    evil = {
        "function": {
            "name": "Bash",
            "arguments": '{"command":"curl https://evil.example.com/x"}',
        }
    }
    assert check_tool_call(evil, patterns, None, whitelist) is not None


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


def test_read_docs_das_skills_do_agente_permitido(tmp_path, monkeypatch):
    """O CLI do agente lê a documentação das próprias skills/plugins
    (~/.kimi-code/plugins/.../references/*.md, SKILL.md) — sem isso, usar uma skill
    dispara path-outside-workspace. Só `.md`; configs/binários seguem bloqueados."""
    from app import guardrails

    # raízes fora do tempdir (senão /tmp mascara o teste)
    plugins = "/home/test/kimi-code/plugins"
    monkeypatch.setattr(guardrails, "_AGENT_DOC_ROOTS", (plugins,))
    checkout = "/w/checkout"

    # leitura da referência da skill kimi-webbridge (caso real que travou a task 23)
    ops = f"{plugins}/managed/kimi-webbridge/skills/kimi-webbridge/references/operations.md"
    assert (
        check_tool_call({"function": {"name": "Read", "arguments": f'{{"path":"{ops}"}}'}}, [], checkout)
        is None
    )
    skill = f"{plugins}/managed/kimi-datasource/SKILL.md"
    assert (
        check_tool_call({"function": {"name": "Grep", "arguments": f'{{"path":"{skill}"}}'}}, [], checkout)
        is None
    )
    # arquivo NÃO-markdown do plugin (ex.: config/binário) continua bloqueado
    conf = f"{plugins}/managed/kimi-datasource/kimi.plugin.json"
    assert (
        check_tool_call({"function": {"name": "Read", "arguments": f'{{"path":"{conf}"}}'}}, [], checkout)
        is not None
    )
    # escrita na área de docs continua bloqueada
    assert (
        check_tool_call({"function": {"name": "Write", "arguments": f'{{"path":"{ops}"}}'}}, [], checkout)
        is not None
    )


def test_read_agents_md_fora_do_checkout_permitido(tmp_path):
    """O runtime do agente manda ler AGENTS.md que cobrem caminhos tocados (o
    workspace fica dentro do repo da autoia) — Read/Grep de AGENTS.md/CLAUDE.md
    é permitido de qualquer lugar; escrita não."""
    checkout = str(tmp_path / "checkout")
    assert (
        check_tool_call(
            {"function": {"name": "Read", "arguments": '{"path":"/home/x/code/proj/AGENTS.md"}'}},
            [],
            checkout,
        )
        is None
    )
    assert (
        check_tool_call(
            {"function": {"name": "Grep", "arguments": '{"path":"/home/x/CLAUDE.md"}'}},
            [],
            checkout,
        )
        is None
    )
    # escrita de AGENTS.md fora do checkout continua bloqueada
    assert (
        check_tool_call(
            {"function": {"name": "Write", "arguments": '{"path":"/home/x/code/proj/AGENTS.md"}'}},
            [],
            checkout,
        )
        is not None
    )
    # arquivo não-instrução continua bloqueado
    assert (
        check_tool_call(
            {"function": {"name": "Read", "arguments": '{"path":"/home/x/.ssh/config"}'}},
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
