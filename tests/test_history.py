from datetime import date, timedelta

import pandas as pd
import pytest

from analysis import performance


def test_snapshot_series_empty(tmp_db):
    assert performance.snapshot_series("stock") is None


def test_snapshot_series_filters_asset_type(tmp_db):
    tmp_db.save_snapshot("stock", 1000.0, "2026-07-01")
    tmp_db.save_snapshot("crypto", 5000.0, "2026-07-01")
    df = performance.snapshot_series("stock")
    assert df is not None and len(df) == 1
    assert df["Wert"].iloc[0] == 1000.0   # nur stock, kein crypto


def test_snapshot_series_time_window(tmp_db):
    today = date.today()
    tmp_db.save_snapshot("stock", 900.0, (today - timedelta(days=200)).isoformat())
    tmp_db.save_snapshot("stock", 1000.0, (today - timedelta(days=20)).isoformat())
    tmp_db.save_snapshot("stock", 1100.0, today.isoformat())

    # 1 Monat (30 Tage) -> nur die letzten beiden Punkte
    df_1m = performance.snapshot_series("stock", performance.PERIODS["1M"])
    assert list(df_1m["Wert"]) == [1000.0, 1100.0]

    # 1 Jahr -> alle drei
    df_1y = performance.snapshot_series("stock", performance.PERIODS["1J"])
    assert len(df_1y) == 3


def test_snapshot_series_sorted_datetime_index(tmp_db):
    tmp_db.save_snapshot("crypto", 3.0, "2026-07-03")
    tmp_db.save_snapshot("crypto", 1.0, "2026-07-01")
    tmp_db.save_snapshot("crypto", 2.0, "2026-07-02")
    df = performance.snapshot_series("crypto")
    assert list(df["Wert"]) == [1.0, 2.0, 3.0]           # aufsteigend nach Datum
    assert str(df.index.dtype).startswith("datetime64")


def test_periods_complete():
    assert list(performance.PERIODS) == ["1M", "3M", "6M", "1J", "3J", "5J"]


def test_history_includes_cash_in_total(tmp_db):
    tmp_db.save_snapshot("stock", 1000.0, "2026-07-01")
    tmp_db.save_snapshot("crypto", 500.0, "2026-07-01")
    tmp_db.save_snapshot("cash", 200.0, "2026-07-01")
    history = performance.history_df()
    assert history is not None
    assert history["Gesamt"].iloc[0] == 1700.0


def test_performance_index_removes_external_deposit(tmp_db):
    tmp_db.save_snapshot("stock", 1000.0, "2026-07-01")
    tmp_db.save_snapshot("cash", 0.0, "2026-07-01")
    tmp_db.save_snapshot("stock", 1600.0, "2026-07-02")
    tmp_db.save_snapshot("cash", 0.0, "2026-07-02")
    tmp_db.add_cashflow(500.0, "2026-07-02", "Einzahlung")
    index = performance.performance_index()
    assert index is not None
    # Fließkomma-Toleranz: (1600-500)/1000*100 ergibt 110.00000000000001
    assert abs(index["Index"].iloc[0] - 110.0) < 1e-9


# ---- Benchmark-Serie, CAGR, Projektion (Dashboard-Chart) ----

def _fake_close(days: int, start: float = 100.0, end: float = 200.0) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(date.today()), periods=days, freq="D")
    values = pd.Series(range(days), index=idx) / (days - 1) * (end - start) + start
    return pd.DataFrame({"Close": values})


def test_benchmark_series_period_mapping(monkeypatch):
    calls = []

    def fake_get_history(symbol, period="1y"):
        calls.append(period)
        return _fake_close(50)

    from data import stocks
    monkeypatch.setattr(stocks, "get_history", fake_get_history)
    assert performance.benchmark_series("^GSPC", days=3650) is not None
    assert performance.benchmark_series("^GSPC", days=1500) is not None
    assert performance.benchmark_series("^GSPC", days=365) is not None
    assert calls == ["10y", "5y", "1y"]


def test_benchmark_series_yf_failure(monkeypatch):
    from data import stocks
    monkeypatch.setattr(stocks, "get_history", lambda *a, **k: None)
    assert performance.benchmark_series("^GSPC") is None


def test_benchmark_series_cutoff(monkeypatch):
    from data import stocks
    monkeypatch.setattr(stocks, "get_history", lambda *a, **k: _fake_close(400))
    series = performance.benchmark_series("^GSPC", days=100)
    assert series is not None
    assert series.index[0] >= pd.Timestamp(date.today() - timedelta(days=100))


def test_sp500_cagr_from_series():
    # Verdopplung über ~10 Jahre -> CAGR ~= 2^(1/10) - 1 ~= 7.18%
    idx = pd.date_range(end=pd.Timestamp(date.today()), periods=2, freq="D")
    idx = pd.DatetimeIndex([idx[-1] - pd.Timedelta(days=3652), idx[-1]])
    close = pd.Series([100.0, 200.0], index=idx)
    cagr = performance.sp500_cagr(close)
    assert cagr == pytest.approx(2 ** (1 / 10) - 1, rel=1e-2)


