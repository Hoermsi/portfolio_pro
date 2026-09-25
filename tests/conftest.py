import sys
from pathlib import Path

import pytest

# portfolio_pro auf den Pfad legen, damit die Pakete importierbar sind
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Leere Test-DB + kein Legacy-JSON."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LEGACY_PORTFOLIO_JSON", tmp_path / "missing.json")
    from core import db
    db.init_db()
    return db


@pytest.fixture(autouse=True)
def _no_hyperliquid_network(monkeypatch):
    """Sicherheitsnetz: kein Test erreicht je die Hyperliquid-API.

    core.bot.run_deterministic_cycle() ruft seit der Signal-Engine in jedem
    Takt Kerzen und die Marktliste ab. Ohne diese Sperre wuerden Tests, die
    eigentlich nur den Risikoteil pruefen, echte Netzaufrufe machen - langsam,
    flatterhaft und gegen die Projektregel "externe APIs immer gemockt".
    Wer Signale testen WILL, injiziert sein eigenes `candles_fn` (siehe
    tests/test_bot_signals.py) oder patcht diese Funktionen erneut.
    """
    from data import hyperliquid

    monkeypatch.setattr(hyperliquid, "candles_df", lambda *a, **k: None)
    # Niedrige Ebene patchen, nicht supported_symbols() selbst: Dateien wie
    # tests/test_hyperliquid.py mocken _mids()/_meta_and_ctxs() bereits korrekt
    # ueber ihre eigene autouse-Fixture. Deren Fixture wird NACH dieser hier
    # angewandt (Modul-Fixtures ueberschreiben Conftest-Fixtures desselben
    # Scopes) und gewinnt - ein direkter Patch von supported_symbols() wuerde
    # das dagegen unabhaengig von der lokalen Fixture uebersteuern.
    monkeypatch.setattr(hyperliquid, "_mids", lambda: {})
    monkeypatch.setattr(hyperliquid, "_meta_and_ctxs", lambda: [{"universe": []}, []])
