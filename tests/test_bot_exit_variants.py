"""analysis/bot_exit_variants.py: Ausstiegsmechanik-Vergleich ueber
Walk-Forward-Fenster (Analyseplan "Trading-Bot ueberdenken", Abschnitt 5,
Schritt 2).

Rein synthetische Kursreihen, kein Netz - dieselbe Konvention wie
tests/test_bot_ablation.py/tests/test_bot_walkforward.py. Der Schwerpunkt
liegt auf der MECHANIK (ein Datenabruf fuer beide Varianten, exit_variant
korrekt durchgereicht, verdict() wendet EXIT_VARIANT_CRITERIA mechanisch an)
- nicht auf einer bestimmten Rendite, die ohnehin an der erfundenen Reihe
haengt."""
import numpy as np
import pandas as pd
import pytest

from analysis import bot_exit_variants as ev
from analysis import bot_walkforward as wf


def _series(n, drift=0.0022, noise=0.0032, seed=3, start=100.0, freq="h", from_ts=None):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    op = np.concatenate([[start], close[:-1]])
    high = np.maximum(op, close) * (1 + abs(rng.normal(0, noise, n)))
    low = np.minimum(op, close) * (1 - abs(rng.normal(0, noise, n)))
    start_ts = from_ts or pd.Timestamp("2026-01-01", tz="UTC")
    idx = pd.date_range(start_ts, periods=n, freq=freq)
    return pd.DataFrame({"Open": op, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(n, 1000.0)}, index=idx)


def _daily_btc(n_days, drift=0.006, noise=0.006, seed=90, start_ts=None):
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, noise, n_days)))
    op = np.concatenate([[100.0], close[:-1]])
    idx = pd.date_range(start_ts or pd.Timestamp("2025-01-01", tz="UTC"), periods=n_days, freq="1d")
    return pd.DataFrame({"Open": op, "High": np.maximum(op, close) * 1.01,
                         "Low": np.minimum(op, close) * 0.99, "Close": close,
                         "Volume": np.full(n_days, 1000.0)}, index=idx)


def _synthetic_fetchers(window_days, num_windows, seed=1):
    """Identisch zum Vorbild in tests/test_bot_ablation.py - siehe dort fuer
    die ausfuehrliche Erklaerung."""
    total_days = window_days * num_windows + wf._BURN_IN_LOOKBACK_DAYS + 5
    n_hours = total_days * 24
    end_ts = pd.Timestamp.now(tz="UTC").floor("h")
    start_ts = end_ts - pd.Timedelta(hours=n_hours)
    btc = _series(n_hours, seed=seed, from_ts=start_ts)
    eth = _series(n_hours, seed=seed + 1, from_ts=start_ts)
    btc_daily = _daily_btc(total_days + 120, seed=seed + 2,
                           start_ts=start_ts - pd.Timedelta(days=120))
    frames = {"BTC": btc, "ETH": eth}

    def candles_fn(symbol, start_ms, end_ms, interval):
        df = frames.get(symbol)
        if df is None:
            return None
        lo = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        hi = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        sub = df[(df.index >= lo) & (df.index <= hi)]
        if interval == "1h" or sub.empty:
            return sub
        return sub.resample("4h").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last",
             "Volume": "sum"}).dropna()

    def daily_fn(symbol, start_ms, end_ms, interval):
        lo = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        hi = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        return btc_daily[(btc_daily.index >= lo) & (btc_daily.index <= hi)]

    def funding_fn(symbol, start_ms, end_ms):
        return None

    return candles_fn, daily_fn, funding_fn


# --- run_exit_variant_comparison(): Mechanik ---

