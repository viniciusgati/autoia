"""Perfis de toolchain usados pelas execuções dos robôs.

O sandbox genérico monta apenas os runtimes das CLIs. Projetos que precisam de
uma toolchain pesada (Android, por exemplo) usam um perfil administrado que
define uma imagem imutável, variáveis de ambiente e um preflight determinístico.
Assim o agente não precisa adivinhar onde estão JDK/SDK e uma ausência de
infraestrutura não é confundida com defeito no código do projeto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROFILE_GENERIC = "generic"
PROFILE_ANDROID_COMPOSE_37 = "android-compose-37"
PROFILE_ANDROID_EMULATOR_35 = "android-emulator-35"

# System image embutida na imagem do perfil do emulador (bake no build).
_EMULATOR_SYSTEM_IMAGE = "system-images;android-35;google_apis;x86_64"

# Comandos de bootstrap do emulador DENTRO do container de execução: cria o AVD
# (se ausente) em área host-backed gravável ($AUTOIA_ANDROID_AVD_HOME), inicia o
# emulador headless com KVM, aguarda o boot e prepara o device (desabilita
# concorrentes de resolução de MIME e desbloqueia a tela). Falha no boot = exit
# 71 (mesma convenção do device_preflight). O `set -eu` do bootstrap não pode
# abortar no poll de boot — usa-se `if`, não `&&`, para checar o estado.
_EMULATOR_BOOTSTRAP = r"""
if [ "${AUTOIA_ANDROID_EMULATOR:-}" = "1" ]; then
  export ANDROID_AVD_HOME="${AUTOIA_ANDROID_AVD_HOME:-$HOME/.android/avd}"
  mkdir -p "$ANDROID_AVD_HOME"
  if [ ! -f "$ANDROID_AVD_HOME/autoia.ini" ]; then
    echo no | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd -n autoia \
      -d pixel_4 -k "${AUTOIA_ANDROID_SYSTEM_IMAGE:-}" --force >/dev/null 2>&1 || true
  fi
  nohup "$ANDROID_HOME/emulator/emulator" -avd autoia \
    -no-window -no-audio -no-boot-anim -no-snapshot -no-snapshot-save \
    -gpu swiftshader_indirect -accel auto -memory 2048 \
    > "$AUTOIA_EMULATOR_LOG" 2>&1 &
  timeout 120 "$ANDROID_HOME/platform-tools/adb" wait-for-device >/dev/null 2>&1 || true
  booted=""
  for _ in $(seq 1 60); do
    b=$("$ANDROID_HOME/platform-tools/adb" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [ "$b" = "1" ]; then booted="1"; break; fi
    sleep 5
  done
  if [ -z "$booted" ]; then
    echo "emulador android não bootou em 5min (log: $AUTOIA_EMULATOR_LOG)" >&2
    exit 71
  fi
  "$ANDROID_HOME/platform-tools/adb" shell pm disable-user --user 0 com.android.chrome >/dev/null 2>&1 || true
  "$ANDROID_HOME/platform-tools/adb" shell pm disable-user --user 0 com.android.htmlviewer >/dev/null 2>&1 || true
  # Lockscreen segura: o AVD é fresco (por execução) e sem PIN o keystore não
  # cria chaves com autenticação de usuário ("Secure lock screen must be
  # enabled") — os testes de vault falham. Define PIN 1234 (mesma convenção do
  # test_env.md do projeto) e desbloqueia.
  "$ANDROID_HOME/platform-tools/adb" shell locksettings set-pin 1234 >/dev/null 2>&1 || true
  # Tela nunca dorme: se o keyguard re-engajar durante um build longo (gradle),
  # a suíte instrumentada falha com "No compose hierarchies found in the app".
  "$ANDROID_HOME/platform-tools/adb" shell settings put system screen_off_timeout 2147483647 >/dev/null 2>&1 || true
  # Desbloqueio VERIFICADO com retry: o AVD é fresco (por execução) e o keyguard
  # pode não estar pronto logo após o boot — um swipe/input único se perde em
  # silêncio. Repete a sequência até `isKeyguardShowing=false` (ou desiste).
  # Swipe com coordenadas DINÂMICAS (wm size): o AVD usa o profile pixel_4
  # (1080x2280), mas qualquer resolução funciona sem chutar coordenadas. O
  # swipe precisa COMEÇAR na área de gesto do rodapé (h-60) e subir quase a tela
  # inteira (h/20); swipes menores (ex.: 1800->800) não revelam o PIN pad.
  # keyevent 224 = WAKEUP (não alterna a tela como 26/POWER, que num loop
  # desligava o display nas iterações pares) e o check só roda depois que o
  # unlock processa (sleep 2) — sem isso o loop nunca via `false` e desistia.
  "$ANDROID_HOME/platform-tools/adb" shell input keyevent 223 >/dev/null 2>&1 || true
  unlocked=""
  size=$("$ANDROID_HOME/platform-tools/adb" shell wm size 2>/dev/null | tr -d '\r' | awk '/Physical size/ {print $3}')
  w="${size%%x*}"; h="${size##*x}"
  [ -n "$w" ] || w=1080; [ -n "$h" ] || h=2280
  sw="$((w/2)) $((h-60)) $((w/2)) $((h/20)) 400"
  for _ in $(seq 1 5); do
    "$ANDROID_HOME/platform-tools/adb" shell input keyevent 224 >/dev/null 2>&1 || true
    sleep 1
    "$ANDROID_HOME/platform-tools/adb" shell input swipe $sw >/dev/null 2>&1 || true
    sleep 1
    "$ANDROID_HOME/platform-tools/adb" shell input text 1234 >/dev/null 2>&1 || true
    sleep 1
    "$ANDROID_HOME/platform-tools/adb" shell input keyevent 66 >/dev/null 2>&1 || true
    sleep 2
    k=$("$ANDROID_HOME/platform-tools/adb" shell dumpsys window 2>/dev/null | grep -c "isKeyguardShowing=false" || true)
    if [ "$k" -ge 1 ]; then unlocked="1"; break; fi
    sleep 2
  done
  if [ -z "$unlocked" ]; then
    echo "aviso: não foi possível desbloquear o keyguard do emulador" >&2
  fi
fi
"""


@dataclass(frozen=True)
class ToolchainProfile:
    name: str
    image: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    # Quando False, a imagem é dona do runtime (JDK/SDK/libc). Montar /usr do
    # host por cima dela destruiria a reprodutibilidade da imagem.
    mount_system_ro: bool = True
    preflight: tuple[str, ...] = ()
    device_preflight: tuple[str, ...] = ()
    # Passa /dev/kvm ao container (emulador acelerado por hardware) — exige
    # também o grupo do device no host (--group-add) para o uid do worker.
    kvm: bool = False
    # Comandos extras do bootstrap do container, rodados ANTES da CLI do agente
    # (ex.: subir o emulador Android dentro do container). Executados em $HOME
    # tmpfs + área host-backed do AVD — não alteram o checkout.
    bootstrap_extra: str = ""


def get_profile(
    name: str | None,
    *,
    android_image: str,
    android_emu_image: str = "",
    sandbox_enabled: bool = True,
    host_java_home: str = "",
    host_android_home: str = "",
    adb_server_socket: str = "",
) -> ToolchainProfile:
    """Retorna um perfil conhecido; nomes desconhecidos caem no genérico.

    Perfis são allowlisted no código. A imagem pode ser pinada pelo administrador
    no projeto, mas a task/LLM nunca fornece um nome de imagem diretamente.
    """
    normalized = (name or PROFILE_GENERIC).strip().lower()
    if normalized == PROFILE_ANDROID_COMPOSE_37:
        java_home = "/opt/java/openjdk" if sandbox_enabled else host_java_home
        android_home = "/opt/android-sdk" if sandbox_enabled else host_android_home
        environment = {
            "JAVA_HOME": java_home,
            "ANDROID_HOME": android_home,
            "ANDROID_SDK_ROOT": android_home,
            # Cache efêmero no tmpfs do sandbox; o Dockerfile já contém a
            # toolchain, portanto não precisamos escrever no host.
            "GRADLE_USER_HOME": "/tmp/autoia-gradle",
            # O Codex pode disparar mais de uma verificação Gradle em paralelo.
            # Sem esta contenção cada comando cria um daemon de até 2 GB e o
            # container alcança o limite de threads antes dos testes Android.
            "GRADLE_OPTS": "-Dorg.gradle.daemon=false -Dorg.gradle.workers.max=2 -Dorg.gradle.jvmargs=-Xmx1536m",
            "AUTOIA_TOOLCHAIN_PROFILE": PROFILE_ANDROID_COMPOSE_37,
        }
        if adb_server_socket:
            environment["ADB_SERVER_SOCKET"] = adb_server_socket
        return ToolchainProfile(
            name=PROFILE_ANDROID_COMPOSE_37,
            image=android_image,
            environment=environment,
            mount_system_ro=False,
            preflight=(
                'test -x "$JAVA_HOME/bin/java" || { echo "JDK ausente em $JAVA_HOME" >&2; exit 70; }',
                'test -x "$ANDROID_HOME/platform-tools/adb" || { echo "Android SDK/platform-tools ausente em $ANDROID_HOME" >&2; exit 70; }',
                'test -d "$ANDROID_HOME/platforms/android-37.0" || { echo "Android SDK platform android-37.0 ausente" >&2; exit 70; }',
                'test -d "$ANDROID_HOME/build-tools/36.0.0" || { echo "Android Build Tools 36.0.0 ausente" >&2; exit 70; }',
                '"$JAVA_HOME/bin/java" -version',
                '"$ANDROID_HOME/platform-tools/adb" version',
            ),
            device_preflight=(
                'devices=0; for _ in $(seq 1 30); do devices=$("$ANDROID_HOME/platform-tools/adb" devices | awk "NR > 1 && \\$2 == \\"device\\" { count++ } END { print count + 0 }"); test "$devices" -gt 0 && break; sleep 1; done',
                'test "$devices" -gt 0 || { echo "nenhum dispositivo/emulador Android conectado após 30s" >&2; exit 71; }',
            ),
        )
    if normalized == PROFILE_ANDROID_EMULATOR_35:
        # Imagem SELF-CONTAINED: toolchain de build (idêntica à do compose) +
        # emulador + system image. O emulador bota DENTRO do container de
        # execução (bootstrap), com KVM passthrough — os testes instrumentados
        # rodam sem depender de emulador no host. O `device_preflight` fica
        # vazio: a garantia de device é o bootstrap (exit 71 se não bootar),
        # não o preflight (que roda num container separado e rápido).
        java_home = "/opt/java/openjdk" if sandbox_enabled else host_java_home
        android_home = "/opt/android-sdk" if sandbox_enabled else host_android_home
        environment = {
            "JAVA_HOME": java_home,
            "ANDROID_HOME": android_home,
            "ANDROID_SDK_ROOT": android_home,
            "GRADLE_USER_HOME": "/tmp/autoia-gradle",
            "GRADLE_OPTS": "-Dorg.gradle.daemon=false -Dorg.gradle.workers.max=2 -Dorg.gradle.jvmargs=-Xmx1536m",
            "AUTOIA_TOOLCHAIN_PROFILE": PROFILE_ANDROID_EMULATOR_35,
            # Marca para o sandbox injetar o bootstrap do emulador + áreas do AVD.
            "AUTOIA_ANDROID_EMULATOR": "1",
            "AUTOIA_ANDROID_SYSTEM_IMAGE": _EMULATOR_SYSTEM_IMAGE,
        }
        return ToolchainProfile(
            name=PROFILE_ANDROID_EMULATOR_35,
            image=android_emu_image,
            environment=environment,
            mount_system_ro=False,
            preflight=(
                'test -x "$JAVA_HOME/bin/java" || { echo "JDK ausente em $JAVA_HOME" >&2; exit 70; }',
                'test -x "$ANDROID_HOME/platform-tools/adb" || { echo "Android SDK/platform-tools ausente em $ANDROID_HOME" >&2; exit 70; }',
                'test -d "$ANDROID_HOME/platforms/android-37.0" || { echo "Android SDK platform android-37.0 ausente" >&2; exit 70; }',
                'test -d "$ANDROID_HOME/build-tools/36.0.0" || { echo "Android Build Tools 36.0.0 ausente" >&2; exit 70; }',
                'test -x "$ANDROID_HOME/emulator/emulator" || { echo "emulador ausente em $ANDROID_HOME/emulator" >&2; exit 70; }',
                'test -d "$ANDROID_HOME/system-images/android-35/google_apis/x86_64" || { echo "system image android-35 ausente" >&2; exit 70; }',
                '"$JAVA_HOME/bin/java" -version',
                '"$ANDROID_HOME/platform-tools/adb" version',
            ),
            kvm=True,
            bootstrap_extra=_EMULATOR_BOOTSTRAP,
        )
    return ToolchainProfile(name=PROFILE_GENERIC)


def preflight_script(profile: ToolchainProfile, *, require_device: bool = False) -> str | None:
    """Monta um script pequeno executado no mesmo ambiente do robô."""
    checks = list(profile.preflight)
    if require_device:
        checks.extend(profile.device_preflight)
    if not checks:
        return None
    return "set -eu\n" + "\n".join(checks) + "\n"
