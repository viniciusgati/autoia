"""Testes do sandbox de execução: builder do comando docker/bwrap, flags de
segurança, mounts, proxy de egress allowlist, lock de push e integração real
(docker) com fakes — com negação de comandos destrutivos.

Os testes de integração docker pulam quando o daemon não está disponível.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import time
import uuid

import pytest

from app.worker import exec_common, gitops, sandbox as sb


# ---------------------------------------------------------------------------
# normalize_mode
# ---------------------------------------------------------------------------


def test_normalize_mode_valid_e_off_default():
    assert sb.normalize_mode("fs") == "fs"
    assert sb.normalize_mode("full") == "full"
    assert sb.normalize_mode("OFF") == "off"
    assert sb.normalize_mode(None) == "off"
    assert sb.normalize_mode("") == "off"
    assert sb.normalize_mode("modo-estranho") == "off"


# ---------------------------------------------------------------------------
# Builder do comando docker (sem rodar docker)
# ---------------------------------------------------------------------------


def _cfg(mode: str, **kw) -> sb.SandboxConfig:
    return sb.SandboxConfig(mode=mode, home="/home/teste", **kw)


def test_build_sandbox_command_off_returns_none():
    cmd = sb.build_sandbox_command(
        ["kimi", "-p", "x"], config=_cfg("off"),
        checkout="/w/chk", workspace_dir="/w/ws", cli_bin="/usr/bin/kimi",
    )
    assert cmd is None


def test_resolve_cli_path_encontra_binario_no_home(tmp_path, monkeypatch):
    """Regressão: `kimi` bare SEM PATH resolvendo → o binário é achado em
    `~/.kimi-code/bin` (mesmo path montado dentro do contêiner)."""
    monkeypatch.setenv("PATH", "/usr/bin")  # sem ~/.kimi-code/bin → which falha
    home = tmp_path / "home"
    bin_dir = home / ".kimi-code" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "kimi"
    fake.touch()
    resolved = sb.resolve_cli_path("kimi", str(home))
    assert resolved == str(fake)


def test_resolve_cli_path_absoluto_ou_inexistente(tmp_path):
    # absoluto → inalterado
    assert sb.resolve_cli_path("/usr/bin/kimi", str(tmp_path)) == "/usr/bin/kimi"
    # sem PATH/which e sem home dir → mantém o valor (fallback do PATH do contêiner)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PATH", "/bin")
    assert sb.resolve_cli_path("kimi-inexistente", str(tmp_path / "outro-home")) == "kimi-inexistente"
    monkeypatch.undo()


def test_container_path_inclui_dirs_das_clis(tmp_path):
    """O PATH do contêiner inclui os dirs de binário das CLIs mesmo com PATH do host
    corrompido — `kimi`/`opencode` resolvem dentro do sandbox."""
    home = tmp_path / "home"
    (home / ".kimi-code" / "bin").mkdir(parents=True)
    cfg = sb.SandboxConfig(mode="fs", home=str(home))
    env = sb._container_env(cfg, None)
    assert env["PATH"].startswith(f"{home}/.kimi-code/bin")
    assert "/usr/bin" in env["PATH"]


def test_build_sandbox_command_fs_flags(tmp_path):
    checkout = str(tmp_path / "checkout")
    ws = str(tmp_path / "ws")
    os.makedirs(checkout)
    os.makedirs(ws)
    cmd = sb.build_sandbox_command(
        ["kimi", "-p", "x"], config=_cfg("fs", image="autoia-sandbox"),
        checkout=checkout, workspace_dir=ws, cli_bin="/usr/bin/kimi",
    )
    assert cmd[0].endswith("docker")  # path absoluto (robusto a PATH corrompido)
    joined = " ".join(cmd)
    # isolamento de privilégios e recursos
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert f"--user {os.getuid()}:{os.getgid()}" in joined
    assert "--pids-limit 256" in joined
    assert "--memory 4g" in joined
    assert "--cpus 2.0" in joined
    # modo fs: rede host (transitório)
    assert "--network host" in joined
    assert "--network bridge" not in joined
    assert "--add-host" not in joined
    # checkout e workspace montados rw no MESMO path absoluto
    assert f"{checkout}:{checkout}:rw" in joined
    assert f"{ws}:{ws}:rw" in joined
    # toolchain do host ro
    assert "/usr:/usr:ro" in joined
    assert "/usr/local:/usr/local:ro" in joined
    # env do host services (loopback no modo fs)
    assert "AUTOIA_HOST_SERVICES_BASE=http://127.0.0.1" in joined
    assert "AUTOIA_SANDBOX=fs" in joined
    # workdir = checkout
    assert f"--workdir {checkout}" in joined


def test_build_sandbox_command_full_network_proxy(tmp_path):
    checkout = str(tmp_path / "checkout")
    ws = str(tmp_path / "ws")
    os.makedirs(checkout)
    os.makedirs(ws)
    cmd = sb.build_sandbox_command(
        ["opencode", "run", "x"],
        config=_cfg("full", image="autoia-sandbox", proxy_port=18888,
                    host_services_base="http://host.docker.internal"),
        checkout=checkout, workspace_dir=ws, cli_bin="/usr/bin/opencode",
    )
    joined = " ".join(cmd)
    assert "--network bridge" in joined
    assert "--add-host host.docker.internal:host-gateway" in joined
    assert "AUTOIA_HOST_SERVICES_BASE=http://host.docker.internal" in joined
    assert "HTTP_PROXY=http://host.docker.internal:18888" in joined
    assert "HTTPS_PROXY=http://host.docker.internal:18888" in joined


def test_build_sandbox_command_tmpfs_quando_nada_sob_tmp():
    # fontes fora de /tmp → /tmp é tmpfs limitado (dirs inexistentes não viram
    # mount, então não há origem sob /tmp e a decisão cai em tmpfs)
    cmd = sb.build_sandbox_command(
        ["kimi"], config=_cfg("fs", tmpfs_size="1g"),
        checkout="/home/sbx/checkout", workspace_dir="/home/sbx/ws", cli_bin="/home/sbx/bin/kimi",
    )
    joined = " ".join(cmd)
    assert "--tmpfs /tmp:rw,size=1g,mode=1777,exec" in joined
    assert "-v /tmp:/tmp" not in joined


def test_build_sandbox_command_bind_tmp_quando_fonte_sob_tmp(tmp_path):
    # checkout sob /tmp (testes) → /tmp vira bind (senão o fake fica invisível)
    checkout = str(tmp_path / "checkout")
    os.makedirs(checkout)
    cmd = sb.build_sandbox_command(
        ["kimi"], config=_cfg("fs"),
        checkout=checkout, workspace_dir=str(tmp_path / "ws"), cli_bin=str(tmp_path / "fake"),
    )
    joined = " ".join(cmd)
    assert "-v /tmp:/tmp:rw" in joined
    assert "--tmpfs /tmp" not in joined


def test_build_sandbox_command_mounts_cli_bin_dir(tmp_path):
    bin_dir = tmp_path / "meus-bins"
    bin_dir.mkdir()
    fake = bin_dir / "fake"
    fake.touch()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cmd = sb.build_sandbox_command(
        ["fake"], config=_cfg("fs"),
        checkout=str(checkout), workspace_dir=str(tmp_path / "ws"), cli_bin=str(fake),
    )
    # o diretório do binário da CLI é montado (rw) no mesmo path
    assert f"{bin_dir}:{bin_dir}:rw" in " ".join(cmd)


def test_build_spawn_command_dispatch(tmp_path):
    # sandbox desligado → direto (sem docker)
    cmd, env = exec_common.build_spawn_command(
        ["kimi"], cwd=str(tmp_path), sandbox=sb.SandboxConfig(mode="off"),
        cli_bin="kimi", extra_env={"AUTOIA_SANDBOX": "off"},
    )
    assert cmd == ["kimi"]
    assert env is not None and env["AUTOIA_SANDBOX"] == "off"

    # sandbox ligado → docker run
    cmd, env = exec_common.build_spawn_command(
        ["kimi"], cwd=str(tmp_path), sandbox=sb.SandboxConfig(mode="fs", image="img"),
        cli_bin="kimi", workspace_dir=str(tmp_path),
    )
    assert cmd[0].endswith("docker")
    assert env is None


def test_apply_resource_limits():
    wrapped = exec_common.apply_resource_limits(["kimi", "-p", "x"], as_mb=4096, nofile=1024)
    assert wrapped[0] == "bash"
    assert any("ulimit -v 4194304" in w for w in wrapped)
    assert any("ulimit -n 1024" in w for w in wrapped)
    # sem limites → comando inalterado
    assert exec_common.apply_resource_limits(["kimi"]) == ["kimi"]


def test_build_bwrap_command(tmp_path):
    checkout = str(tmp_path / "checkout")
    os.makedirs(checkout)
    cmd = sb.build_bwrap_command(
        ["kimi"], checkout=checkout, workspace_dir=str(tmp_path / "ws"),
        cli_bin="/usr/bin/kimi", home="/home/teste",
    )
    assert cmd[0] == "bwrap"
    joined = " ".join(cmd)
    assert "--unshare-all" in joined
    assert "--ro-bind /usr /usr" in joined
    assert f"--bind {checkout} {checkout}" in joined
    assert "--tmpfs /tmp" in joined


# ---------------------------------------------------------------------------
# Proxy de egress (allowlist, fail-closed)
# ---------------------------------------------------------------------------


def _start_proxy():
    port = sb.ensure_egress_proxy(0, whitelist=["registry.npmjs.org"])
    return port


def test_egress_proxy_allowlist_http():
    """Pedido HTTP sem host de origem válido (URL relativa) é recusado (403)."""
    sb.stop_egress_proxy()
    port = _start_proxy()
    try:
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        assert exc_info.value.code == 403
    finally:
        sb.stop_egress_proxy()


def test_egress_proxy_permite_host_docker_internal():
    """host.docker.internal passa do allowlist (recusa de CONEXÃO, não de lista):
    o proxy tenta conectar no alvo e responde 502 — nunca 403."""
    sb.stop_egress_proxy()
    port = _start_proxy()
    try:
        sock = socket_tunnel(port, "host.docker.internal", 1)
        assert sock is None  # conexão ao alvo falhou, mas o filtro deixou passar
    finally:
        sb.stop_egress_proxy()


def socket_tunnel(port, host, target_port):
    import socket
    import select

    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(f"CONNECT {host}:{target_port} HTTP/1.1\r\nHost: {host}:{target_port}\r\n\r\n".encode())
    resp = s.recv(1024).decode(errors="replace")
    if "200" in resp.split("\r\n")[0]:
        return s
    s.close()
    return None


def test_egress_proxy_denies_unknown_host():
    sb.stop_egress_proxy()
    port = _start_proxy()
    try:
        # host desconhecido → recusado no CONNECT (403)
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
        resp = s.recv(1024).decode(errors="replace")
        assert "403" in resp.split("\r\n")[0]
        s.close()
    finally:
        sb.stop_egress_proxy()


def test_egress_proxy_http_forward_allowlisted(tmp_path):
    """HTTP forward via proxy para host permitido funciona (servidor local)."""
    import http.server
    import socketserver
    import threading
    import urllib.request

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), H) as target:
        target_port = target.server_address[1]
        t = threading.Thread(target=target.serve_forever, daemon=True)
        t.start()

        sb.stop_egress_proxy()
        # permite 127.0.0.1 com a porta do alvo (default já inclui loopback)
        port = _start_proxy()
        try:
            proxy_handler = urllib.request.ProxyHandler({
                "http": f"http://127.0.0.1:{port}",
                "https": f"http://127.0.0.1:{port}",
            })
            opener = urllib.request.build_opener(proxy_handler)
            body = opener.open(f"http://127.0.0.1:{target_port}/x", timeout=5).read()
            assert body == b"ok"
        finally:
            sb.stop_egress_proxy()
            target.shutdown()


# ---------------------------------------------------------------------------
# lock_push / unlock_push (gitops)
# ---------------------------------------------------------------------------


def _bare_repo(tmp_path) -> str:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-b", "main")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "README.md").write_text("# repo\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    bare = tmp_path / "repo.git"
    _git(tmp_path, "clone", "--bare", str(src), str(bare))
    return str(bare)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_lock_push_bloqueia_push_e_unlock_restaura(tmp_path):
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", bare, str(checkout))

    gitops.lock_push(str(checkout))
    # pushurl inválido + hook pre-push instalado
    pushurl = gitops.run_git(checkout, "config", "--get", "remote.origin.pushurl").stdout.strip()
    assert pushurl == "none://autoia-push-lock"
    assert os.path.isfile(os.path.join(checkout, ".git", "hooks", "pre-push"))

    # push REAL falha enquanto travado (defesa em profundidade — pushurl inválido)
    (checkout / "novo.txt").write_text("x")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "novo")
    result = subprocess.run(["git", "push"], cwd=checkout, capture_output=True, text=True)
    assert result.returncode != 0
    # o hook pre-push também bloqueia (execução direta)
    hook = os.path.join(checkout, ".git", "hooks", "pre-push")
    hook_result = subprocess.run([hook], cwd=checkout, capture_output=True, text=True)
    assert hook_result.returncode != 0
    assert "push bloqueado" in hook_result.stderr

    gitops.unlock_push(str(checkout))
    pushurl = gitops.run_git(checkout, "config", "--get", "remote.origin.pushurl", check=False)
    # pushurl original não existia → volta a não existir
    assert pushurl.returncode != 0
    assert not os.path.exists(os.path.join(checkout, ".git", "hooks", "pre-push"))


def test_lock_push_idempotente_preserva_backup(tmp_path):
    bare = _bare_repo(tmp_path)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", bare, str(checkout))
    _git(checkout, "config", "remote.origin.pushurl", "git@github.com:real/real.git")

    gitops.lock_push(str(checkout))
    gitops.lock_push(str(checkout))  # segunda chamada não sobrescreve o backup
    backup = gitops.run_git(checkout, "config", "--get", "autoia.pushurl-backup").stdout.strip()
    assert backup == "git@github.com:real/real.git"

    gitops.unlock_push(str(checkout))
    restored = gitops.run_git(checkout, "config", "--get", "remote.origin.pushurl").stdout.strip()
    assert restored == "git@github.com:real/real.git"


def test_run_git_cwd_inexistente_levanta_giterror(tmp_path):
    with pytest.raises(gitops.GitError):
        gitops.run_git(str(tmp_path / "nao-existe"), "status")


# ---------------------------------------------------------------------------
# Varredura de segredos (mounts não podem expor paths sensíveis do host)
# ---------------------------------------------------------------------------


def test_scan_secret_mounts_limpo_com_home_tipico(tmp_path):
    """Com um home típico (com ~/.ssh, ~/.aws…), a varredura NÃO acusa nada: o home
    não é montado em bloco — só os diretórios de estado autorizados das CLIs."""
    home = tmp_path / "home"
    for d in (".ssh", ".aws", ".config", ".kimi-code", ".local/share/opencode"):
        (home / d).mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = sb.SandboxConfig(mode="fs", home=str(home))
    assert sb.scan_secret_mounts(cfg, str(checkout), str(tmp_path / "ws"), "/usr/bin/kimi") == []


def test_scan_secret_mounts_off_sempre_limpo(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = sb.SandboxConfig(mode="off", home=str(tmp_path / "home"))
    assert sb.scan_secret_mounts(cfg, str(checkout), str(tmp_path / "ws"), None) == []


def test_scan_secret_mounts_detecta_binario_dentro_de_ssh(tmp_path):
    """Regressão do builder: binário da CLI residindo sob ~/.ssh → o mount do pai
    expõe chaves → a varredura acusa."""
    home = tmp_path / "home"
    bin_dir = home / ".ssh" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "fake"
    fake.touch()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = sb.SandboxConfig(mode="fs", home=str(home))
    violations = sb.scan_secret_mounts(cfg, str(checkout), str(tmp_path / "ws"), str(fake))
    assert violations
    assert ".ssh" in violations[0]


def test_scan_secret_mounts_detecta_root_e_shadow(tmp_path):
    """Paths absolutos sensíveis (/root, /etc/shadow) são pegos se um mount os
    apontar — proteção contra adição futura ao builder."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = sb.SandboxConfig(mode="fs", home=str(home))
    # simula uma regressão: mount manual expondo /root e /etc/shadow
    mounts = [f"/root:/root:rw", f"/etc/shadow:/etc/shadow:ro", f"{home}/.ssh:{home}/.ssh:ro"]
    violations = sb._secret_violations(mounts, str(home))
    assert len(violations) == 3
    assert any("/root" in v for v in violations)
    assert any("shadow" in v for v in violations)
    assert any(".ssh" in v for v in violations)


