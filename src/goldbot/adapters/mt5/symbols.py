"""Resolución del símbolo y lectura de especificaciones reales con symbol_info()."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from goldbot.domain.types import SymbolSpec


def resolve_symbol(mt5: Any, requested: str, override: str | None = None) -> str:
    """Devuelve el nombre exacto del símbolo (con sufijo) o lanza un error claro."""
    candidate = override or requested
    info = mt5.symbol_info(candidate)
    if info is None:
        if not mt5.symbol_select(candidate, True):
            matches = sorted(
                item.name
                for item in (mt5.symbols_get() or [])
                if item.name.upper().startswith(requested.upper()) and item.trade_mode == 4
            )
            if not matches:
                raise LookupError(f"símbolo {requested!r} no encontrado en el terminal")
            if len(matches) > 1:
                raise LookupError(
                    f"símbolo {requested!r} ambiguo; candidatos: {matches}. "
                    "Fija mt5.symbol_override en el YAML."
                )
            candidate = matches[0]
            if not mt5.symbol_select(candidate, True):
                raise LookupError(f"no se pudo seleccionar el símbolo {candidate!r}")
        info = mt5.symbol_info(candidate)

    if info is None:
        raise LookupError(f"symbol_info devolvió None para {candidate!r}")
    if not info.visible and not mt5.symbol_select(candidate, True):
        raise LookupError(f"el símbolo {candidate!r} no está visible ni se pudo seleccionar")
    return candidate


def read_symbol_spec(mt5: Any, symbol: str, *, requested: str, now_utc: datetime) -> SymbolSpec:
    """Copia tick size/value, volumen, stops/freeze, fillings y trade_mode reales."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise LookupError(f"symbol_info devolvió None para {symbol!r}")

    spec = SymbolSpec(
        resolved_name=symbol,
        requested_name=requested,
        digits=int(info.digits),
        point=float(info.point),
        tick_size=float(info.trade_tick_size),
        trade_tick_value_loss=float(info.trade_tick_value_loss),
        trade_tick_value_profit=float(info.trade_tick_value_profit),
        contract_size=float(info.trade_contract_size),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        stops_level_points=int(info.trade_stops_level),
        freeze_level_points=int(info.trade_freeze_level),
        allowed_fillings=int(info.filling_mode),
        filling_mode=int(info.filling_mode),
        trade_mode=int(info.trade_mode),
        currency_profit=str(info.currency_profit),
        spec_hash="",
        fetched_at_utc=now_utc,
    )
    return replace(spec, spec_hash=compute_spec_hash(spec))


def compute_spec_hash(spec: SymbolSpec) -> str:
    """Huella de las especificaciones usadas, para auditar cada cálculo de lote."""
    payload = asdict(spec)
    payload.pop("spec_hash", None)
    payload.pop("fetched_at_utc", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
