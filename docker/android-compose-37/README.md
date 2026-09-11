# `autoia-android-compose:2026-09`

Imagem de toolchain para o perfil `android-compose-37`.

```bash
docker build -t autoia-android-compose:2026-09 docker/android-compose-37
```

O checkout e as CLIs continuam sendo montados pelo worker. A imagem contém o
JDK e o Android SDK necessários para compilar o projeto, mas não contém um
emulador. Testes `connectedDebugAndroidTest` devem usar o runner Android/ADB
separado configurado para o host ou para um sidecar.

Depois de construir a imagem, configure o projeto via API/administração:

```json
{
  "sandbox": "full",
  "sandbox_profile": "android-compose-37",
  "sandbox_image": "autoia-android-compose:2026-09"
}
```

Para testes instrumentados (`connectedDebugAndroidTest`), o contêiner precisa
alcançar um servidor ADB com um emulador/dispositivo já conectado. No modo
`full`, o padrão é `tcp:host.docker.internal:5037`; inicie o servidor ADB do
host com `adb -a start-server` e restrinja a exposição no firewall local. Para
um runner/sidecar diferente, defina `AUTOIA_ANDROID_ADB_SERVER` (por exemplo,
`tcp:android-adb:5037`) no ambiente do worker.
