"""Aplicación FastAPI del dashboard: proceso separado del bot, solo loopback.

El dashboard nunca importa `MetaTrader5` ni construye el bot: recibe la ruta de la base y
lee lo que el bot publicó. La única escritura que hace es encolar comandos, y esos pasan
por el StateStore con token. Los estáticos se sirven en local (sin CDN), así que la página
funciona sin internet.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from goldbot import __version__
from goldbot.dashboard.api import build_command_router, build_read_router
from goldbot.dashboard.readmodel import DashboardReadModel
from goldbot.domain.interfaces import Clock, StateStore

STATIC_DIR = Path(__file__).parent / "static"


class _LocalHostHeaders(BaseHTTPMiddleware):
    """`no-store` en la API y cabeceras de un servidor que nunca sale de la máquina."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def create_app(
    *,
    db_path: str | Path,
    config: Any,
    clock: Clock | None = None,
    store: StateStore | None = None,
) -> FastAPI:
    """Construye la app con routers de lectura y comandos protegidos por token."""
    if clock is None:
        from goldbot.engine.data_service import build_clock  # noqa: PLC0415 - evita ciclo

        clock = build_clock(config)
    if store is None:
        from goldbot.infra.state_store import SqliteStateStore  # noqa: PLC0415

        store = SqliteStateStore(db_path, clock)

    read_model = DashboardReadModel(
        db_path, clock, stale_after_s=config.dashboard.stale_after_s
    )

    # `docs_url=None`: la UI de Swagger carga assets de CDN y el dashboard debe funcionar
    # sin internet; el JSON de esquema sigue disponible en /openapi.json para pruebas.
    app = FastAPI(
        title="goldbot dashboard",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(_LocalHostHeaders)
    app.state.config = config
    app.state.clock = clock
    app.state.store = store
    app.state.read_model = read_model

    app.include_router(
        build_read_router(
            rm=read_model,
            clock=clock,
            config=config,
            stale_after_s=config.dashboard.stale_after_s,
        )
    )
    app.include_router(
        build_command_router(store=store, config=config, rm=read_model, clock=clock)
    )

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        """Un fallo leyendo nunca derriba el panel: devuelve 500 con el detalle mínimo."""
        return JSONResponse(status_code=500, content={"detail": f"error interno: {exc}"})

    # `mimetypes` lee el registro de Windows al iniciarse (`read_windows_registry`) y cada
    # `HKCR\<ext>` con valor `Content Type` se sobrepone a la tabla base de Python. En una máquina
    # con `HKCR\.js = text/plain` esto hacía que los scripts salieran como texto plano y el
    # navegador los rechazara en silencio por el `nosniff` del middleware de arriba: página con
    # estilos y vacía de datos, sin ningún aviso. El panel no puede depender de lo que diga el
    # registro de la máquina, así que el tipo se fija aquí. Sin `init()`: llamarla reconstruye la
    # tabla y vuelve a leer el registro, deshaciendo este `add_type` — y `add_type` ya inicializa
    # perezosamente si la tabla todavía no existe.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