def test_sp500_cagr_unusable_series():
    assert performance.sp500_cagr(pd.Series([100.0], index=[pd.Timestamp("2026-01-01")])) is None


def test_projection_series_compounds_monthly():
    last = pd.Timestamp("2026-07-01")
    proj = performance.projection_series(1000.0, last, 0.10, 2027)
    assert proj is not None
    assert proj.iloc[0] == 1000.0 and proj.index[0] == last     # dockt am echten Punkt an
    months = len(proj) - 1
    assert proj.iloc[-1] == pytest.approx(1000.0 * (1.10) ** (months / 12))
    assert proj.index[-1] <= pd.Timestamp("2027-12-31")


def test_projection_series_past_year_returns_none():
    assert performance.projection_series(1000.0, pd.Timestamp("2026-07-01"), 0.10, 2025) is None
    assert performance.projection_series(0.0, pd.Timestamp("2026-07-01"), 0.10, 2050) is None


def test_projection_series_horizon_years():
    """Horizont in Jahren statt Kalenderjahr - für den Horizont-Regler im Dashboard."""
    last = pd.Timestamp("2026-07-01")
    proj = performance.projection_series(1000.0, last, 0.10, horizon_years=10)
    assert proj is not None
    assert proj.iloc[0] == 1000.0 and proj.index[0] == last          # dockt am echten Punkt an
    assert proj.index[-1] <= last + pd.DateOffset(years=10)
    assert proj.index[-1] > last + pd.DateOffset(years=10) - pd.Timedelta(days=32)
    months = len(proj) - 1
    assert proj.iloc[-1] == pytest.approx(1000.0 * 1.10 ** (months / 12))


def test_projection_series_horizon_zero_or_negative_return_none():
    last = pd.Timestamp("2026-07-01")
    assert performance.projection_series(1000.0, last, 0.10, horizon_years=0) is None


def test_projection_series_monthly_contribution_zero_return():
    """Rendite 0 + Sparrate: Endwert = Startwert + Sparrate * Monate."""
    last = pd.Timestamp("2026-07-01")
    proj = performance.projection_series(1000.0, last, 0.0, horizon_years=10,
                                         monthly_contribution=100.0)
    assert proj is not None
    n_months = len(proj) - 1
    assert proj.iloc[-1] == pytest.approx(1000.0 + 100.0 * n_months)


def test_projection_series_monthly_contribution_beats_pure_deposits():
    """Positive Rendite: Endwert liegt über der reinen Summe aus Startwert + Einzahlungen."""
    last = pd.Timestamp("2026-07-01")
    proj = performance.projection_series(1000.0, last, 0.08, horizon_years=20,
                                         monthly_contribution=200.0)
    assert proj is not None
    n_months = len(proj) - 1
    eingezahlt = 1000.0 + 200.0 * n_months
    assert float(proj.iloc[-1]) > eingezahlt   # Wertzuwachs positiv


def test_projection_series_no_contribution_matches_closed_form():
    """Ohne Sparrate unverändert: iterativ == last*(1+monthly)^n (Regressionsschutz)."""
    last = pd.Timestamp("2026-07-01")
    proj = performance.projection_series(1000.0, last, 0.10, horizon_years=5)
    months = len(proj) - 1
    assert proj.iloc[-1] == pytest.approx(1000.0 * 1.10 ** (months / 12))


# ---- pct_from_anchor (Vergleich: Rebasing auf 0 % am Ankertag) ----

def _series(dates, values):
    return pd.Series(values, index=pd.to_datetime(dates), dtype=float)


def test_pct_from_anchor_at_first_point():
    s = _series(["2026-07-01", "2026-07-02", "2026-07-03"], [100.0, 110.0, 90.0])
    pct = performance.pct_from_anchor(s, s.index[0])
    assert list(pct) == pytest.approx([0.0, 10.0, -10.0])


def test_pct_from_anchor_midseries_via_asof():
    """Anker mitten in der Serie: Basis ist der Wert am/kurz vor dem Ankertag."""
    s = _series(["2026-07-01", "2026-07-10", "2026-07-20"], [100.0, 200.0, 300.0])
    # Anker zwischen dem 10. und 20. -> asof nimmt den Wert vom 10. (=200)
    pct = performance.pct_from_anchor(s, pd.Timestamp("2026-07-15"))
    assert pct.loc["2026-07-10"] == pytest.approx(0.0)
    assert pct.loc["2026-07-20"] == pytest.approx(50.0)


def test_pct_from_anchor_before_series_uses_first():
    s = _series(["2026-07-10", "2026-07-11"], [50.0, 75.0])
    # Anker vor Serienbeginn (junger Benchmark) -> auf ersten Wert rebasieren
    pct = performance.pct_from_anchor(s, pd.Timestamp("2026-07-01"))
    assert list(pct) == pytest.approx([0.0, 50.0])


def test_pct_from_anchor_empty_or_none():
    assert performance.pct_from_anchor(None, pd.Timestamp("2026-07-01")) is None
    assert performance.pct_from_anchor(pd.Series([], dtype=float), pd.Timestamp("2026-07-01")) is None
