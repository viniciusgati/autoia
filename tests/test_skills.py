"""Testes unitários do módulo de skills de projeto (`app/skills.py`)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.skills import (
    MAX_SKILL_ZIP_BYTES,
    MAX_SKILL_ZIP_ENTRIES,
    SkillLimitError,
    SkillZipError,
    parse_skill_md,
    remove_skill_dir,
    skill_name_from_zip,
    validate_and_extract,
)

SKILL_MD_OK = "---\nname: minha-skill\ndescription: Skill de exemplo\n---\n# Conteúdo\n"


def make_zip(entries: dict[str, bytes]) -> bytes:
    """Gera um .zip em memória com as entradas dadas (nome → conteúdo)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# validate_and_extract — caminho feliz
# ---------------------------------------------------------------------------


def test_validate_and_extract_valid_zip(tmp_path):
    zip_bytes = make_zip(
        {
            "SKILL.md": SKILL_MD_OK.encode(),
            "docs/guia.md": b"# guia\n",
            "scripts/": b"",  # entrada de diretório (não conta como arquivo)
            "scripts/run.sh": b"echo oi\n",
        }
    )
    dest = tmp_path / "skill"
    result = validate_and_extract(zip_bytes, dest, zip_filename="exemplo.zip")

    assert result["name"] == "minha-skill"
    assert result["description"] == "Skill de exemplo"
    assert result["file_count"] == 3  # SKILL.md + guia.md + run.sh
    assert result["size_bytes"] == len(SKILL_MD_OK.encode()) + len(b"# guia\n") + len(b"echo oi\n")
    assert (dest / "SKILL.md").is_file()
    assert (dest / "docs" / "guia.md").is_file()
    assert (dest / "scripts" / "run.sh").is_file()
    assert (dest / "SKILL.md").read_text() == SKILL_MD_OK


def test_validate_and_extract_fallback_name_without_frontmatter(tmp_path):
    zip_bytes = make_zip({"SKILL.md": b"# sem frontmatter\n", "extra.py": b"x = 1\n"})
    result = validate_and_extract(zip_bytes, tmp_path / "skill", zip_filename="docs.zip")
    assert result["name"] == "docs"  # fallback = nome do .zip sem extensão
    assert result["description"] == ""
    assert result["file_count"] == 2


def test_validate_and_extract_unsafe_frontmatter_name_falls_back(tmp_path):
    zip_bytes = make_zip({"SKILL.md": b"---\nname: ../evil\n---\n# x\n"})
    result = validate_and_extract(zip_bytes, tmp_path / "skill", zip_filename="ok.zip")
    assert result["name"] == "ok"  # nome inseguro não vira diretório no checkout


# ---------------------------------------------------------------------------
# validate_and_extract — erros específicos (nada é extraído)
# ---------------------------------------------------------------------------


def _assert_raises_and_not_extracted(tmp_path, zip_bytes, exc, message):
    dest = tmp_path / "skill"
    with pytest.raises(exc) as excinfo:
        validate_and_extract(zip_bytes, dest, zip_filename="bad.zip")
    assert message in str(excinfo.value)
    assert not dest.exists()  # validação falhou antes de tocar o disco


def test_validate_and_extract_missing_skill_md(tmp_path):
    zip_bytes = make_zip({"README.md": b"# sem skill"})
    _assert_raises_and_not_extracted(
        tmp_path, zip_bytes, SkillZipError, "ZIP inválido: falta SKILL.md na raiz"
    )


def test_validate_and_extract_skill_md_nested_is_not_enough(tmp_path):
    zip_bytes = make_zip({"sub/SKILL.md": b"# aninhado"})
    _assert_raises_and_not_extracted(
        tmp_path, zip_bytes, SkillZipError, "ZIP inválido: falta SKILL.md na raiz"
    )


@pytest.mark.parametrize("entry", ["../evil.txt", "a/../../evil", "..", ".", "a//b"])
def test_validate_and_extract_path_traversal(tmp_path, entry):
    zip_bytes = make_zip({"SKILL.md": b"# x\n", entry: b"x"})
    _assert_raises_and_not_extracted(tmp_path, zip_bytes, SkillZipError, "caminho inválido no zip")


