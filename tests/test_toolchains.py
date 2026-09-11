"""Perfis de toolchain por projeto e preflight determinístico."""

from __future__ import annotations

from app.worker import toolchains
from app.worker.sandbox import SandboxConfig, build_sandbox_command, runtime_environment


def test_android_profile_is_self_contained_and_has_preflight():
    profile = toolchains.get_profile(
        "android-compose-37",
        android_image="autoia-android-compose:2026-09",
        sandbox_enabled=True,
    )

    assert profile.image == "autoia-android-compose:2026-09"
    assert profile.mount_system_ro is False
    assert profile.environment["JAVA_HOME"] == "/opt/java/openjdk"
    assert profile.environment["ANDROID_HOME"] == "/opt/android-sdk"
    assert any("android-37.0" in command for command in profile.preflight)
    assert any("build-tools/36.0.0" in command for command in profile.preflight)
    assert profile.device_preflight


def test_android_profile_uses_host_paths_when_sandbox_is_off():
    profile = toolchains.get_profile(
        "android-compose-37",
        android_image="image",
        sandbox_enabled=False,
        host_java_home="/host/jdk",
        host_android_home="/host/sdk",
    )

    assert profile.environment["JAVA_HOME"] == "/host/jdk"
    assert profile.environment["ANDROID_HOME"] == "/host/sdk"


def test_android_profile_can_use_external_adb_server():
    profile = toolchains.get_profile(
        "android-compose-37",
        android_image="image",
        adb_server_socket="tcp:host.docker.internal:5037",
    )

    assert profile.environment["ADB_SERVER_SOCKET"] == "tcp:host.docker.internal:5037"


def test_self_contained_image_does_not_mount_host_system_dirs(tmp_path):
    checkout = tmp_path / "checkout"
    workspace = tmp_path / "workspace"
    checkout.mkdir()
    workspace.mkdir()
    config = SandboxConfig(
        mode="full",
        profile="android-compose-37",
        image="autoia-android-compose:2026-09",
        mount_system_ro=False,
        environment={"JAVA_HOME": "/opt/java/openjdk"},
    )

    command = build_sandbox_command(
        ["/bin/bash", "-lc", "java -version"],
        config=config,
        checkout=str(checkout),
        workspace_dir=str(workspace),
        cli_bin="/bin/bash",
    )
    joined = " ".join(command or [])
    assert "/usr:/usr:ro" not in joined
    assert "JAVA_HOME=/opt/java/openjdk" in joined


def test_runtime_environment_prepends_profile_toolchain():
    config = SandboxConfig(
        mode="off",
        environment={
            "JAVA_HOME": "/host/jdk",
            "ANDROID_HOME": "/host/sdk",
        },
    )
    env = runtime_environment(config)
    assert env["JAVA_HOME"] == "/host/jdk"
    assert env["ANDROID_HOME"] == "/host/sdk"
    assert env["PATH"].split(":")[:2] == ["/host/jdk/bin", "/host/jdk/platform-tools"]


def test_android_emulator_profile_boots_emulator_self_contained():
    """O perfil `android-emulator-35` é self-contained: KVM + bootstrap que sobe
    o emulador DENTRO do container (sem depender de emulador no host) e sem
    device_preflight (a garantia de device é o bootstrap, não o preflight)."""
    profile = toolchains.get_profile(
        "android-emulator-35",
        android_image="autoia-android-compose:2026-09",
        android_emu_image="autoia-android-emu:2026-09",
        sandbox_enabled=True,
    )

    assert profile.name == "android-emulator-35"
    assert profile.image == "autoia-android-emu:2026-09"
    assert profile.mount_system_ro is False
    assert profile.kvm is True
    assert profile.bootstrap_extra
    assert "emulator" in profile.bootstrap_extra
    assert "sys.boot_completed" in profile.bootstrap_extra
    assert profile.environment["AUTOIA_ANDROID_EMULATOR"] == "1"
    assert profile.environment["AUTOIA_ANDROID_SYSTEM_IMAGE"].startswith("system-images;")
    # toolchain de build presente (mesma do compose)
    assert any("android-37.0" in command for command in profile.preflight)
    assert any("emulator" in command for command in profile.preflight)
    # device garantido pelo bootstrap — device_preflight vazio
    assert not profile.device_preflight


def test_android_emulator_profile_no_kvm_when_sandbox_off():
    """Com sandbox desligado não há container nem KVM — é escolha explícita de
    diagnóstico (host usa toolchain do host)."""
    profile = toolchains.get_profile(
        "android-emulator-35",
        android_image="x",
        android_emu_image="y",
        sandbox_enabled=False,
        host_java_home="/host/jdk",
        host_android_home="/host/sdk",
    )
    assert profile.environment["JAVA_HOME"] == "/host/jdk"
    assert profile.environment["ANDROID_HOME"] == "/host/sdk"
