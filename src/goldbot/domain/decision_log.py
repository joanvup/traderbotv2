"""Registro JSONL de decisiones, con la misma huella que la tabla `decisions`."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from goldbot.domain.types import DecisionRecord, FilterOutcome, Signal


def _iso(value: datetime) -> str:
    return value.isoformat()


def _signal_payload(signal: Signal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "signal_id": signal.signal_id,
        "playbook_id": signal.playbook_id,
        "side": signal.side.value,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "risk_distance": signal.risk_distance,
        "confidence": signal.confidence,
        "regime_at_signal": signal.regime_at_signal.value,
        "created_at_utc": _iso(signal.created_at_utc),
        "valid_until_bar_utc": (
            _iso(signal.valid_until_bar_utc) if signal.valid_until_bar_utc else None
        ),
        "invalidation_price": signal.invalidation_price,
        "rationale": signal.rationale,
        "inputs_hash": signal.inputs_hash,
    }


def _filters_payload(outcome: FilterOutcome | None) -> dict[str, Any] | None:
    if outcome is None:
        return None
    return {
        "blocked": outcome.blocked,
        "primary_reason": outcome.primary_reason,
        "results": [
            {
                "code": result.code.value,
                "blocked": result.blocked,
                "observed": result.observed,
                "threshold": result.threshold,
                "message": result.message,
            }
            for result in outcome.results
        ],
    }


def decision_payload(record: DecisionRecord) -> dict[str, Any]:
    """Convierte la decisión en un diccionario estable y legible."""
    return {
        "decision_id": record.decision_id,
        "cycle_id": record.cycle_id,
        "ts_utc": _iso(record.ts_utc),
        "bar_time_utc": _iso(record.bar_time_utc),
        "action": record.action,
        "playbook_id": record.playbook_id,
        "snapshot_hash": record.snapshot.snapshot_hash,
        "config_hash": record.config_hash,
        "model_versions": dict(record.model_versions),
        "regime": {
            "label": record.regime.label.value,
            "confidence": record.regime.confidence,
            "confirmed_bars": record.regime.confirmed_bars,
            "method": record.regime.probs.method,
            "probs": {regime.value: value for regime, value in record.regime.probs.probs.items()},
        },
        "signal": _signal_payload(record.signal),
        "filters": _filters_payload(record.filters),
        "rationale": record.rationale,
        "latency_ms": record.latency_ms,
        "snapshot": record.snapshot.to_jsonable(),
    }


class JsonlDecisionLogger:
    """Escribe una línea JSON por ciclo para auditar por qué el bot operó o se abstuvo."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: IO[str] | None = None

    def log(self, record: DecisionRecord) -> None:
        """Persiste la decisión con `decision_id` y `snapshot_hash` reproducibles."""
        if self._handle is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")
        self._handle.write(
            json.dumps(decision_payload(record), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        self._handle.flush()

    def close(self) -> None:
        """Cierra el archivo de log si está abierto."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> JsonlDecisionLogger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
