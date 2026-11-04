# `autoia-android-emu:2026-09`

Imagem **self-contained** do perfil `android-emulator-35`: toolchain de build
(JDK 21 + Android SDK platform/build-tools) + **emulador** + **system image**
`android-35;google_apis;x86_64` bakeados na imagem.

O emulador sobe **dentro** do container de execução (bootstrap em
`app/worker/toolchains.py`, com `/dev/kvm` em passthrough), então os testes
instrumentados (`connectedDebugAndroidTest`) rodam sem emulador no host.

## Construir

```bash
docker build -t autoia-android-emu:2026-09 docker/android-emulator-35
```

A tag precisa bater com a config (`AUTOIA_SANDBOX_ANDROID_EMU_IMAGE` ou
`Repository.sandbox_image` do projeto).

## Reconstrução automática

A receita fica versionada aqui de propósito: a imagem já existiu **apenas** como
imagem local do Docker e uma limpeza de disco a apagou, deixando as tasks
android bloqueadas em `infra_blocked ... imagem indisponível (fail-closed)`.

Agora o worker detecta a imagem faltando e reconstrói antes de bloquear a fase
(`toolchains.ensure_image`, com lock entre workers e log em
`data/logs/docker_build_<tag>.log`). Desligue com
`AUTOIA_SANDBOX_BUILD_IMAGE=0`; limite de tempo em
`AUTOIA_SANDBOX_BUILD_TIMEOUT` (default 2400 s).

Depois de construir, configure o projeto via API/administração:

```json
{
  "sandbox": "full",
  "sandbox_profile": "android-emulator-35",
  "sandbox_image": "autoia-android-emu:2026-09"
}
```

## Requisitos

- `/dev/kvm` acessível no host (o sandbox passa o dispositivo quando o perfil
  pede `kvm=True`).
- A system image tem que casar com `_EMULATOR_SYSTEM_IMAGE` em
  `app/worker/toolchains.py` (o preflight verifica o caminho dentro da imagem).
