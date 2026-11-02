"""Validación del YAML y de los candados de seguridad de la configuración."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from goldbot.config import AppConfig, Mt5Credentials, compute_config_hash, load_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"


def _planos(modelo: AppConfig) -> dict[str, object]:
    """El modelo aplastado a rutas `seccion.clave.subclave`, para poder comparar dos instancias.

    Los mapas vacíos (`playbooks.no_trade.params`) desaparecen de la proyección: no hay hoja que
    comparar, y desaparece igual de los dos lados, así que la resta de claves sigue siendo justa.
    """
    plano: dict[str, object] = {}

    def caminar(nodo: object, prefijo: str) -> None:
        if isinstance(nodo, dict):
            for clave, valor in nodo.items():
                caminar(valor, f"{prefijo}{clave}.")
        else:
            plano[prefijo.rstrip(".")] = nodo

    caminar(modelo.model_dump(mode="json"), "")
    return plano


def test_example_config_loads() -> None:
    config = load_config(CONFIG_PATH)
    assert config.app.magic == 20261030
    assert config.mt5.symbol == "XAUUSD"
    assert config.risk.max_trades_day == 2
    assert config.dashboard.host == "127.0.0.1"
    assert config.safety.allow_live is False
    assert config.safety.require_demo is True
    assert "no_trade" in config.playbooks


def test_config_hash_is_stable() -> None:
    config = load_config(CONFIG_PATH)
    assert compute_config_hash(config) == compute_config_hash(config)
    assert len(compute_config_hash(config)) == 64


def test_dashboard_cannot_bind_public_interface() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"dashboard": {"host": "0.0.0.0"}})


def test_max_trades_per_day_is_hard_capped() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"max_trades_day": 3}})


def test_live_mode_requires_double_lock() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"safety": {"allow_live": True}})


def test_defaults_are_demo_first() -> None:
    config = AppConfig()
    assert config.safety.require_demo is True
    assert config.safety.allow_live is False
    assert config.dashboard.host == "127.0.0.1"


def test_los_umbrales_del_kill_switch_vienen_del_yaml_y_no_del_codigo(
    tmp_path: Path,
) -> None:
    """El candado tiene que discriminar: si el YAML manda, un YAML distinto tiene que dar otro runtime.

    La versión anterior pedía al fichero de producción `600.0`, `30.0` y `3`... que son exactamente
    los defaults del modelo, así que el test pasaba también con un YAML vacío y no probaba nada. Con
    números que ningún default comparte, la única explicación posible de que salgan esos tres es que
    se leyeron del fichero.
    """
    ruta = tmp_path / "umbrales.yaml"
    ruta.write_text(
        "safety:\n"
        "  missing_grace_s: 42.0\n"
        "  min_free_margin_pct: 7.5\n"
        "  max_consecutive_execution_errors: 1\n",
        encoding="utf-8",
    )
    config = load_config(ruta)

    assert config.safety.missing_grace_s == 42.0
    assert config.safety.min_free_margin_pct == 7.5
    assert config.safety.max_consecutive_execution_errors == 1

    # Y no es casualidad: los defaults del modelo son otros tres números.
    defaults = AppConfig().safety
    assert (defaults.missing_grace_s, defaults.min_free_margin_pct) == (600.0, 30.0)
    assert defaults.max_consecutive_execution_errors == 3


def test_el_yaml_de_produccion_no_diverge_de_los_defaults_sin_declarse() -> None:
    """El fichero y el modelo no pueden contradecirse en silencio.

    `config/config.example.yaml` es el que se pasa con `--config`, o sea el de producción, y hoy
    repite 1:1 los defaults del modelo con UNA excepción medida: `sessions.entries` a 00:15-20:00
    frente al default 07:00-20:00, la apertura dominical que el operador abrió a propósito
    (#59). Si alguien mueve un umbral en un solo de los dos sitios, aquí aparece nombrado en vez
    de llegar al runtime sin que nadie lo decida.
    """
    del_yaml = _planos(load_config(CONFIG_PATH))
    del_codigo = _planos(AppConfig())

    # Sanidad: si el aplastado se rompiera, la comparación sería vacía y el test mentiría.
    assert len(del_yaml) > 100
    assert del_yaml.keys() == del_codigo.keys()

    distintas = {
        clave for clave in del_yaml if del_yaml[clave] != del_codigo[clave]
    }
    assert distintas == {"sessions.entries"}
    assert del_yaml["sessions.entries"] == ["00:15-20:00"]
    assert del_codigo["sessions.entries"] == ["07:00-20:00"]


def test_max_feed_staleness_s_ya_no_existe() -> None:
    """La clave muerta se retiró: `extra="forbid"` la rechaza en vez de ignorarla."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"safety": {"max_feed_staleness_s": 120}})


def test_mt5_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_LOGIN", "12345678")
    monkeypatch.setenv("MT5_PASSWORD", "secreto")
    monkeypatch.setenv("MT5_SERVER", "Pepperstone-Demo")
    credentials = Mt5Credentials.from_env()
    assert credentials.is_complete
    assert credentials.login == 12345678
