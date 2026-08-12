"""Skills por projeto: upload de `.zip` com `SKILL.md` na raiz.

Skills são conhecimento de domínio fornecido pelo usuário e materializadas no
checkout dos robôs nas fases seguintes (`.autoia/skills/`/`.opencode/skills/`),
sem poluir o git do repositório. Este módulo cuida da validação segura do `.zip`
(limites, path traversal), da extração para `data/skills/<repo_id>/<skill_id>/`,
do parse do frontmatter do `SKILL.md` e da exclusão do diretório no disco.
"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

# Limites do upload (blueprint): 5 MB no `.zip` recebido e 50 entradas no zip.
MAX_SKILL_ZIP_BYTES = 5 * 1024 * 1024
MAX_SKILL_ZIP_ENTRIES = 50
SKILL_MD = "SKILL.md"


class SkillZipError(ValueError):
    """Erro de validação do `.zip` de skill — mensagem em PT-BR exibida na UI."""


class SkillLimitError(SkillZipError):
    """Violação de limite (tamanho ou nº de entradas) do `.zip` de skill."""


def parse_skill_md(content: str) -> tuple[str | None, str | None]:
    """Extrai `name`/`description` do frontmatter de um `SKILL.md` (parse simples).

    Frontmatter: bloco delimitado por `---` na primeira linha (linhas
    `name: <valor>` / `description: <valor>`), sem dependência de PyYAML.
    Ausente/malformado → `(None, None)`; valores são as linhas após o `:`.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    name: str | None = None
    description: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "name" and name is None:
            name = value
        elif key == "description" and description is None:
            description = value
    return name, description


def skill_name_from_zip(zip_filename: str) -> str:
    """Nome default da skill: nome do arquivo `.zip` sem extensão (`docs.zip` → `docs`)."""
    return os.path.splitext(os.path.basename(zip_filename))[0]


def _safe_skill_name(name: str | None) -> bool:
    """True se `name` serve como segmento único de diretório (materialização segura).

    Rejeita vazio, `..`, `.` e qualquer separador de caminho (`/`, `\\`) — um nome
    com esses caracteres quebraria `.autoia/skills/<nome>/` no checkout.
    """
    if not name:
        return False
    return (
        name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and not name.startswith(("/", "\\"))
        and not (len(name) > 1 and name[1] == ":")
    )


def _validate_member(name: str) -> tuple[list[str], bool] | None:
    """Valida uma entrada do zip: retorna (partes do caminho, é_diretório) ou None.

    Entrada inválida: vazia, absoluta (prefixo `/`, `\\`, `C:`), ou com segmento
    vazio/`.`/`..` (ex.: `a//b`, `a/../b`). Entradas de diretório terminam em `/`;
    a barra invertida (`\\`) é tratada como separador (zips criados no Windows).
    """
    is_dir = name.endswith("/")
    normalized = name.rstrip("/")
    if not normalized:
        return None  # entrada vazia (ou só "/")
    if normalized.startswith("/") or normalized.startswith("\\"):
        return None
    if len(normalized) > 1 and normalized[1] == ":":
        return None  # drive letter (ex.: `C:\...`)
    parts = normalized.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts, is_dir


def validate_and_extract(
    zip_bytes: bytes,
    dest_dir: str | os.PathLike[str],
    zip_filename: str = "skill.zip",
) -> dict:
    """Valida e extrai um `.zip` de skill no diretório destino.

    Regras (ordem de verificação): tamanho ≤ 5 MB; ≤ 50 entradas; nenhuma entrada
    com path traversal/absoluto/vazia; entrada exatamente `SKILL.md` na raiz.
    Em qualquer violação lança `SkillZipError` (ou `SkillLimitError`) com a
    mensagem específica — **nada é extraído**. Retorna:
    `{name, description, file_count, size_bytes}` (nome/descrição do frontmatter
    do `SKILL.md`, com fallback do nome no nome do `.zip`).
    """
    if len(zip_bytes) > MAX_SKILL_ZIP_BYTES:
        raise SkillLimitError("arquivo muito grande (máx. 5 MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise SkillZipError("ZIP inválido: arquivo não é um .zip válido") from None

    dest = Path(dest_dir)
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_SKILL_ZIP_ENTRIES:
            raise SkillLimitError("muitos arquivos no zip (máx. 50)")
        # Valida TODAS as entradas antes de tocar o disco (erro = nada extraído).
        members: list[tuple[zipfile.ZipInfo, list[str], bool]] = []
        has_skill_md = False
        for info in infos:
            validated = _validate_member(info.filename)
            if validated is None:
                raise SkillZipError("caminho inválido no zip")
            parts, is_dir = validated
            if parts == [SKILL_MD] and not is_dir:
                has_skill_md = True
            members.append((info, parts, is_dir))
        if not has_skill_md:
            raise SkillZipError(f"ZIP inválido: falta {SKILL_MD} na raiz")

        dest.mkdir(parents=True, exist_ok=True)
        file_count = 0
        size_bytes = 0
        for info, parts, is_dir in members:
            target = dest.joinpath(*parts)
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Defesa em profundidade contra zip-slip (nomes já validados acima).
            target = target.resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                raise SkillZipError("caminho inválido no zip")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            file_count += 1
            size_bytes += info.file_size

    raw = (dest / SKILL_MD).read_text(encoding="utf-8", errors="replace")
    name, description = parse_skill_md(raw)
    fallback_name = skill_name_from_zip(zip_filename)
    if not _safe_skill_name(name):
        name = fallback_name
    return {
        "name": name,
        "description": description or "",
        "file_count": file_count,
        "size_bytes": size_bytes,
    }


def remove_skill_dir(skill_dir: str | os.PathLike[str]) -> None:
    """Remove o diretório da skill do disco (idempotente)."""
    shutil.rmtree(skill_dir, ignore_errors=True)