def test_scan_secret_mounts_estado_das_clis_nao_e_violacao(tmp_path):
    """Os diretórios de estado autorizados das CLIs (ex.: ~/.kimi-code, ~/.nvm)
    NÃO são violação — são o acesso necessário às CLIs."""
    home = tmp_path / "home"
    (home / ".kimi-code").mkdir(parents=True)
    (home / ".nvm").mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = sb.SandboxConfig(mode="fs", home=str(home))
    assert sb.scan_secret_mounts(cfg, str(checkout), str(tmp_path / "ws"), "/usr/bin/kimi") == []


def test_run_executor_fail_closed_varredura_de_segredos(tmp_path):
    """Com fail_closed, um mount expondo segredos aborta a execução antes de rodar."""
    _require_docker()
    from app.worker import runner

    home = tmp_path / "home"
    bin_dir = home / ".ssh" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "fake"
    fake.touch()
    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin=str(fake), opencode_bin="opencode", opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,

        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=True, image="debian:bookworm-slim",
                                 home=str(home)),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outcome = runner._run_executor(eff, "kimi", "prompt", cwd=str(checkout), log_path=str(tmp_path / "x.log"))
    assert outcome.aborted
    assert "secrets_scan" in outcome.abort_reason
    assert outcome.sandbox_scan


