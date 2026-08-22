"""Tests für analysis/cycle_backtest.py (rein rechnerisch, keine Netzquellen -
alle Serien werden injiziert; Muster: tests/test_cycle.py)."""
import numpy as np
import pandas as pd
import pytest

from analysis import cycle, cycle_backtest


def _price_series(n=800, seed=None) -> pd.Series:
    """Deterministischer Sägezahn: mehrere Auf-/Abschwünge, damit Perzentile
    über die Historie tatsächlich variieren (reines Monoton-Wachstum wäre für
    diese Tests ungeeignet - siehe tests/test_cycle.py-Erkenntnis dazu)."""
    idx = pd.date_range("2018-01-01", periods=n, freq="D")
    t = np.arange(n)
    trend = 100.0 * (1.001 ** t)
    wave = 1 + 0.4 * np.sin(2 * np.pi * t / 250.0)
    return pd.Series(trend * wave, index=idx)


# --- _expanding_pct ---

def test_expanding_pct_matches_percentile_of_last_at_each_point():
    """Kernäquivalenz: _expanding_pct(series).iloc[k] muss exakt
    cycle._percentile_of_last(series.iloc[:k+1]) entsprechen - das ist die
    ganze Lookahead-Schutz-Garantie des Backtests."""
    s = pd.Series(np.linspace(1.0, 50.0, 60) * (1 + 0.1 * np.sin(np.arange(60))))
    expanding = cycle_backtest._expanding_pct(s, min_points=30)
    for k in [30, 40, 59]:
        manual = cycle._percentile_of_last(s.iloc[:k + 1], min_points=30)
        assert expanding.iloc[k] == pytest.approx(manual)


def test_expanding_pct_nan_before_min_points():
    s = pd.Series(np.arange(1.0, 21.0))
    result = cycle_backtest._expanding_pct(s, min_points=30)
    assert result.isna().all()  # nur 20 Punkte, min_points=30 nie erreicht


# --- walk_forward_score ---

def test_walk_forward_score_renormalizes_when_component_starts_later():
    price = _price_series(400)
    # mvrv-Historie beginnt erst 100 Tage nach der Preisreihe.
    mvrv = pd.Series(np.linspace(1.0, 100.0, 300), index=price.index[100:])

    score = cycle_backtest.walk_forward_score(price, mvrv=mvrv)
    early = score.iloc[60]   # vor Beginn der mvrv-Reihe, nach dem 30-Tage-Warmup
    late = score.iloc[-1]
    assert not pd.isna(early)   # trotz fehlendem mvrv verfügbar (Preis-Bausteine reichen)
    assert not pd.isna(late)


def test_walk_forward_score_all_nan_without_enough_price_history():
    price = _price_series(10)  # unter min_points
    score = cycle_backtest.walk_forward_score(price)
    assert score.isna().all()


# --- simulate_sell_ladder ---

def test_simulate_sell_ladder_no_trades_below_threshold():
    idx = pd.date_range("2020-01-01", periods=10, freq="D")
    price = pd.Series([100.0] * 10, index=idx)
    score = pd.Series([50.0] * 10, index=idx)

    sim = cycle_backtest.simulate_sell_ladder(price, score, sell_thresholds=[65, 75, 85])
    assert sim["trades"] == []
    assert sim["final_btc_units"] == 1.0
    assert sim["final_cash_eur"] == 0.0
    assert sim["final_value_eur"] == 100.0


def test_simulate_sell_ladder_executes_tiers_in_order_with_cost():
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    price = pd.Series([100.0, 100.0, 100.0, 100.0, 100.0], index=idx)
    score = pd.Series([50.0, 70.0, 70.0, 80.0, 90.0], index=idx)  # Stufe1 @ Tag2, Stufe2 @ Tag4, Stufe3 @ Tag5

    sim = cycle_backtest.simulate_sell_ladder(
        price, score, sell_thresholds=[65, 75, 85], fractions=(33.0, 66.0, 100.0),
        trade_cost_pct=1.0,
    )
    assert len(sim["trades"]) == 3
    assert [tr["tier"] for tr in sim["trades"]] == [1, 2, 3]

    t1 = sim["trades"][0]
    assert t1["units"] == pytest.approx(0.33)
    assert t1["proceeds_eur"] == pytest.approx(0.33 * 100.0 * 0.99)  # 1% Kosten

    t2 = sim["trades"][1]
    assert t2["units"] == pytest.approx(0.33, abs=1e-6)  # 0.66-0.33

    t3 = sim["trades"][2]
    assert t3["units"] == pytest.approx(0.34, abs=1e-6)  # 1.00-0.66

    assert sim["final_btc_units"] == pytest.approx(0.0, abs=1e-6)
    assert sim["sell_tier_reached"] == 3


def test_simulate_sell_ladder_ratchet_never_regresses():
    """Kernschutz: fällt der Score nach Erreichen von Stufe 2 wieder unter
    Stufe 1, darf NICHT nochmal (oder rückgängig) verkauft werden."""
    idx = pd.date_range("2020-01-01", periods=4, freq="D")
    price = pd.Series([100.0] * 4, index=idx)
    score = pd.Series([80.0, 80.0, 40.0, 40.0], index=idx)  # Stufe2 sofort, dann Absturz

    sim = cycle_backtest.simulate_sell_ladder(price, score, sell_thresholds=[65, 75, 85])
    assert len(sim["trades"]) == 1
    assert sim["trades"][0]["tier"] == 2
    assert sim["sell_tier_reached"] == 2