@pytest.mark.parametrize("entry", ["/etc/passwd", "C:/windows", "C:\\windows", "\\server\\share"])
def test_validate_and_extract_absolute_path(tmp_path, entry):
    zip_bytes = make_zip({"SKILL.md": b"# x\n", entry: b"x"})
    _assert_raises_and_not_extracted(tmp_path, zip_bytes, SkillZipError, "caminho inválido no zip")


def test_validate_and_extract_empty_entry(tmp_path):
    zip_bytes = make_zip({"SKILL.md": b"# x\n", "": b"x"})
    _assert_raises_and_not_extracted(tmp_path, zip_bytes, SkillZipError, "caminho inválido no zip")


def test_validate_and_extract_too_many_entries(tmp_path):
    entries = {"SKILL.md": b"# x\n"}
    entries.update({f"f{i}.txt": b"x" for i in range(MAX_SKILL_ZIP_ENTRIES)})
    zip_bytes = make_zip(entries)  # 51 entradas (1 + 50)
    _assert_raises_and_not_extracted(tmp_path, zip_bytes, SkillLimitError, "muitos arquivos no zip (máx. 50)")


def test_validate_and_extract_too_large(tmp_path):
    zip_bytes = b"x" * (MAX_SKILL_ZIP_BYTES + 1)
    _assert_raises_and_not_extracted(tmp_path, zip_bytes, SkillLimitError, "arquivo muito grande (máx. 5 MB)")


def test_validate_and_extract_not_a_zip(tmp_path):
    _assert_raises_and_not_extracted(tmp_path, b"not a zip", SkillZipError, "não é um .zip válido")


def test_validate_and_extract_exact_limits_ok(tmp_path):
    # exatamente no limite: 50 entradas e 5 MB são aceitos
    entries = {"SKILL.md": b"# x\n"}
    entries.update({f"f{i}.txt": b"x" for i in range(MAX_SKILL_ZIP_ENTRIES - 1)})
    zip_bytes = make_zip(entries)
    assert len(zf_infolist(zip_bytes)) == MAX_SKILL_ZIP_ENTRIES
    result = validate_and_extract(zip_bytes, tmp_path / "skill")
    assert result["file_count"] == MAX_SKILL_ZIP_ENTRIES


def zf_infolist(zip_bytes: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.infolist()


# ---------------------------------------------------------------------------
# parse do frontmatter e nome a partir do .zip
# ---------------------------------------------------------------------------


def test_parse_skill_md_with_frontmatter():
    content = "---\nname: minha-skill\ndescription: Skill de exemplo\n---\n# Corpo\n"
    assert parse_skill_md(content) == ("minha-skill", "Skill de exemplo")


def test_parse_skill_md_without_frontmatter():
    assert parse_skill_md("# só conteúdo") == (None, None)
    assert parse_skill_md("") == (None, None)
    assert parse_skill_md("---\nname: sem fechamento") == ("sem fechamento", None)


def test_parse_skill_md_ignores_non_metadata_lines():
    content = "---\nname: x\nautor: alguém\ndescription: desc com : dois pontos\n---\n"
    assert parse_skill_md(content) == ("x", "desc com : dois pontos")


def test_skill_name_from_zip():
    assert skill_name_from_zip("docs.zip") == "docs"
    assert skill_name_from_zip("minha.skill.zip") == "minha.skill"
    assert skill_name_from_zip("/caminho/para/skill.zip") == "skill"


# ---------------------------------------------------------------------------
# Settings.skills_dir + ensure_dirs e exclusão
# ---------------------------------------------------------------------------


def test_settings_ensure_dirs_creates_skills_dir(tmp_path):
    skills_dir = tmp_path / "skills"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "workspaces"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(skills_dir),
    )
    settings.ensure_dirs()
    assert skills_dir.is_dir()


def test_settings_skills_dir_default_and_env(monkeypatch):
    monkeypatch.delenv("AUTOIA_SKILLS_DIR", raising=False)
    assert Settings().skills_dir == "data/skills"
    monkeypatch.setenv("AUTOIA_SKILLS_DIR", str(Path("outro/skills")))
    assert Settings().skills_dir == "outro/skills"


def test_remove_skill_dir(tmp_path):
    skill_dir = tmp_path / "skills" / "1" / "1"
    (skill_dir / "nested").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# x")
    remove_skill_dir(skill_dir)
    assert not skill_dir.exists()
    remove_skill_dir(skill_dir)  # idempotente
