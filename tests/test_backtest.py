"""Tests für das Backtest-Modul (Kursquellen gemockt)."""
from datetime import date, timedelta

import pandas as pd

from analysis import backtest


def _series(price_x, price_now, start):
    """Zwei-Punkt-Reihe: Kurs am Kaufdatum und heute."""
    idx = pd.to_datetime([start, date.today()])
    return pd.Series([price_x, price_now], index=idx)


def test_backtest_aggregates_quantities(tmp_db, monkeypatch):
    db = tmp_db
    db.save_position("NVDA", "stock", 2.0, 100.0, category="A")
    db.save_position("NVDA", "stock", 3.0, 100.0, category="B")
    start = date.today() - timedelta(days=100)
    monkeypatch.setattr(backtest, "_series_eur",
                        lambda sym, at, days: _series(50.0, 80.0, start))

    res = backtest.run_backtest("stock", start)
    assert len(res["rows"]) == 1
    row = res["rows"][0]
    assert row["Symbol"] == "NVDA"
    assert row["Investiert (€)"] == 5 * 50.0   # 2 + 3 aggregiert
    assert row["Wert heute (€)"] == 5 * 80.0
    assert round(row["Rendite (%)"], 1) == 60.0
    assert round(res["total_return_pct"], 1) == 60.0
    assert res["curve"] is not None


def test_backtest_skips_without_price(tmp_db, monkeypatch):
    db = tmp_db
    db.save_position("XYZ", "crypto", 1.0, 0.0, category="K")
    start = date.today() - timedelta(days=100)
    monkeypatch.setattr(backtest, "_series_eur", lambda *a, **k: None)

    res = backtest.run_backtest("crypto", start)
    assert res["rows"] == []
    assert "XYZ" in res["skipped"]
    assert res["total_return_pct"] is None


def test_backtest_skips_eur_cash(tmp_db, monkeypatch):
    db = tmp_db
    db.save_position("EUR", "crypto", 100.0, 1.0, category="Kraken")
    db.save_position("BTC", "crypto", 1.0, 0.0, category="Kraken")
    start = date.today() - timedelta(days=100)
    monkeypatch.setattr(backtest, "_series_eur",
                        lambda *a, **k: _series(20000.0, 30000.0, start))

    res = backtest.run_backtest("crypto", start)
    assert {r["Symbol"] for r in res["rows"]} == {"BTC"}