# --- _trades_vs_future_peak ---

def test_trades_vs_future_peak_computes_miss():
    idx = pd.date_range("2020-01-01", periods=10, freq="D")
    price = pd.Series([100, 110, 120, 150, 140, 130, 120, 110, 100, 90], index=idx, dtype=float)
    trades = [{"date": "2020-01-02", "price_eur": 110.0, "tier": 1}]  # Tag danach steigt auf 150

    gaps = cycle_backtest._trades_vs_future_peak(trades, price, lookforward_days=30)
    assert gaps["avg_miss_pct"] == pytest.approx((150 / 110 - 1) * 100, rel=0.01)


def test_trades_vs_future_peak_empty_without_trades():
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    price = pd.Series([100.0] * 5, index=idx)
    gaps = cycle_backtest._trades_vs_future_peak([], price, lookforward_days=30)
    assert gaps == {"avg_miss_pct": None, "worst_miss_pct": None, "per_trade": []}


# --- run_backtest (Integration, alle Serien injiziert) ---

def test_run_backtest_unavailable_with_short_history():
    result = cycle_backtest.run_backtest(price=_price_series(10))
    assert result["available"] is False
    assert "reason" in result


def test_run_backtest_full_pipeline(tmp_db):
    price = _price_series(1200)
    result = cycle_backtest.run_backtest(price=price, sell_thresholds=[65, 75, 85])

    assert result["available"] is True
    assert result["ladder_value_eur"] is not None
    assert result["buy_hold_value_eur"] == pytest.approx(float(price.iloc[-1]))
    assert result["perfect_top_value_eur"] == pytest.approx(float(price.max()))
    assert result["perfect_top_value_eur"] >= result["buy_hold_value_eur"]
    assert result["start_date"] == price.index[0].strftime("%Y-%m-%d")
    assert result["end_date"] == price.index[-1].strftime("%Y-%m-%d")
    assert 0 <= result["sell_tier_reached"] <= 3


def test_run_backtest_uses_saved_ladder_config_by_default(tmp_db):
    from core import profile
    profile.save_ladder_config("crypto", [10.0, 20.0, 30.0], [5.0, 3.0, 1.0])
    price = _price_series(1200)

    result = cycle_backtest.run_backtest(price=price)
    assert result["sell_thresholds"] == [10.0, 20.0, 30.0]
    # so niedrige Schwellen müssen bei einer schwankenden Serie auslösen
    assert result["trade_count"] > 0


# --- Regressionstest: Burn-in gegen Stichproben-Rauschen direkt nach dem Start ---

def test_run_backtest_burn_in_prevents_early_spurious_trigger():
    """Beobachtet an echten Daten: eine BTC-Serie, die in den ersten Wochen nach
    _MIN_POINTS (30 Tage) zufällig ein paar Aufwärtstage hat, kann technisch
    ein 100.Perzentil erreichen (Stichprobe viel zu klein für eine belastbare
    Aussage) und ohne Burn-in sofort auf Verkaufsstufe 3 springen - das hat
    live einen 10-Jahres-Backtest auf einen Endwert von 554 EUR ruiniert
    (BTC-Kurs beim Fehlauslöser im Okt. 2016). burn_in_days muss das verhindern."""
    idx = pd.date_range("2016-08-22", periods=1500, freq="D")
    t = np.arange(1500)
    # Erste 50 Tage: steiler, kurzer Anstieg (erzeugt in der winzigen Stichprobe
    # direkt nach dem Warmup ein Schein-Perzentil nahe 100) - danach ein
    # gemächlicher, realistischerer Trend über den Rest der Historie.
    spike = 100.0 * (1.02 ** np.arange(50))
    rest = spike[-1] * (1.0008 ** np.arange(1, 1451))
    price = pd.Series(np.concatenate([spike, rest]), index=idx)

    result = cycle_backtest.run_backtest(price=price, sell_thresholds=[65.0, 75.0, 85.0])
    assert result["available"] is True
    # Ohne Burn-in würde hier binnen weniger Wochen (weit vor Tag 365) verkauft.
    for tr in result["trades"]:
        trade_date = pd.Timestamp(tr["date"])
        assert (trade_date - idx[0]).days >= 365


def test_run_backtest_burn_in_disabled_reproduces_the_bug():
    """Gegenprobe: mit burn_in_days=0 (altes Verhalten) löst genau dasselbe
    Fixture sehr früh aus - belegt, dass der Burn-in die eigentliche Ursache
    behebt und nicht zufällig etwas anderes."""
    idx = pd.date_range("2016-08-22", periods=1500, freq="D")
    spike = 100.0 * (1.02 ** np.arange(50))
    rest = spike[-1] * (1.0008 ** np.arange(1, 1451))
    price = pd.Series(np.concatenate([spike, rest]), index=idx)

    result = cycle_backtest.run_backtest(price=price, sell_thresholds=[65.0, 75.0, 85.0], burn_in_days=0)
    assert result["trade_count"] > 0
    first_trade_date = pd.Timestamp(result["trades"][0]["date"])
    assert (first_trade_date - idx[0]).days < 100
