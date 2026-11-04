# Imagem SELF-CONTAINED para o perfil `android-emulator-35`.
#
# Toolchain de build (idêntica à do compose-37) + emulador + system image.
# O emulador bota DENTRO do container de execução (bootstrap em
# `app/worker/toolchains.py`), com `/dev/kvm` em passthrough — os testes
# instrumentados rodam sem depender de emulador no host.
#
# Construir com a tag imutável usada na config (`AUTOIA_SANDBOX_ANDROID_EMU_IMAGE`
# / `Repository.sandbox_image`):
#   docker build -t autoia-android-emu:2026-09 docker/android-emulator-35
#
# Esta receita fica VERSIONADA de propósito: a imagem já existiu apenas como
# imagem local do Docker e uma limpeza de disco a apagou, deixando todas as
# tasks android bloqueadas em `infra_blocked ... imagem indisponível`. Agora o
# worker reconstrói sozinho quando some — `toolchains.ensure_image`.
#
# Digest pinado na construção de 2026-09; atualizações devem gerar outra tag.
FROM eclipse-temurin:21-jdk-jammy@sha256:ce5767b7222312d42395f5bab033cd91f09e44032a2f21bdfd7b5b912dbe1e77

ARG ANDROID_SDK_ROOT=/opt/android-sdk
ARG ANDROID_CMDLINE_TOOLS=11076708
# System image usada pelo bootstrap do emulador (tem que casar com
# `_EMULATOR_SYSTEM_IMAGE` em toolchains.py).
ARG EMULATOR_SYSTEM_IMAGE="system-images;android-35;google_apis;x86_64"

ENV JAVA_HOME=/opt/java/openjdk \
    ANDROID_HOME=${ANDROID_SDK_ROOT} \
    ANDROID_SDK_ROOT=${ANDROID_SDK_ROOT} \
    PATH=${ANDROID_SDK_ROOT}/platform-tools:${ANDROID_SDK_ROOT}/emulator:${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:/opt/java/openjdk/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        dbus \
        file \
        fonts-dejavu-core \
        git \
        unzip \
        # Dependências de runtime do emulador headless (sem elas o processo
        # morre com "error while loading shared libraries" antes de logar).
        libasound2 \
        libfontconfig1 \
        libgl1 \
        libglib2.0-0 \
        libnss3 \
        libpulse0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxtst6 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools" \
    && curl --fail --silent --show-error --location \
        "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS}_latest.zip" \
        -o /tmp/cmdline-tools.zip \
    && unzip -q /tmp/cmdline-tools.zip -d /tmp/android-cmdline-tools \
    && mv /tmp/android-cmdline-tools/cmdline-tools "${ANDROID_SDK_ROOT}/cmdline-tools/latest" \
    && rm -rf /tmp/cmdline-tools.zip /tmp/android-cmdline-tools \
    && yes | sdkmanager --licenses >/dev/null || true \
    && sdkmanager \
        "platform-tools" \
        "platforms;android-37.0" \
        "build-tools;36.0.0" \
        "emulator" \
        "${EMULATOR_SYSTEM_IMAGE}" \
    && yes | sdkmanager --licenses >/dev/null || true \
    && chmod -R a+rX "${ANDROID_SDK_ROOT}"

WORKDIR /workspace

# O worker usa --user UID:GID do host; não executar a CLI como root.
ENTRYPOINT ["/usr/bin/env"]
