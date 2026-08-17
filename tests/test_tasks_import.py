"""Testes do endpoint `POST /api/tasks/description-from-file` (import de descrição).

Cobrem: sucesso (.md/.txt, com e sem BOM, extensão case-insensitive, fronteira
de 100 KB) e rejeições (extensão inválida, tamanho > 100 KB, arquivo vazio e
bytes não-UTF-8) — sempre com `400` e mensagem específica. Os arquivos de
entrada são gerados em `tmp_path`, sem fixtures externas.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

MAX_BYTES = 100 * 1024  # espelha MAX_DESCRIPTION_FILE_BYTES do endpoint


@pytest.fixture
def client(settings, bare_repo):
    app = create_app(settings)
    return TestClient(app)


def _post_file(client: TestClient, name: str, content: bytes):
    return client.post(
        "/api/tasks/description-from-file",
        files={"file": (name, content, "application/octet-stream")},
    )


def test_description_from_markdown_file(client, tmp_path):
    content = "# Especificação\n\n- item 1\n- item 2\n".encode("utf-8")
    path = tmp_path / "spec.md"
    path.write_bytes(content)

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 200, response.text
    assert response.json() == {"description": "# Especificação\n\n- item 1\n- item 2\n"}


def test_description_from_txt_file_with_bom(client, tmp_path):
    content = "texto sem BOM: á é ã ç".encode("utf-8")
    with_bom = b"\xef\xbb\xbf" + content
    path = tmp_path / "desc.txt"
    path.write_bytes(with_bom)

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 200, response.text
    # BOM inicial removido pela decodificação utf-8-sig
    assert response.json() == {"description": content.decode("utf-8")}


def test_description_file_extension_case_insensitive(client, tmp_path):
    content = b"conteudo em maiusculas"
    path = tmp_path / "SPEC.MD"
    path.write_bytes(content)

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 200, response.text
    assert response.json() == {"description": "conteudo em maiusculas"}


def test_description_file_exactly_100kb_is_valid(client, tmp_path):
    content = b"a" * MAX_BYTES
    path = tmp_path / "exato.md"
    path.write_bytes(content)

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 200, response.text
    assert len(response.json()["description"]) == MAX_BYTES


def test_description_file_invalid_extension(client, tmp_path):
    path = tmp_path / "spec.pdf"
    path.write_bytes(b"conteudo")

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 400, response.text
    assert "extensão não permitida" in response.json()["detail"]
    assert ".txt" in response.json()["detail"] and ".md" in response.json()["detail"]


def test_description_file_no_extension(client, tmp_path):
    path = tmp_path / "sem_extensao"
    path.write_bytes(b"conteudo")

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 400, response.text
    assert "extensão não permitida" in response.json()["detail"]


def test_description_file_too_large(client, tmp_path):
    path = tmp_path / "grande.md"
    path.write_bytes(b"a" * (MAX_BYTES + 1))

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 400, response.text
    assert "muito grande" in response.json()["detail"]


def test_description_file_empty(client, tmp_path):
    path = tmp_path / "vazio.md"
    path.write_bytes(b"")

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 400, response.text
    assert "arquivo vazio" in response.json()["detail"]


def test_description_file_invalid_utf8(client, tmp_path):
    path = tmp_path / "binario.md"
    path.write_bytes(b"caf\xe9")

    response = _post_file(client, path.name, path.read_bytes())

    assert response.status_code == 400, response.text
    assert "UTF-8" in response.json()["detail"]