def test_run_comparison_reports_both_variants():
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    result = ev.run_exit_variant_comparison(
        ["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
        starting_equity_usd=250.0, candles_fn=candles_fn,
        daily_fn=daily_fn, funding_fn=funding_fn)

    assert "error" not in result
    assert set(result["variants"]) == set(ev.VARIANTS)
    for name, exit_variant in ev.VARIANTS.items():
        assert result["variants"][name]["exit_variant"] == exit_variant


def test_run_comparison_loads_market_data_exactly_once():
    """Derselbe Sinn wie bei analysis.bot_ablation: EIN Datenabruf fuer BEIDE
    Varianten, sonst saehe unterschiedliche Datenvollstaendigkeit zwischen
    Varianten wie ein Effekt der Ausstiegsregel aus."""
    import analysis.bot_backtest as bb

    calls = {"n": 0}
    original = bb.load_market_data

    def counting_load(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    import analysis.bot_exit_variants as ev_module
    orig_ref = ev_module.bot_backtest.load_market_data
    ev_module.bot_backtest.load_market_data = counting_load
    try:
        ev_module.run_exit_variant_comparison(
            ["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
            starting_equity_usd=250.0, candles_fn=candles_fn,
            daily_fn=daily_fn, funding_fn=funding_fn)
    finally:
        ev_module.bot_backtest.load_market_data = orig_ref

    assert calls["n"] == 1


def test_run_comparison_reports_error_without_any_candles():
    result = ev.run_exit_variant_comparison(
        ["BTC"], window_days=10, num_windows=2,
        candles_fn=lambda *a, **k: None,
        daily_fn=lambda *a, **k: None,
        funding_fn=lambda *a, **k: None)
    assert "error" in result


def test_run_comparison_reports_error_when_btc_daily_missing():
    candles_fn, _, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)

    def empty_daily_fn(symbol, start_ms, end_ms, interval):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = ev.run_exit_variant_comparison(
        ["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
        candles_fn=candles_fn, daily_fn=empty_daily_fn, funding_fn=funding_fn)
    assert "error" in result
    assert "BTC-Tagesdaten" in result["error"]


def test_run_comparison_reports_error_for_num_windows_below_one():
    result = ev.run_exit_variant_comparison(["BTC"], window_days=10, num_windows=0)
    assert "error" in result


def test_run_comparison_progress_callback_receives_daten_and_variante_stages():
    events = []
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=2)
    ev.run_exit_variant_comparison(
        ["BTC", "ETH"], window_days=20, num_windows=2, risk_level=9,
        candles_fn=candles_fn, daily_fn=daily_fn, funding_fn=funding_fn,
        progress_fn=lambda *event: events.append(event))

    assert any(e[0] == "daten" for e in events)
    assert any(e[0] == "variante" for e in events)
    variant_events = [e for e in events if e[0] == "variante"]
    assert variant_events[-1][1] == len(ev.VARIANTS)


# --- _variant_metrics(): reine Pool-Weiterleitung, kein Nachbau ---

def _fake_trades(n_wins, win_pnl, win_r, n_losses, loss_pnl, loss_r):
    trades = [{"net_pnl_usd": win_pnl, "r_multiple": win_r} for _ in range(n_wins)]
    trades += [{"net_pnl_usd": -loss_pnl, "r_multiple": -loss_r} for _ in range(n_losses)]
    return trades


def _fake_window(index, trades, start_usd, costs_usd=2.0, days=20):
    end_usd = start_usd + sum(t["net_pnl_usd"] for t in trades)
    base = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=index * days)
    curve = pd.Series([start_usd, end_usd], index=[base, base + pd.Timedelta(days=days - 1)])
    return {"index": index, "start": base, "stop": base + pd.Timedelta(days=days),
           "regime": "bull", "btc_return_pct": 0.0,
           "metrics": {"start_usd": start_usd, "end_usd": end_usd, "costs_usd": costs_usd,
                      "trades": len(trades)},
           "trades": trades, "equity_curve": curve, "risk_level": 5, "limits": {}}


def test_variant_metrics_pools_across_valid_windows_only():
    windows = [
        _fake_window(0, _fake_trades(4, 5.0, 2.0, 1, 2.0, 1.0), 250.0),
        {"index": 1, "start": pd.Timestamp("2026-01-21", tz="UTC"),
         "stop": pd.Timestamp("2026-02-10", tz="UTC"), "error": "kaputt", "regime": "unbekannt"},
        _fake_window(2, _fake_trades(4, 5.0, 2.0, 1, 2.0, 1.0), 275.0),
    ]
    metrics = ev._variant_metrics({"windows": windows})

    assert metrics["valid_windows"] == 2
    assert metrics["trades"] == 10
    assert metrics["trades_per_day"] == pytest.approx(10 / 40)
    assert metrics["avg_r"] is not None


def test_variant_metrics_passes_through_the_error():
    metrics = ev._variant_metrics({"error": "Keine Kursdaten geladen."})
    assert metrics == {"error": "Keine Kursdaten geladen."}


# --- verdict(): EXIT_VARIANT_CRITERIA mechanisch angewandt, JE Variante ---

def test_verdict_prefers_the_variant_when_avg_r_improves_enough():
    reference = {"avg_r": 0.20, "profit_factor": 1.5, "trades": 40}
    variant = {"avg_r": 0.30, "profit_factor": 1.6, "trades": 42}   # +0.10 R

    result = ev.verdict({"referenz": reference, "mindestgewinn_stop": variant})

    assert result["mindestgewinn_stop"]["prefer_variant"] is True
    assert result["mindestgewinn_stop"]["avg_r_gain"] == pytest.approx(0.10)


def test_verdict_keeps_the_reference_when_the_variant_does_not_improve_enough():
    reference = {"avg_r": 0.30, "profit_factor": 1.5, "trades": 40}
    variant = {"avg_r": 0.31, "profit_factor": 1.52, "trades": 40}   # kaum ein Unterschied

    result = ev.verdict({"referenz": reference, "mindestgewinn_stop": variant})

    assert result["mindestgewinn_stop"]["prefer_variant"] is False


def test_verdict_prefers_the_variant_on_profit_factor_alone():
    reference = {"avg_r": 0.30, "profit_factor": 1.00, "trades": 40}
    variant = {"avg_r": 0.30, "profit_factor": 1.10, "trades": 40}   # +10 % PF, Ø R unveraendert

    result = ev.verdict({"referenz": reference, "mindestgewinn_stop": variant})

    assert result["mindestgewinn_stop"]["prefer_variant"] is True
    assert result["mindestgewinn_stop"]["profit_factor_gain_pct"] == pytest.approx(10.0)


def test_verdict_judges_every_non_reference_variant_independently():
    """Zwei Varianten (Schritt 2 + Schritt 3a) - jede bekommt ihr EIGENES
    Urteil gegen dieselbe Referenz, nie gegeneinander."""
    reference = {"avg_r": 0.20, "profit_factor": 1.5, "trades": 40}
    good = {"avg_r": 0.40, "profit_factor": 2.0, "trades": 42}
    bad = {"avg_r": 0.19, "profit_factor": 1.4, "trades": 38}

    result = ev.verdict({"referenz": reference, "mindestgewinn_stop": good,
                         "stuendliches_nachziehen": bad})

    assert result["mindestgewinn_stop"]["prefer_variant"] is True
    assert result["stuendliches_nachziehen"]["prefer_variant"] is False


def test_verdict_reports_error_without_a_valid_reference():
    result = ev.verdict({"referenz": {"error": "kaputt"}, "mindestgewinn_stop": {"avg_r": 0.3}})
    assert "error" in result


def test_verdict_reports_a_per_variant_error_without_swallowing_other_variants():
    reference = {"avg_r": 0.3, "profit_factor": 1.5, "trades": 40}
    ok_variant = {"avg_r": 0.5, "profit_factor": 2.0, "trades": 40}
    result = ev.verdict({"referenz": reference, "mindestgewinn_stop": {"error": "kaputt"},
                         "stuendliches_nachziehen": ok_variant})

    assert "error" in result["mindestgewinn_stop"]
    assert result["stuendliches_nachziehen"]["prefer_variant"] is True
