"""Validación del YAML y de los candados de seguridad de la configuración."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from goldbot.config import AppConfig, Mt5Credentials, compute_config_hash, load_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"


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


def test_los_umbrales_del_kill_switch_vienen_del_yaml() -> None:
    """Ningún umbral del kill switch puede quedar hardcodeado en el código."""
    config = load_config(CONFIG_PATH)
    assert config.safety.missing_grace_s == 600.0
    assert config.safety.min_free_margin_pct == 30.0
    assert config.safety.max_consecutive_execution_errors == 3


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
