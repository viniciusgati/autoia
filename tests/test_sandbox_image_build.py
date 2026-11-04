"""Imagem do sandbox: receita versionada + reconstrução automática.

Regressão (07/10): uma limpeza de disco apagou a `autoia-android-emu:2026-09`
do Docker local — a receita nunca tinha sido commitada e todas as tasks do
perfil `android-emulator-35` morriam em
`infra_blocked: ... imagem indisponível (fail-closed)` sem como se recuperar.
"""

from __future__ import annotations

import stat

import pytest

from app.worker import sandbox, toolchains


def _fake_docker(tmp_path, *, build_creates_image: bool = True) -> str:
    """Script `docker` fake: `image inspect` só passa depois de um `build`."""
    state = tmp_path / "image-exists"
    script = tmp_path / "fake_docker"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"state = {str(state)!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['image', 'inspect']:\n"
        "    sys.exit(0 if os.path.exists(state) else 1)\n"
        "if args[:3] == ['image', 'rm']:\n"
        "    if os.path.exists(state): os.remove(state)\n"
        "    sys.exit(0)\n"
        "if args and args[0] == 'build':\n"
        "    open(os.path.join(os.path.dirname(state), 'build-args.txt'), 'a').write(' '.join(args) + '\\n')\n"
        f"    {'open(state, \"w\").write(\"ok\")' if build_creates_image else 'pass'}\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fake_docker(monkeypatch, tmp_path):
    """Instala o docker fake no sandbox (resolve_docker_bin + estado isolado)."""
    bin_path = _fake_docker(tmp_path)
    monkeypatch.setattr(sandbox, "resolve_docker_bin", lambda: bin_path)
    return {"bin": bin_path, "state": tmp_path / "image-exists", "tmp": tmp_path}


def test_build_context_dir_da_receita_versionada():
    ctx = toolchains.build_context_dir("autoia-android-emu:2026-09")
    assert ctx is not None and (ctx / "Dockerfile").is_file()
    # Tag diferente do mesmo nome de imagem usa o mesmo contexto.
    assert toolchains.build_context_dir("autoia-android-emu:outra") == ctx
    # Imagem sem receita versionada → None (sem build automático possível).
    assert toolchains.build_context_dir("autoia-sandbox") is None
    assert toolchains.build_context_dir("") is None
    assert toolchains.build_context_dir("  ") is None


def test_build_context_dir_respeita_env(monkeypatch, tmp_path):
    (tmp_path / "minhas-receitas" / "android-emulator-35").mkdir(parents=True)
    (tmp_path / "minhas-receitas" / "android-emulator-35" / "Dockerfile").write_text(
        "FROM scratch\n"
    )
    monkeypatch.setenv("AUTOIA_SANDBOX_DOCKER_DIR", str(tmp_path / "minhas-receitas"))
    ctx = toolchains.build_context_dir("autoia-android-emu:2026-09")
    assert ctx == tmp_path / "minhas-receitas" / "android-emulator-35"
    # Diretório inexistente → sem receita (não tenta build em path errado).
    monkeypatch.setenv("AUTOIA_SANDBOX_DOCKER_DIR", str(tmp_path / "nao-existe"))
    assert toolchains.build_context_dir("autoia-android-emu:2026-09") is None


def test_ensure_image_constrói_quando_a_imagem_sumiu(fake_docker, tmp_path):
    """Imagem apagada + receita no repo → constrói e devolve True."""
    image = "autoia-android-emu:2026-09"
    assert sandbox.docker_image_available(image) is False

    ok = sandbox.ensure_image(
        image,
        log_dir=str(tmp_path / "logs"),
        lock_dir=str(tmp_path / "ws"),
        timeout_s=60,
    )

    assert ok is True
    assert sandbox.docker_image_available(image) is True
    # O build foi disparado com a receita do repo e registrado em log.
    build_args = (tmp_path / "build-args.txt").read_text()
    assert "build" in build_args and image in build_args
    log = (tmp_path / "logs" / "docker_build_autoia-android-emu_2026-09.log").read_text()
    assert "docker" in log or "build" in log


def test_ensure_image_sem_receita_nao_tenta_build(fake_docker, tmp_path):
    """Imagem sem Dockerfile versionado → False e NENHUM build (fluxo antigo)."""
    ok = sandbox.ensure_image(
        "autoia-sandbox",
        log_dir=str(tmp_path / "logs"),
        lock_dir=str(tmp_path / "ws"),
        timeout_s=60,
    )
    assert ok is False
    assert not (tmp_path / "build-args.txt").exists()


def test_ensure_image_desligado_pela_env(fake_docker, tmp_path):
    """`AUTOIA_SANDBOX_BUILD_IMAGE=0` volta ao comportamento de bloquear."""
    ok = sandbox.ensure_image(
        "autoia-android-emu:2026-09",
        log_dir=str(tmp_path / "logs"),
        lock_dir=str(tmp_path / "ws"),
        timeout_s=60,
        enabled=False,
    )
    assert ok is False
    assert not (tmp_path / "build-args.txt").exists()


def test_ensure_image_ja_existente_nao_constrói(fake_docker, tmp_path):
    """Imagem presente → True sem rodar build nenhum."""
    image = "autoia-android-emu:2026-09"
    (tmp_path / "image-exists").write_text("ok")

    assert sandbox.ensure_image(
        image, log_dir=str(tmp_path / "logs"), lock_dir=str(tmp_path / "ws")
    )
    assert not (tmp_path / "build-args.txt").exists()


def test_ensure_image_nao_duplica_build_com_lock(fake_docker, tmp_path):
    """Outro processo com o lock da imagem → espera em vez de construir 2×."""
    import fcntl

    image = "autoia-android-emu:2026-09"
    lock_dir = tmp_path / "ws"
    lock_dir.mkdir()
    lock_path = lock_dir / ".image-build-autoia-android-emu_2026-09.lock"
    holder = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ok = sandbox.ensure_image(
            image,
            log_dir=str(tmp_path / "logs"),
            lock_dir=str(lock_dir),
            timeout_s=1,
        )
    finally:
        holder.close()

    assert ok is False  # ninguém construiu dentro da janela de espera
    assert not (tmp_path / "build-args.txt").exists()  # build não duplicou


def test_startup_lista_imagens_pendentes(flow, settings):
    """Na subida do worker, as imagens dos repositórios configurados são listadas
    (é o gatilho que reconstrói antes de uma fase cair em `infra_blocked`)."""
    from app.main import _pending_sandbox_images
    from app.models import Repository

    session_factory = flow["session_factory"]
    # Repo do teste nasce com sandbox off → nenhuma imagem pendente.
    assert _pending_sandbox_images(settings, session_factory) == []

    with session_factory() as s:
        repo = s.get(Repository, 1)
        repo.sandbox = "full"
        repo.sandbox_profile = "android-emulator-35"
        repo.sandbox_image = "autoia-android-emu:2026-09"
        s.commit()

    assert _pending_sandbox_images(settings, session_factory) == [
        "autoia-android-emu:2026-09"
    ]
