"""Tests für das Backtest-Modul (Kursquellen gemockt)."""
from datetime import date, timedelta

import pandas as pd
import pytest

from analysis import backtest
from core import shadow


def _series(price_x, price_now, start):
    """Zwei-Punkt-Reihe: Kurs am Kaufdatum und heute."""
    idx = pd.to_datetime([start, date.today()])
    return pd.Series([price_x, price_now], index=idx)


def _ago(days):
    return date.today() - timedelta(days=days)


def test_backtest_aggregates_quantities(tmp_db, monkeypatch):
    db = tmp_db
    db.save_position("NVDA", "stock", 2.0, 100.0, category="A")
    db.save_position("NVDA", "stock", 3.0, 100.0, category="B")
    start = date.today() - timedelta(days=100)
    monkeypatch.setattr(backtest, "_series_eur",
                        lambda sym, at, days: _series(50.0, 80.0, start))
    # Kein Live-Kurs verfügbar -> Fallback auf den letzten Punkt der Reihe (80.0)
    monkeypatch.setattr(shadow, "price_eur", lambda sym, at: None)

    res = backtest.run_backtest("stock", start)
    assert len(res["rows"]) == 1
    row = res["rows"][0]
    assert row["Symbol"] == "NVDA"
    assert row["Investiert (€)"] == 5 * 50.0   # 2 + 3 aggregiert
    assert row["Wert heute (€)"] == 5 * 80.0
    assert round(row["Rendite (%)"], 1) == 60.0
    assert round(res["total_return_pct"], 1) == 60.0
    assert res["curve"] is not None


def test_build_curve_backfills_leading_gaps():
    """Startet eine Reihe (z.B. Aktien am Wochenende ohne Kurs) erst später als
    eine andere, darf die Kurve zu Beginn nicht künstlich zu tief liegen -
    bfill() muss die führende Lücke mit dem ersten echten Kurswert auffüllen."""
    start = pd.Timestamp("2026-01-01")
    idx_full = pd.date_range(start, periods=5, freq="D")
    idx_late = pd.date_range(start + pd.Timedelta(days=2), periods=3, freq="D")
    series_map = {
        "CRYPTO": (1.0, pd.Series([10.0] * 5, index=idx_full)),
        "STOCK": (1.0, pd.Series([100.0] * 3, index=idx_late)),  # erst ab Tag 3
    }
    curve = backtest._build_curve(series_map, start)
    # Ohne bfill wären die ersten beiden Tage nur 10.0 (STOCK zählt als 0).
    assert curve.iloc[0] == 110.0
    assert curve.iloc[-1] == 110.0


def test_backtest_prefers_live_price(tmp_db, monkeypatch):
    """'Wert heute' nutzt den echten Live-Kurs, nicht den letzten Reihenpunkt -
    damit stimmt der Backtest mit dem Dashboard/den echten Positionen überein."""
    db = tmp_db
    db.save_position("NVDA", "stock", 2.0, 100.0, category="A")
    start = date.today() - timedelta(days=100)
    monkeypatch.setattr(backtest, "_series_eur",
                        lambda sym, at, days: _series(50.0, 80.0, start))
    monkeypatch.setattr(shadow, "price_eur", lambda sym, at: 95.0)  # abweichender Live-Kurs

    res = backtest.run_backtest("stock", start)
    assert res["rows"][0]["Kurs heute (€)"] == 95.0
    assert res["rows"][0]["Wert heute (€)"] == 2 * 95.0


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


def test_benchmark_curve_rebased_to_invest(monkeypatch):
    """Die Benchmark-Kurve startet beim investierten Kapital (Rebase am Kaufdatum)."""
    from analysis import performance
    from views import backtest as bt_view

    start = _ago(100)
    # Benchmark: 100 -> 120 Punkte über den Zeitraum
    idx = pd.date_range(start, periods=101, freq="D")
    series = pd.Series([100.0 + 0.2 * i for i in range(101)], index=idx)
    monkeypatch.setattr(performance, "benchmark_series", lambda sym, days: series)

    curve = bt_view._benchmark_curve("^GSPC", start, invest=10000.0)
    assert curve is not None
    assert curve.iloc[0] == pytest.approx(10000.0)               # Start = Investiert
    assert curve.iloc[-1] == pytest.approx(12000.0)              # +20 % wie der Benchmark


def test_benchmark_curve_none_without_data(monkeypatch):
    from analysis import performance
    from views import backtest as bt_view
    monkeypatch.setattr(performance, "benchmark_series", lambda sym, days: None)
    assert bt_view._benchmark_curve("^GSPC", _ago(100), invest=10000.0) is None
    assert bt_view._benchmark_curve("^GSPC", _ago(100), invest=0.0) is None