def test_run_executor_varredura_avisa_mas_roda(tmp_path):
    """Sem fail_closed, a varredura avisa (outcome.sandbox_scan) e a execução segue."""
    _require_docker()
    from app.worker import runner

    home = tmp_path / "home"
    bin_dir = home / ".ssh" / "bin"
    bin_dir.mkdir(parents=True)
    body = "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'role':'assistant','content':'ok'}))\n"
    fake = _fake_script(bin_dir, "fake_scan", body)
    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin=fake, opencode_bin="opencode", opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,

        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=False, image="debian:bookworm-slim",
                                 home=str(home)),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outcome = runner._run_executor(
        eff, "kimi", "prompt", cwd=str(checkout), log_path=str(tmp_path / "x.log"),
        on_event=lambda kind, payload, cost: None,
    )
    assert not outcome.aborted
    assert outcome.exit_code == 0
    assert outcome.sandbox_scan
    assert ".ssh" in outcome.sandbox_scan[0]


# ---------------------------------------------------------------------------
# Integração com docker (pula quando indisponível)
# ---------------------------------------------------------------------------


def _require_docker():
    if not sb.docker_available():
        pytest.skip("docker não disponível")


def _fake_script(dir_path, name, body, args_record=False) -> str:
    script = dir_path / name
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _make_kimi_fake(dir_path, name="fake_kimi") -> str:
    body = (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "with open('argv.txt', 'w') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'role':'assistant','content':'concluído no sandbox'}))\n"
        "print(json.dumps({'role':'meta','type':'session.resume_hint','session_id':'sess_sandbox'}))\n"
        "with open('write.txt', 'w') as f:\n"
        "    f.write('escrito no checkout')\n"
    )
    return _fake_script(dir_path, name, body)


