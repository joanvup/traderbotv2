"""Visualización autocontenida del régimen sobre el histórico (solo lectura).

Descarga velas cerradas del timeframe de decisión, entrena el detector configurado fuera de
muestra y pinta un SVG con bandas de régimen bajo la curva de precio, más dos pistas
comparativas (reglas y HMM). No usa CDN ni escribe en el StateStore: genera
`reports/regimes.html` y termina.
"""

from __future__ import annotations

import argparse
import html
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from goldbot.adapters.mt5.connection import Mt5Connection
from goldbot.adapters.mt5_adapter import Mt5BrokerAdapter
from goldbot.config import AppConfig, Mt5Credentials, load_config, load_environment
from goldbot.domain.regime import (
    HmmRegimeDetector,
    RulesRegimeDetector,
    create_regime_detector,
)
from goldbot.domain.regime.features import compute_features
from goldbot.domain.types import Candle, Regime, RegimeState, Timeframe
from goldbot.engine.analysis import build_regime_settings
from goldbot.engine.data_service import build_clock

LOGGER = logging.getLogger("goldbot.visualize_regimes")

_COLORS: dict[Regime, str] = {
    Regime.TREND_UP: "#1b9e77",
    Regime.TREND_DOWN: "#d95f02",
    Regime.RANGE: "#7570b3",
    Regime.HIGH_VOLATILITY: "#e6ab02",
    Regime.UNKNOWN: "#9e9e9e",
}

_WIDTH = 1240
_HEIGHT = 520
_LEFT = 70
_RIGHT = 24
_TOP = 28
_BOTTOM = 40
_MIN_ROWS = 50


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="visualize_regimes", description="Genera un HTML con el régimen histórico"
    )
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument(
        "--timeframe",
        default=None,
        choices=[timeframe.value for timeframe in Timeframe],
        help="timeframe de decisión (por defecto, el de la config)",
    )
    parser.add_argument("--limit", type=int, default=600, help="velas visibles")
    parser.add_argument("--output", default="reports/regimes.html", help="HTML de salida")
    parser.add_argument("--level", default="INFO", help="nivel de logging")
    return parser


def _build_broker(config: AppConfig, clock) -> Mt5BrokerAdapter:
    connection = Mt5Connection(
        clock,
        config.mt5.symbol,
        terminal_path=config.mt5.terminal_path,
        timeout_ms=config.mt5.timeout_ms,
        credentials=Mt5Credentials.from_env(),
    )
    return Mt5BrokerAdapter(
        connection,
        clock,
        deviation_points=config.execution.deviation_points,
        symbol_override=config.mt5.symbol_override,
    )


def _fetched_count(config: AppConfig, limit: int) -> int:
    settings = build_regime_settings(config)
    return settings.train_window_bars + settings.atr_percentile_window + limit + 20


def _train(
    config: AppConfig,
    candles: tuple[Candle, ...],
    *,
    limit: int,
):
    settings = build_regime_settings(config)
    features = compute_features(candles, settings)
    display = candles[-min(limit, len(candles)) :]
    detector = create_regime_detector(settings)

    boundary = len(candles) - len(display) - 1
    train_end = candles[boundary].close_time_utc if boundary >= 0 else candles[-1].close_time_utc
    usable = features.dropna()
    if len(usable[usable.index <= pd.Timestamp(train_end)]) < _MIN_ROWS:
        LOGGER.warning("histórico insuficiente para entrenar fuera de muestra; se usa todo")
        train_end = candles[-1].close_time_utc

    info = detector.fit(features, train_end_utc=train_end)
    tracks: dict[str, dict[datetime, RegimeState]] = {
        "both": _track(detector, features)
    }
    for key, alternative in (
        ("reglas", RulesRegimeDetector(settings)),
        ("HMM", HmmRegimeDetector(settings)),
    ):
        try:
            alternative.fit(features, train_end_utc=train_end)
            tracks[key] = _track(alternative, features)
        except Exception as exc:  # hmmlearn ausente o ajuste degenerado
            LOGGER.warning("pista %s no disponible: %s", key, exc)
    return display, tracks, info, detector


def _track(detector, features: pd.DataFrame) -> dict[datetime, RegimeState]:
    return {state.decided_at_utc: state for state in detector.predict_batch(features)}


