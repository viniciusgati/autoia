# Imagem self-contained para projetos Android Kotlin/Compose.
# Construir e publicar com uma tag imutável, por exemplo:
#   docker build -t autoia-android-compose:2026-09 docker/android-compose-37
# O worker monta o checkout, mas NÃO monta /usr, /lib ou o SDK do host nesta
# imagem. Isso evita que a toolchain de um projeto dependa do ambiente do host.

# Digest pinado na construção de 2026-09; atualizações devem gerar outra tag.
FROM eclipse-temurin:21-jdk-jammy@sha256:ce5767b7222312d42395f5bab033cd91f09e44032a2f21bdfd7b5b912dbe1e77

ARG ANDROID_SDK_ROOT=/opt/android-sdk
ARG ANDROID_CMDLINE_TOOLS=11076708

ENV JAVA_HOME=/opt/java/openjdk \
    ANDROID_HOME=${ANDROID_SDK_ROOT} \
    ANDROID_SDK_ROOT=${ANDROID_SDK_ROOT} \
    PATH=${ANDROID_SDK_ROOT}/platform-tools:${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:/opt/java/openjdk/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        file \
        git \
        unzip \
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
    && yes | sdkmanager --licenses >/dev/null || true \
    && chmod -R a+rX "${ANDROID_SDK_ROOT}"

WORKDIR /workspace

# O worker usa --user UID:GID do host; não executar a CLI como root.
ENTRYPOINT ["/usr/bin/env"]