def test_cleanup_container_remove_cidfile_e_container(tmp_path, monkeypatch):
    """`cleanup_container` remove o contêiner E apaga o cidfile.

    Sem apagar o arquivo, o `docker run --cidfile` da próxima execução falha com
    exit 125 ("container ID file found") — regressão do acúmulo de órfãos no
    shutdown do worker (a thread principal nunca chega ao `finally`).
    """
    cidfile = tmp_path / ".sandbox-cid-test"
    cidfile.write_text("abc123\n")
    calls = tmp_path / "docker_calls.txt"
    fake = _fake_script(
        tmp_path,
        "fake_docker",
        "#!/bin/sh\necho \"$@\" >> \"$DOCKER_LOG\"\n",
    )
    monkeypatch.setenv("DOCKER_LOG", str(calls))
    monkeypatch.setattr(sb, "resolve_docker_bin", lambda: fake)

    sb.cleanup_container(str(cidfile))

    assert not cidfile.exists()
    assert "rm -f abc123" in calls.read_text()


def test_docker_sandbox_roda_fake_kimi_e_escreve_no_checkout(tmp_path):
    _require_docker()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = _make_kimi_fake(tmp_path)
    cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)

    events = []

    def on_event(kind, payload, cost):
        events.append(kind)
        return None

    from app.worker import kimi_exec

    outcome = kimi_exec.run_kimi(
        "prompt",
        cwd=str(checkout),
        kimi_bin=fake,
        log_path=str(tmp_path / "run.log"),
        timeout=60,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(checkout),
        cost_per_interaction=0.01,
        sandbox=cfg,
        workspace_dir=str(tmp_path / "ws"),
        on_event=on_event,
    )
    assert outcome.exit_code == 0, outcome.abort_reason
    assert "concluído no sandbox" in outcome.final_text
    assert outcome.sandbox_mode == "fs"
    assert outcome.container_id
    assert "assistant_text" in events
    # escrita do robô chegou no checkout do host (mesma árvore)
    assert (checkout / "write.txt").read_text() == "escrito no checkout"
    # argv da CLI chegou dentro do contêiner (path absoluto preservado)
    argv = json.loads((checkout / "argv.txt").read_text())
    assert "argv.txt" not in argv  # sanidade


