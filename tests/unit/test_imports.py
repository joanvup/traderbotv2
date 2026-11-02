"""El paquete debe importarse por completo sin cargar MetaTrader5."""

from __future__ import annotations

import importlib
import pkgutil
import sys

import goldbot


def test_adapters_do_not_import_mt5_eagerly() -> None:
    importlib.import_module("goldbot.adapters.mt5.connection")
    importlib.import_module("goldbot.adapters.mt5_adapter")
    importlib.import_module("goldbot.adapters.simulator")
    assert "MetaTrader5" not in sys.modules


def test_all_modules_import() -> None:
    modules = [m.name for m in pkgutil.walk_packages(goldbot.__path__, goldbot.__name__ + ".")]
    assert modules
    for name in modules:
        importlib.import_module(name)
