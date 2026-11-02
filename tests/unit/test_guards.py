"""Puerta de frescura de la entrada: solo el marco de decisión y la edad del tick."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from goldbot.domain.types import DataFreshness, DataStatus, Timeframe
from goldbot.engine.guards import GuardViolation, assert_feed_fresh

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _freshness(
    **status_by_tf: DataStatus,
) -> dict[Timeframe, DataFreshness]:
    return {
        timeframe: DataFreshness(
            timeframe,
            NOW,
            30.0 if status_by_tf.get(timeframe) is DataStatus.OK else 5_400.0,
            status_by_tf.get(timeframe, DataStatus.OK),
        )
        for timeframe in (
            Timeframe.M5,
            Timeframe.M15,
            Timeframe.H1,
            Timeframe.H4,
            Timeframe.D1,
        )
    }


def _assert(tf: Timeframe, freshness: dict[Timeframe, DataFreshness], tick_age_s: float) -> None:
    assert_feed_fresh(
        freshness,
        decision_timeframe=tf,
        tick_age_s=tick_age_s,
        max_tick_age_s=120.0,
    )


def test_decision_fresco_y_tick_actual_permiten_entrar() -> None:
    _assert(Timeframe.M5, _freshness(), 1.5)


def test_la_frescura_del_contexto_no_bloquea_la_entrada() -> None:
    """Es el arranque del lunes: D1/H4/H1 siguen atrasados horas y hay que poder operar."""
    freshness = _freshness(
        D1=DataStatus.MISSING,
        H4=DataStatus.STALE,
        H1=DataStatus.STALE,
    )
    _assert(Timeframe.M5, freshness, 2.0)


def test_marco_de_decision_stale_bloquea_la_entrada() -> None:
    with pytest.raises(GuardViolation, match="M5 en estado STALE"):
        _assert(Timeframe.M5, _freshness(M5=DataStatus.STALE), 2.0)


def test_marco_de_decision_sin_medir_bloquea_la_entrada() -> None:
    with pytest.raises(GuardViolation, match="sin medición de frescura"):
        assert_feed_fresh(
            {},
            decision_timeframe=Timeframe.M5,
            tick_age_s=1.0,
            max_tick_age_s=120.0,
        )


def test_tick_atrasado_bloquea_la_entrada_aunque_las_velas_esten_bien() -> None:
    with pytest.raises(GuardViolation, match="tick atrasado: 300 s"):
        _assert(Timeframe.M5, _freshness(), 300.0)


def test_tick_ausente_bloquea_la_entrada() -> None:
    with pytest.raises(GuardViolation, match="sin tick"):
        assert_feed_fresh(
            _freshness(),
            decision_timeframe=Timeframe.M5,
            tick_age_s=None,
            max_tick_age_s=120.0,
        )