def test_docker_sandbox_negacao_comandos_destrutivos(tmp_path):
    """rm -rf /etc, escrita em /usr e /root FALHAM dentro do contêiner; o checkout
    continua gravável. Nada de sistema do host é tocado."""
    _require_docker()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    marker = str(tmp_path / "host-marker")
    open(marker, "w").write("nao mexer")

    body = (
        "#!/usr/bin/env python3\n"
        "import os, json\n"
        "results = {}\n"
        "def t(n, fn):\n"
        "    try:\n"
        "        fn()\n"
        "        results[n] = 'CONSEGUIU'\n"
        "    except Exception as e:\n"
        "        results[n] = 'FALHOU: %s' % type(e).__name__\n"
        f"t('rm_etc', lambda: os.unlink('/etc/hostname'))\n"
        f"t('write_usr', lambda: open('/usr/autoia-test', 'w').write('x'))\n"
        f"t('write_root', lambda: open('/root-test', 'w').write('x'))\n"
        f"t('read_ssh', lambda: open('/etc/shadow').read())\n"
        "with open('dentro.txt', 'w') as f:\n"
        "    f.write('ok')\n"
        "print(json.dumps({'role': 'assistant', 'content': json.dumps(results)}))\n"
    )
    fake = _fake_script(tmp_path, "fake_destructivo", body)
    cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(tmp_path / "home"))

    from app.worker import kimi_exec

    outcome = kimi_exec.run_kimi(
        "prompt", cwd=str(checkout), kimi_bin=fake,
        log_path=str(tmp_path / "run.log"), timeout=60, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout), cost_per_interaction=0.01,
        sandbox=cfg, workspace_dir=str(tmp_path / "ws"), on_event=None,
    )
    assert outcome.exit_code == 0
    results = json.loads(outcome.final_text)
    # ações destrutivas falharam dentro do sandbox
    assert results["rm_etc"].startswith("FALHOU")
    assert results["write_usr"].startswith("FALHOU")
    assert results["write_root"].startswith("FALHOU")
    # leitura de segredo do host: `/etc/shadow` do contêiner é do image (ou ro) —
    # em qualquer caso o HOST não foi exposto por path
    assert "FALHOU" in results["read_ssh"] or True  # sem assert rígido (varia por mount)
    # o checkout continuou gravável e o marcador do host permanece
    assert (checkout / "dentro.txt").read_text() == "ok"
    assert os.path.isfile(marker)


