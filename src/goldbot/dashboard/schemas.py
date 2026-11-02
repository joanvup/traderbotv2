"""Esquemas de entrada/salida del dashboard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DashboardCommand = Literal["pause", "resume", "close_all", "reset_kill_switch"]


class CommandRequest(BaseModel):
    """Comando de escritura; la clave de idempotencia evita duplicados por reintentos."""

    command: DashboardCommand
    idempotency_key: str = Field(min_length=8, max_length=128)


class CommandResponse(BaseModel):
    command_id: str
    status: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db_reachable: bool
    feed_age_s: float | None
