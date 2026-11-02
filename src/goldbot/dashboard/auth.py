"""Autenticación por token para los comandos de escritura del dashboard."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Header, HTTPException


def verify_token(provided: str | None, expected: str) -> bool:
    """Compara en tiempo constante; un token vacío o ausente nunca es válido."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def token_dependency(expected_token: str) -> Callable[..., None]:
    """Dependencia de FastAPI que exige la cabecera `X-Dashboard-Token`."""

    def _require_token(
        x_dashboard_token: str | None = Header(default=None, alias="X-Dashboard-Token"),
    ) -> None:
        if not verify_token(x_dashboard_token, expected_token):
            raise HTTPException(status_code=401, detail="token del dashboard inválido")

    return _require_token