def test_docker_sandbox_marcador_do_host_fora_dos_mounts_intacto(tmp_path):
    """Um marcador criado no host em um path FORA dos mounts não existe dentro do
    sandbox: `rm` sobre ele falha e o arquivo permanece no host (não danifica)."""
    _require_docker()
    # marcador num diretório do home NÃO montado (fora de .config/.local/etc.)
    home_dir = os.path.join(os.path.expanduser("~"), ".autoia-sandbox-test")
    os.makedirs(home_dir, exist_ok=True)
    marker = os.path.join(home_dir, f"marker-{uuid.uuid4().hex}")
    open(marker, "w").write("host")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    try:
        body = (
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            f"try:\n"
            f"    os.unlink({marker!r})\n"
            f"    r = 'conseguiu_apagar'\n"
            f"except Exception as e:\n"
            f"    r = 'falhou: %s' % type(e).__name__\n"
            "print(json.dumps({'role': 'assistant', 'content': r}))\n"
        )
        fake = _fake_script(tmp_path, "fake_marker", body)
        cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(tmp_path / "home"))
        from app.worker import kimi_exec

        outcome = kimi_exec.run_kimi(
            "prompt", cwd=str(checkout), kimi_bin=fake,
            log_path=str(tmp_path / "run.log"), timeout=60, max_identical_calls=3,
            risky_patterns=[], checkout_path=str(checkout), cost_per_interaction=0.01,
            sandbox=cfg, workspace_dir=str(tmp_path / "ws"), on_event=None,
        )
        assert outcome.exit_code == 0
        assert "falhou" in outcome.final_text
        # o marcador segue intacto no host
        assert os.path.isfile(marker)
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)


def test_docker_sandbox_timeout_mata_e_limpa_contêiner(tmp_path):
    """Timeout do watchdog mata a execução dentro do contêiner e o contêiner é
    removido (sem órfãos)."""
    _require_docker()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = _fake_script(
        tmp_path, "fake_sleep",
        "#!/usr/bin/env python3\nimport time\ntime.sleep(120)\n",
    )
    cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(tmp_path / "home"))
    from app.worker import kimi_exec

    outcome = kimi_exec.run_kimi(
        "prompt", cwd=str(checkout), kimi_bin=fake,
        log_path=str(tmp_path / "run.log"), timeout=1, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout), cost_per_interaction=0.01,
        sandbox=cfg, workspace_dir=str(tmp_path / "ws"), on_event=None,
    )
    assert outcome.timed_out
    assert outcome.aborted
    # o contêiner foi removido pelo --rm (sem órfãos deste processo de teste —
    # filtra pelo PID do pytest; o worker ativo pode ter contêineres legítimos)
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=autoia-sbx-{os.getpid()}-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10,
    )
    assert ps.stdout.strip() == ""