def _render(
    display: tuple[Candle, ...],
    tracks: dict[str, dict[datetime, RegimeState]],
    *,
    symbol: str,
    timeframe: Timeframe,
    info,
) -> str:
    states = tracks.get("both", {})
    closes = [candle.close for candle in display]
    low = min(candle.low for candle in display)
    high = max(candle.high for candle in display)
    span = max(high - low, 1e-9)
    low -= span * 0.05
    high += span * 0.05
    plot_width = _WIDTH - _LEFT - _RIGHT
    plot_height = _HEIGHT - _TOP - _BOTTOM
    step = plot_width / max(1, len(display) - 1)

    def x(index: int) -> float:
        return _LEFT + index * step

    def y(price: float) -> float:
        return _TOP + (high - price) / (high - low) * plot_height

    bands: list[str] = []
    start = 0
    for index in range(1, len(display) + 1):
        if index == len(display) or _label(states, display, index) != _label(
            states, display, start
        ):
            label = _label(states, display, start)
            color = _COLORS[Regime(label)]
            x0 = x(start) - step / 2
            x1 = x(index - 1) + step / 2
            bands.append(
                f'<rect x="{x0:.1f}" y="{_TOP}" width="{max(1.0, x1 - x0):.1f}" '
                f'height="{plot_height}" fill="{color}" opacity="0.22">'
                f"<title>{html.escape(label)}</title></rect>"
            )
            start = index

    line = " ".join(
        f"{x(index):.1f},{y(price):.1f}" for index, price in enumerate(closes)
    )
    counts = _counts(display, states)
    total = max(1, len(display))
    legend = " ".join(
        f'<span class="chip"><i style="background:{_COLORS[regime]}"></i>'
        f"{regime.value} · {counts[regime]} ({counts[regime] / total:.0%})</span>"
        for regime in Regime
    )

    def mini(title: str, track: dict[datetime, RegimeState]) -> str:
        rects: list[str] = []
        start = 0
        for index in range(1, len(display) + 1):
            if index == len(display) or _label(track, display, index) != _label(
                track, display, start
            ):
                label = _label(track, display, start)
                x0 = x(start) - step / 2
                x1 = x(index - 1) + step / 2
                rects.append(
                    f'<rect x="{x0:.1f}" y="4" width="{max(1.0, x1 - x0):.1f}" '
                    f'height="22" fill="{_COLORS[Regime(label)]}" opacity="0.85">'
                    f"<title>{html.escape(label)}</title></rect>"
                )
                start = index
        track_counts = _counts(display, track)
        summary = " · ".join(
            f"{regime.value} {track_counts[regime]}"
            for regime in Regime
            if track_counts[regime]
        )
        return f"""<div class="track">
    <div class="track-head"><span>{html.escape(title)}</span>
      <span class="track-counts">{html.escape(summary)}</span></div>
    <svg viewBox="0 0 {_WIDTH} 30" role="img" aria-label="pista {html.escape(title)}">
      {"".join(rects)}
    </svg>
  </div>"""

    comparison = "".join(
        mini(title, tracks[key])
        for key, title, method in (
            ("reglas", "Reglas", "rules"),
            ("HMM", "HMM", "hmm"),
        )
        if key in tracks and method != info.name
    )

    rows = []
    for candle in display[-12:]:
        state = states.get(candle.open_time_utc)
        label = state.label.value if state is not None else Regime.UNKNOWN.value
        confidence = f"{state.confidence:.2f}" if state is not None else "—"
        rows.append(
            f"<tr><td>{candle.open_time_utc:%Y-%m-%d %H:%M}</td>"
            f"<td>{candle.close:.2f}</td>"
            f'<td><span class="dot" style="background:{_COLORS[Regime(label)]}"></span>'
            f"{label}</td><td>{confidence}</td></tr>"
        )

    grid = []
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        price = low + (high - low) * fraction
        grid.append(
            f'<line x1="{_LEFT}" y1="{y(price):.1f}" x2="{_LEFT + plot_width}" '
            f'y2="{y(price):.1f}" stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{_LEFT - 8}" y="{y(price) + 4:.1f}" text-anchor="end" '
            f'class="axis">{price:.1f}</text>'
        )

    start_label = display[0].open_time_utc.strftime("%Y-%m-%d %H:%M")
    end_label = display[-1].open_time_utc.strftime("%Y-%m-%d %H:%M")
    fit_note = (
        f"{info.name} {info.version} · params {info.params_hash} · "
        f"entrenado hasta {info.trained_until_utc:%Y-%m-%d %H:%M} UTC"
    )

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>goldbot · regímenes {html.escape(symbol)} {timeframe.value}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 24px;
         background: #fafafa; color: #111827; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #4b5563; font-size: 13px; margin-bottom: 16px; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; }}
  .chip {{ display: inline-flex; align-items: center; gap: 6px; margin-right: 14px;
           font-size: 13px; color: #374151; }}
  .chip i, .dot {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
  svg {{ display: block; width: 100%; height: auto; }}
  .axis {{ font-size: 11px; fill: #6b7280; }}
  .track {{ margin-top: 12px; }}
  .track-head {{ display: flex; justify-content: space-between; gap: 12px;
                 font-size: 12px; color: #4b5563; margin-bottom: 2px; }}
  .track-counts {{ color: #6b7280; text-align: right; }}
  table {{ border-collapse: collapse; margin-top: 16px; font-size: 13px; width: 100%; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #f3f4f6; }}
  th {{ color: #6b7280; font-weight: 600; }}
  .note {{ color: #6b7280; font-size: 12px; margin-top: 10px; }}
</style>
</head>
<body>
<h1>goldbot · régimen de mercado</h1>
<div class="meta">{html.escape(symbol)} · {timeframe.value} · {start_label} → {end_label} UTC ·
{len(display)} velas cerradas</div>
<div class="card">
  <div>{legend}</div>
  <svg viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="precio y regímenes">
    {"".join(grid)}
    {"".join(bands)}
    <polyline points="{line}" fill="none" stroke="#111827" stroke-width="1.3"
              stroke-linejoin="round" stroke-linecap="round"/>
  </svg>
  {comparison}
  <div class="note">{html.escape(fit_note)}. La banda principal usa el detector configurado
  ({html.escape(info.name)}) con histéresis; con <code>both</code> solo se confirma un régimen
  cuando reglas y HMM coinciden, así que los desacuerdos aparecen como UNKNOWN. Las pistas
  inferiores muestran cada método por separado para auditar esa diferencia. Oportunidad solo
  sobre velas cerradas; UNKNOWN = confianza por debajo del umbral.</div>
</div>
<table>
  <thead><tr><th>Vela (UTC)</th><th>Cierre</th><th>Régimen</th><th>Confianza</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</body>
</html>
"""


def _label(states: dict[datetime, RegimeState], display: tuple[Candle, ...], index: int) -> str:
    state = states.get(display[index].open_time_utc)
    return state.label.value if state is not None else Regime.UNKNOWN.value


def _counts(
    display: tuple[Candle, ...], states: dict[datetime, RegimeState]
) -> dict[Regime, int]:
    counts = {regime: 0 for regime in Regime}
    for candle in display:
        state = states.get(candle.open_time_utc)
        counts[state.label if state is not None else Regime.UNKNOWN] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)
    logging.basicConfig(level=args.level, format="%(levelname)s %(message)s")

    timeframe = Timeframe(args.timeframe or config.data.decision_timeframe)
    clock = build_clock(config)
    broker = _build_broker(config, clock)
    try:
        health = broker.connect()
        if not health.connected:
            raise RuntimeError(f"MT5 no disponible: {health.last_error}")
        count = _fetched_count(config, args.limit)
        candles = tuple(
            broker.get_candles(
                config.mt5.symbol, timeframe, count, closed_only=True, as_of_utc=clock.now_utc()
            )
        )
        if len(candles) < 60:
            raise RuntimeError(f"histórico insuficiente: {len(candles)} velas cerradas")
        display, tracks, info, detector = _train(config, candles, limit=args.limit)
        report = _render(
            display,
            tracks,
            symbol=broker.resolved_symbol or config.mt5.symbol,
            timeframe=timeframe,
            info=info,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(
            f"{len(display)} velas · {info.name} {info.version} · "
            f"modelos HMM activos: {getattr(detector, 'hmm_active', 'n/a')}"
        )
        for key, label in (("both", "configurado"), ("reglas", "reglas"), ("HMM", "hmm")):
            if key not in tracks:
                continue
            counts = {
                regime.value: count
                for regime, count in _counts(display, tracks[key]).items()
                if count
            }
            print(f"  {label:>11}: {counts}")
        print(f"Informe escrito en {output.resolve()}")
    except KeyboardInterrupt:
        LOGGER.info("interrumpido por el operador")
    except Exception:
        LOGGER.exception("la visualización falló")
        return 2
    finally:
        broker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
