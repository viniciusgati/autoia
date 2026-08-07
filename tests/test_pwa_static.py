"""Testes do servidor de estáticos (PWA) a partir do dist buildado."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_api_serves_frontend_dist(settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>autoia pwa</body></html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "manifest.webmanifest").write_text('{"name":"autoia","start_url":"/"}')
    settings.frontend_dist = str(dist)

    client = TestClient(create_app(settings))

    assert "autoia pwa" in client.get("/").text
    assert client.get("/manifest.webmanifest").json()["name"] == "autoia"
    assert client.get("/assets/app.js").status_code == 200
    # fallback SPA: rota client-side cai no index.html
    assert "autoia pwa" in client.get("/tasks/42").text
    # API continua funcionando na mesma origem
    assert client.get("/api/robots").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


def test_api_without_dist_returns_404(settings):
    settings.frontend_dist = None
    client = TestClient(create_app(settings))
    assert client.get("/").status_code == 404
    assert client.get("/tasks/42").status_code == 404