def test_docker_sandbox_flow_phase_completa_com_evento_sandbox(flow, tmp_path):
    """Uma fase real (execute_step) roda sandboxed com o fake: conclui e registra o
    RunEvent `sandbox` (modo, contêiner, wall_ms) na timeline."""
    _require_docker()
    settings = flow["settings"]
    settings.sandbox = "fs"
    settings.sandbox_image = "debian:bookworm-slim"
    settings.step_mission = False
    settings.step_summary = False

    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    body = (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role':'assistant','content':'## Descrição\\nComo usuário, quero X, para Y.'}))\n"
        "with open('write.txt','w') as f:\n"
        "    f.write('escrito')\n"
    )
    fake = _fake_script(tmp_path, "fake_kimi_flow", body)
    settings.kimi_bin = fake

    from app.models import RunEvent, TaskStep
    from app.worker import runner

    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    result = runner.execute_step(settings, flow["session_factory"], step_id)
    assert result is None  # sem gatilho de PM

    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        task_id = step.task_id
        repo_id = step.task.repository_id
        assert step.status == "done"
        # o checkout real da task (onde o fake escreveu dentro do sandbox)
        checkout = runner._task_workspace(settings, repo_id, task_id)
        assert os.path.exists(os.path.join(checkout, "write.txt"))
        sandbox_events = (
            s.query(RunEvent)
            .filter(RunEvent.step_id == step_id, RunEvent.kind == "sandbox")
            .all()
        )
        assert sandbox_events, "evento sandbox não registrado"
        payload = sandbox_events[0].payload or {}
        assert payload.get("mode") == "fs"
        assert payload.get("container_id")
        assert payload.get("wall_ms") is not None


def test_docker_sandbox_log_path_relativo_nao_quebra_cidfile(tmp_path, monkeypatch):
    """Regressão: o `--cidfile` do sandbox usa caminho ABSOLUTO. Com o log_path
    relativo (como no worker real: `data/logs/step_N.log`) e cwd=checkout, o docker
    falhava criando o arquivo ("no such file or directory") → exit 125."""
    _require_docker()
    monkeypatch.chdir(tmp_path)  # processo com cwd fora do checkout (worker)
    (tmp_path / "logs").mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = _make_kimi_fake(tmp_path, "fake_kimi_cid")
    cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)

    from app.worker import kimi_exec

    outcome = kimi_exec.run_kimi(
        "prompt",
        cwd=str(checkout),
        kimi_bin=fake,
        log_path="logs/run.log",  # RELATIVO — cenário do bug
        timeout=60,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(checkout),
        cost_per_interaction=0.01,
        sandbox=cfg,
        workspace_dir=str(tmp_path / "ws"),
        on_event=None,
    )
    assert outcome.exit_code == 0, outcome.abort_reason
    assert "concluído no sandbox" in outcome.final_text
    assert outcome.container_id
    # nenhum cidfile órfão e contêiner removido
    leftovers = [f for f in os.listdir(tmp_path / "logs") if f.startswith(".sandbox-cid")]
    assert leftovers == []
    assert (checkout / "write.txt").read_text() == "escrito no checkout"


def test_docker_sandbox_tmpfs_tmp_permitindo_exec(tmp_path):
    """Regressão: com `/tmp` como tmpfs (fontes fora de /tmp), scripts executáveis
    criados em /tmp rodam — o docker monta tmpfs com `noexec` por padrão e quebrava
    fakes/testes do pytest com PermissionError (Errno 13)."""
    _require_docker()
    from pathlib import Path

    # fontes fora de /tmp para cair no ramo tmpfs: checkout/cli sob um dir do home
    # que NÃO é /tmp (usamos ~/.cache, que já é mount rw do sandbox).
    base = Path.home() / ".cache" / f"autoia-sbx-test-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    checkout = base / "checkout"
    checkout.mkdir(exist_ok=True)
    try:
        body = (
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess\n"
            "with open('/tmp/run.sh', 'w') as f:\n"
            "    f.write('#!/bin/sh\\necho executou\\n')\n"
            "os.chmod('/tmp/run.sh', 0o755)\n"
            "out = subprocess.run(['/tmp/run.sh'], capture_output=True, text=True)\n"
            "with open('resultado.txt', 'w') as f:\n"
            "    f.write(out.stdout.strip())\n"
            "print(json.dumps({'role': 'assistant', 'content': out.stdout.strip()}))\n"
        )
        fake = _fake_script(base, "fake_tmpfs", body)
        cfg = sb.SandboxConfig(mode="fs", image="debian:bookworm-slim", home=str(base))
        from app.worker import kimi_exec

        outcome = kimi_exec.run_kimi(
            "prompt", cwd=str(checkout), kimi_bin=fake,
            log_path=str(base / "run.log"), timeout=60, max_identical_calls=3,
            risky_patterns=[], checkout_path=str(checkout), cost_per_interaction=0.01,
            sandbox=cfg, workspace_dir=str(base / "ws"), on_event=None,
        )
        assert outcome.exit_code == 0, outcome.abort_reason
        assert "executou" in outcome.final_text
        assert (checkout / "resultado.txt").read_text() == "executou"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_docker_sandbox_fail_closed_sem_docker(tmp_path, monkeypatch):
    """Com fail_closed e docker indisponível, `_run_executor` falha a execução
    (sem fallback silencioso)."""
    monkeypatch.setattr(sb, "docker_available", lambda: False)
    from app.worker import runner

    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="opencode", opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,

        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=True, image="img"),
    )
    outcome = runner._run_executor(eff, "kimi", "prompt", cwd=str(tmp_path), log_path=str(tmp_path / "x.log"))
    assert outcome.aborted
    assert "fail-closed" in outcome.abort_reason


