"""Respostas condicionais (ETag / If-None-Match → 304) para os endpoints pollados.

O frontend faz polling agressivo; um ETag barato derivado de dados do banco
(updated_at / id máximos) permite reusar o corpo anterior sem re-transferir JSON.
O token é computado com queries de escalar (baratas) antes de montar o payload.
"""

from __future__ import annotations

import hashlib

from fastapi import Request, Response


def etag_for(token: str) -> str:
    """ETag weak a partir de um token arbitrário (256 bits, truncado)."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f'W/"{digest[:32]}"'


def conditional(request: Request, response: Response, token: str) -> Response | None:
    """Aplica o ETag no response (200) e, se `If-None-Match` bater, devolve um
    `Response(304)` que o handler deve retornar imediatamente (None → segue)."""
    etag = etag_for(token)
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return None
