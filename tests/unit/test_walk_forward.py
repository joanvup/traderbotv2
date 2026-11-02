"""Fase 6: geometría de pliegues, agregación entre pliegues y su informe.

Estos tests no juzgan la rentabilidad —eso lo dice la corrida real—, juzgan la arquitectura:
que ningún tramo de prueba pise a otro, que el corte de entrenamiento caiga justo donde
empieza la prueba, que un pliegue rechazado se siga informando y que el texto no llame ventaja
a una muestra que no la sostiene.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from _snapshots import NOW
from _synthetic import make_candles

from goldbot.backtest.metrics import (
    BacktestResult,
    compute_metrics,
    regime_playbook_grid,
    summarize,
    t_statistic,
)
from goldbot.backtest.report import WalkForwardReportBuilder, walk_forward_to_jsonable
from goldbot.backtest.walk_forward import (
    FoldWindow,
    WalkForwardReport,
    WalkForwardRunner,
)
from goldbot.config import AppConfig
from goldbot.domain.types import (
    Candle,
    ExitReason,
    ExitReasonSource,
    Regime,
    Side,
    Timeframe,
    TradeRecord,
)

TRAIN_DAYS, TEST_DAYS, STEP_DAYS = 40, 10, 10


def _config(min_cell_trades: int = 4) -> AppConfig:
    return AppConfig.model_validate(
        {
            "data": {"decision_timeframe": "D1"},
            "backtest": {
                "train_window_days": TRAIN_DAYS,
                "test_window_days": TEST_DAYS,
                "step_days": STEP_DAYS,
                "initial_balance": 1_000.0,
                "min_cell_trades": min_cell_trades,
            }
        }
    )


def _runner() -> WalkForwardRunner:
    return WalkForwardRunner(
        train_window_days=TRAIN_DAYS,
        test_window_days=TEST_DAYS,
        step_days=STEP_DAYS,
    )


def _daily(days: int) -> tuple[Candle, ...]:
    """Una vela por día: así los días del plan son velas contables, sin trampa de calendario."""
    return make_candles(Timeframe.D1, days, end_utc=NOW)


def _windows_for(count: int) -> int:
    """Días de histórico necesarios para que el plan dé exactamente `count` pliegues."""
    return TRAIN_DAYS + TEST_DAYS + STEP_DAYS * count


def _trade(
    number: int,
    r: float,
    *,
    regime: Regime = Regime.RANGE,
    playbook: str = "range_fade",
) -> TradeRecord:
    opened = NOW + timedelta(days=number)
    profit = r * 10.0
    return TradeRecord(
        position_id=number,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        entry_price=2_400.0,
        exit_price=2_400.0 + profit,
        sl=2_390.0,
        tp=2_410.0,
        open_time_utc=opened,
        close_time_utc=opened + timedelta(hours=1),
        entry_deal_id=None,
        close_deal_id=None,
        commission=0.7,
        swap=0.0,
        net_profit=profit,
        r_multiple=r,
        duration_s=3_600,
        exit_reason=ExitReason.TP if profit > 0 else ExitReason.SL,
        exit_reason_source=ExitReasonSource.TP if profit > 0 else ExitReasonSource.SL,
        playbook_id=playbook,
        regime_at_entry=regime,
        decision_id_entry=None,
    )


def _result(
    trades: tuple[TradeRecord, ...],
    *,
    start: datetime,
    end: datetime,
    decision_days: int = 10,
) -> BacktestResult:
    return BacktestResult(
        metrics=compute_metrics(trades, initial_balance=1_000.0, total_days=decision_days),
        trades=trades,
        start_utc=start,
        end_utc=end,
        model_version="rules-1+hmm-1-test",
        decision_days=decision_days,
    )


class _Calendar:
    """Sólo lo que pide `_coverage`: los dos extremos de la cobertura exportada."""

    def __init__(self, start_utc: datetime, end_utc: datetime) -> None:
        self.coverage_start_utc = start_utc
        self.coverage_end_utc = end_utc


def _report(
    trades_by_fold: tuple[tuple[TradeRecord, ...], ...] = (),
    *,
    calendar: object | None = None,
    config: AppConfig | None = None,
    folds: int | None = None,
) -> WalkForwardReport:
    """Corre el runner de verdad con un motor de mentira que reparte las operaciones dadas."""
    bars = _daily(_windows_for(folds or max(2, len(trades_by_fold))))
    queue = iter(trades_by_fold)

    def engine(window: FoldWindow) -> BacktestResult:
        return _result(next(queue, ()), start=window.test_start_utc, end=window.test_end_utc)

    return _runner().run(
        candles={Timeframe.D1: bars},
        config=config or _config(),
        calendar=calendar,
        engine_runner=engine,
    )


def _run_with(rejected_index: int, *, folds: int = 3) -> WalkForwardReport:
    """Un runner cuyo motor se niega a correr uno de los pliegues, como haría el motor real."""
    bars = _daily(_windows_for(folds))

    def engine(window: FoldWindow) -> BacktestResult:
        if window.index == rejected_index:
            raise ValueError("no hay velas de prueba después de train_end_utc")
        return _result(
            (_trade(window.index + 1, 0.8), _trade(window.index + 50, -0.2)),
            start=window.test_start_utc,
            end=window.test_end_utc,
        )

    return _runner().run(candles={Timeframe.D1: bars}, config=_config(), engine_runner=engine)


# ---------------------------------------------------------------------------
# Geometría de los pliegues
# ---------------------------------------------------------------------------


def test_plan_ancla_el_primer_pliegue_en_la_primera_vela() -> None:
    bars = _daily(100)
    windows, discarded = _runner().plan(bars)

    assert windows
    assert windows[0].train_start_utc == bars[0].open_time_utc
    assert discarded == (bars[-1].open_time_utc.date() - windows[-1].test_end_utc.date()).days
    assert all(window.train_bars > 0 and window.test_bars > 0 for window in windows)


def test_plan_corta_el_entrenamiento_donde_empieza_la_prueba() -> None:
    """Sin look-ahead: el modelo de cada pliegue muere justo donde arranca su tramo de test."""
    windows, _ = _runner().plan(_daily(100))

    for window in windows:
        assert window.test_start_utc == window.train_end_utc
        assert window.train_end_utc == window.train_start_utc + timedelta(days=TRAIN_DAYS)
        assert window.test_end_utc == window.test_start_utc + timedelta(days=TEST_DAYS)


def test_plan_desplaza_y_no_solapa_los_tramos_de_prueba() -> None:
    windows, _ = _runner().plan(_daily(100))

    for previous, following in zip(windows, windows[1:], strict=False):
        assert following.test_start_utc >= previous.test_end_utc
        assert following.train_start_utc == previous.train_start_utc + timedelta(days=STEP_DAYS)
        assert following.index == previous.index + 1


def test_plan_solo_conserva_tramos_de_prueba_completos() -> None:
    bars = _daily(100)
    windows, discarded = _runner().plan(bars)

    assert windows[-1].test_end_utc <= bars[-1].open_time_utc
    assert discarded < TRAIN_DAYS + TEST_DAYS


def test_plan_devuelve_vacio_si_el_historico_no_alcanza_un_pliegue() -> None:
    windows, margen = _runner().plan(_daily(30))

    assert windows == []
    assert margen < 0


def test_run_aborta_con_el_historico_corto_en_vez_de_silenciarlo() -> None:
    with pytest.raises(ValueError, match="no alcanza un pliegue"):
        _runner().run(candles={Timeframe.D1: _daily(20)}, config=_config())


def test_plan_rechaza_una_caché_vacía() -> None:
    with pytest.raises(ValueError, match="no hay velas"):
        _runner().plan([])


@pytest.mark.parametrize(
    ("train_days", "test_days", "step_days"),
    [(TRAIN_DAYS, TRAIN_DAYS + TEST_DAYS, STEP_DAYS), (TRAIN_DAYS, TEST_DAYS, 0), (0, 0, 1)],
)
def test_geometrias_imposibles_se_rechazan_antes_de_correr(
    train_days: int, test_days: int, step_days: int
) -> None:
    with pytest.raises(ValueError):
        WalkForwardRunner(
            train_window_days=train_days,
            test_window_days=test_days,
            step_days=step_days,
        )


def test_el_plan_devuelve_el_mismo_numero_de_pliegues_que_se_pido() -> None:
    for expected in (2, 3, 5):
        windows, _ = _runner().plan(_daily(_windows_for(expected)))
        assert len(windows) == expected


# ---------------------------------------------------------------------------
# Bucle de ejecución y agregación
# ---------------------------------------------------------------------------


def test_run_agrupa_operaciones_de_todos_los_pliegues() -> None:
    report = _report(
        ((_trade(1, 1.0), _trade(2, -0.5)), (_trade(3, 0.8), _trade(4, -1.2))),
        calendar=_Calendar(NOW - timedelta(days=365), NOW + timedelta(days=1)),
    )

    assert len(report.folds) == len(report.ran_folds) == 2
    assert report.aggregate.trades == 4
    assert report.fold_trades == (2, 2)
    assert report.min_fold_trades == report.max_fold_trades == 2
    assert report.tested_days > 0 and report.uncovered_days == 0
    assert report.news_filter_active is True
    assert report.calendar_coverage_pct == pytest.approx(100.0)
    assert report.train_window_days == TRAIN_DAYS and report.step_days == STEP_DAYS
    assert set(report.by_playbook) == {"range_fade"}
    assert list(report.grid) == [(Regime.RANGE, "range_fade")]
    assert report.model_versions == ("rules-1+hmm-1-test",)


def test_los_dias_sin_operar_ponderan_cada_pliegue_por_sus_dias_de_decision() -> None:
    report = _report(((), (), (_trade(1, 1.0), _trade(2, -0.5))))

    # Pliegues 1 y 2: diez días de decisión sin abrir nada. Pliegue 3: ocho de diez.
    assert report.no_trade_days_pct == pytest.approx((100.0 + 100.0 + 80.0) / 3)


def test_run_informa_el_pliegue_que_el_motor_rechaza_sin_borrarlo() -> None:
    report = _run_with(1)

    assert len(report.folds) == 3
    assert len(report.failed_folds) == 1
    assert len(report.ran_folds) == 2
    assert report.failed_folds[0].error == "no hay velas de prueba después de train_end_utc"
    assert report.failed_folds[0].result is None
    # Sólo se agrupan los pliegues que corrieron: el rechazado no añade ni resta operaciones.
    assert report.aggregate.trades == 4
    assert report.fold_trades == (2, 2)
    # Sin calendario no hay filtro de noticias: se declara, no se finge cobertura.
    assert report.news_filter_active is False
    assert report.calendar_coverage_pct == 0.0


def test_run_mide_la_cobertura_del_calendario_por_pliegue() -> None:
    bars = _daily(_windows_for(3))
    windows, _ = _runner().plan(bars)
    # Un calendario que sólo cubre el tramo de prueba del primer pliegue.
    calendar = _Calendar(windows[0].test_start_utc, windows[0].test_end_utc)

    report = _report(calendar=calendar, folds=3)

    coverages = [fold.calendar_coverage_pct for fold in report.folds]
    assert coverages[0] == pytest.approx(100.0)
    assert all(coverage == 0.0 for coverage in coverages[1:])
    assert 0.0 < report.calendar_coverage_pct < 100.0


# ---------------------------------------------------------------------------
# Métricas por grupo
# ---------------------------------------------------------------------------


def test_summarize_agrega_sin_esconder_la_dispersion() -> None:
    trades = tuple(_trade(index, r) for index, r in enumerate([1.0, -0.5, 2.0, -1.0], start=1))
    stats = summarize(trades, key="pooled", initial_balance=1_000.0)

    assert (stats.trades, stats.wins, stats.losses) == (4, 2, 2)
    assert stats.win_rate_pct == pytest.approx(50.0)
    assert stats.expectancy_r == pytest.approx(0.375)
    assert stats.std_r == pytest.approx(1.3769, abs=1e-3)
    assert stats.profit_factor == pytest.approx(30.0 / 15.0)
    assert stats.max_consecutive_losses == 1
    assert stats.with_r == 4


def test_t_statistic_no_afirma_nada_con_dos_numeros() -> None:
    assert t_statistic(summarize((_trade(1, 2.0),), key="una")) == 0.0
    sin_dispersion = summarize(
        tuple(_trade(index, 0.5) for index in (1, 2, 3)), key="constante"
    )
    assert t_statistic(sin_dispersion) == 0.0

    con_dispersion = summarize(
        tuple(_trade(index, r) for index, r in enumerate([2.0, -1.0, 1.5, -0.5], start=1))
    )
    # 0.5 / (1.472 / sqrt(4)): con cuatro números la pendiente no es ni de lejos significativa.
    assert t_statistic(con_dispersion) == pytest.approx(0.679, abs=1e-3)


def test_regime_playbook_grid_usa_la_etiqueta_de_entrada() -> None:
    trades = (
        _trade(1, 1.0, regime=Regime.RANGE),
        _trade(2, -1.0, regime=Regime.RANGE),
        _trade(3, 2.0, regime=Regime.TREND_UP, playbook="breakout"),
        _trade(4, 0.5, regime=Regime.UNKNOWN, playbook="no_trade"),
    )
    grid = regime_playbook_grid(trades)

    assert grid[(Regime.RANGE, "range_fade")].trades == 2
    assert grid[(Regime.TREND_UP, "breakout")].expectancy_r == pytest.approx(2.0)
    # Cuatro operaciones, tres celdas: RANGE acumula las dos suyas en la misma fila.
    assert len(grid) == 3
    # Ordenado de más a menos operaciones: la celda relevante queda arriba.
    counts = [stats.trades for stats in grid.values()]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# El informe de texto y el JSON
# ---------------------------------------------------------------------------


def test_informe_de_texto_no_llama_ventaja_a_una_muestra_corta() -> None:
    report = _report(((_trade(1, 1.0), _trade(2, -1.0)),))
    text = WalkForwardReportBuilder().build(report)

    assert "WALK-FORWARD FASE 6" in text
    assert "no concluyente" in text
    assert "No hay ventaja demostrada" in text
    assert "desactivado" in text
    assert "Pliegues rentables" in text
    assert report.aggregate.trades == 2


def test_informe_deja_claro_que_no_hubo_operaciones() -> None:
    report = _report()
    text = WalkForwardReportBuilder().build(report)

    assert "Ninguna operación en todo el walk-forward" in text
    assert "no demuestra NINGUNA ventaja" in text
    assert "(sin operaciones que desglosar)" in text
    assert walk_forward_to_jsonable(report)["aggregate"]["trades"] == 0


def test_informe_advierte_de_un_calendario_que_no_cubre_los_pliegues() -> None:
    bars = _daily(_windows_for(3))
    windows, _ = _runner().plan(bars)
    calendar = _Calendar(windows[0].test_start_utc, windows[0].test_end_utc)

    text = WalkForwardReportBuilder().build(
        _report(
            ((_trade(1, 1.0), _trade(2, -0.5), _trade(3, 0.8)),),
            calendar=calendar,
        )
    )

    assert "El calendario sólo cubre" in text
    assert "no miden la estrategia" in text


def test_informe_marca_el_pliegue_rechazado_y_el_json_conserva_el_detalle() -> None:
    report = _run_with(1)
    text = WalkForwardReportBuilder().build(report)
    payload = walk_forward_to_jsonable(report)

    assert "RECHAZADO: no hay velas de prueba" in text
    assert "pliegues rechazados por el motor" in text
    assert payload["kind"] == "walk_forward"
    assert payload["windows"] == {
        "train_days": TRAIN_DAYS,
        "test_days": TEST_DAYS,
        "step_days": STEP_DAYS,
    }
    assert len(payload["folds"]) == 3
    assert payload["folds"][1]["error"] == "no hay velas de prueba después de train_end_utc"
    assert payload["folds"][1]["result"] is None
    assert payload["folds"][0]["result"]["metrics"]["trades"] == 2
    # El JSON por pliegue no arrastra la curva de equity: sería inservible en un informe.
    assert payload["folds"][0]["result"]["equity_curve"] == []
    assert payload["aggregate"]["conclusive"] is True


def test_informe_incluye_todas_las_secciones_que_pide_la_especificacion() -> None:
    report = _report(
        (
            (_trade(1, 1.5), _trade(2, -0.5), _trade(3, 0.8)),
            (_trade(4, 1.1), _trade(5, -1.0, regime=Regime.TREND_UP, playbook="breakout")),
            (_trade(6, 0.2, playbook="breakout"),),
        )
    )
    text = WalkForwardReportBuilder().build(report)

    for section in (
        "Por pliegue:",
        "Agrupado (todas las operaciones de todos los pliegues):",
        "Estabilidad entre pliegues:",
        "Desglose por régimen",
        "Desglose por playbook",
        "Cuadrícula régimen × playbook",
        "Lectura honesta:",
    ):
        assert section in text
    assert "Expectancy" in text and "Profit factor" in text and "Máx. drawdown" in text
    assert "Días sin operar" in text and "Distribución R" in text
    assert "breakout" in text and "TREND_UP" in text


def test_las_r_se_reparten_en_los_mismos_tramos_que_el_corte_unico() -> None:
    report = _report(((_trade(1, 2.5), _trade(2, -1.4)), (_trade(3, 1.5), _trade(4, -0.2))))
    esperada = {"<=-1": 1, "-1..0": 1, "1..2": 1, ">=2": 1}

    assert report.r_distribution == esperada

    datos = walk_forward_to_jsonable(report)
    assert datos["r_distribution"] == esperada
    assert datos["no_trade_days_pct"] == pytest.approx(80.0)