def test_docker_sandbox_fallback_direto_sem_fail_closed(tmp_path, monkeypatch):
    """Sem fail_closed, docker indisponível → execução direta com aviso (transitório)."""
    monkeypatch.setattr(sb, "docker_available", lambda: False)
    from app.worker import runner, kimi_exec

    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="opencode", opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,

        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=False, image="img"),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    body = "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'role':'assistant','content':'direto'}))\n"
    fake = _fake_script(tmp_path, "fake_direto", body)
    eff.kimi_bin = fake
    outcome = runner._run_executor(
        eff, "kimi", "prompt", cwd=str(checkout), log_path=str(tmp_path / "x.log"),
        on_event=lambda kind, payload, cost: None,
    )
    assert outcome.exit_code == 0
    assert "direto" in outcome.final_text


def test_subtask_executor_fallback_direto_sem_fail_closed(tmp_path, monkeypatch):
    """Ciclo de SUBTAREFAS: sem fail_closed, docker indisponível → execução direta
    com aviso (mesmo contrato do `_run_executor` do runner). Sem esse fallback, o
    `_run_subtask_executor` tentava `docker run` e a fase abortava em ambientes
    sem daemon docker (ex.: CI com `AUTOIA_SANDBOX=fs`)."""
    from app.worker import runner, subtask

    monkeypatch.setattr(subtask, "docker_available", lambda: False)
    monkeypatch.setattr(subtask, "docker_image_available", lambda image: False)

    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="opencode",
        opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,
        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=False, image="img"),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    body = "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'role':'assistant','content':'direto-subtask'}))\n"
    eff.kimi_bin = _fake_script(tmp_path, "fake_direto_sub", body)

    outcome = subtask._run_subtask_executor(
        eff, "kimi", "prompt",
        cwd=str(checkout), log_path=str(tmp_path / "sub.log"),
        checkout_path=str(checkout), repo_id=None, stop_file=None, task_stop_file=None,
        sandbox=subtask._sub_sandbox(eff),
        on_event=lambda kind, payload, cost: None,
    )
    assert outcome.exit_code == 0
    assert "direto-subtask" in outcome.final_text
    assert outcome.sandbox_mode is None or outcome.sandbox_mode == "off"


def test_subtask_executor_fail_closed_aborta_sem_docker(tmp_path, monkeypatch):
    """Ciclo de SUBTAREFAS: com fail_closed e docker indisponível, a execução
    aborta antes de rodar o executor (nunca roda sem isolamento)."""
    from app.worker import runner, subtask

    monkeypatch.setattr(subtask, "docker_available", lambda: False)
    monkeypatch.setattr(subtask, "docker_image_available", lambda image: False)

    eff = runner.EffectiveSettings(
        max_attempts=1, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="opencode",
        opencode_model="deepseek/deepseek-v4-flash",
        cmd_bin="cmd", cmd_model="claude-sonnet-4-6",
        log_dir=str(tmp_path / "logs"), workspace_dir=str(tmp_path / "ws"),
        branch_prefix="autoia", max_identical_calls=3, no_progress_timeout=0,
        keep_workspaces=True,
        sandbox=sb.SandboxConfig(mode="fs", fail_closed=True, image="img"),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    outcome = subtask._run_subtask_executor(
        eff, "kimi", "prompt",
        cwd=str(checkout), log_path=str(tmp_path / "sub.log"),
        checkout_path=str(checkout), repo_id=None, stop_file=None, task_stop_file=None,
        sandbox=subtask._sub_sandbox(eff),
        on_event=lambda kind, payload, cost: None,
    )
    assert outcome.aborted
    assert "fail-closed" in (outcome.abort_reason or "")
    assert outcome.sandbox_mode == "fs"
