"""analysis/bot_ablation.py: Gate-Ablation ueber Walk-Forward-Fenster
(Phase 3, Validierungs-Integritaet/Gate-Ablation).

Rein synthetische Kursreihen, kein Netz - dieselbe Konvention wie
tests/test_bot_walkforward.py. Der Schwerpunkt liegt auf der MECHANIK
(ein Datenabruf fuer alle Varianten, disabled_gates korrekt durchgereicht,
verdict() wendet ABLATION_CRITERIA mechanisch an) - nicht auf einer
bestimmten Rendite, die ohnehin an der erfundenen Reihe haengt."""
import numpy as np
import pandas as pd
import pytest

from analysis import bot_ablation as ab
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
    """Identisch zum Vorbild in tests/test_bot_walkforward.py - siehe dort
    fuer die ausfuehrliche Erklaerung."""
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


# --- run_ablation(): Mechanik ---

def test_run_ablation_reports_baseline_and_every_variant():
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    result = ab.run_ablation(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
                             starting_equity_usd=250.0, candles_fn=candles_fn,
                             daily_fn=daily_fn, funding_fn=funding_fn)

    assert "error" not in result
    assert set(result["variants"]) == set(ab.VARIANTS)
    for name, disabled in ab.VARIANTS.items():
        assert result["variants"][name]["disabled_gates"] == sorted(disabled)


def test_run_ablation_loads_market_data_exactly_once():
    """Der ganze Sinn des Moduls: EIN Datenabruf fuer ALLE Varianten, sonst
    saehe unterschiedliche Datenvollstaendigkeit zwischen Varianten wie ein
    Gate-Effekt aus (siehe Moduldoc)."""
    import analysis.bot_backtest as bb

    calls = {"n": 0}
    original = bb.load_market_data

    def counting_load(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    import analysis.bot_ablation as ab_module
    orig_ref = ab_module.bot_backtest.load_market_data
    ab_module.bot_backtest.load_market_data = counting_load
    try:
        ab_module.run_ablation(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
                               starting_equity_usd=250.0, candles_fn=candles_fn,
                               daily_fn=daily_fn, funding_fn=funding_fn)
    finally:
        ab_module.bot_backtest.load_market_data = orig_ref

    assert calls["n"] == 1


def test_run_ablation_reports_error_without_any_candles():
    result = ab.run_ablation(["BTC"], window_days=10, num_windows=2,
                             candles_fn=lambda *a, **k: None,
                             daily_fn=lambda *a, **k: None,
                             funding_fn=lambda *a, **k: None)
    assert "error" in result


def test_run_ablation_reports_error_when_btc_daily_missing():
    """Derselbe Guard wie bot_walkforward.run_walkforward - alle Varianten
    teilen dasselbe btc_daily, ein Ausfall traefe sie gleichzeitig."""
    candles_fn, _, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)

    def empty_daily_fn(symbol, start_ms, end_ms, interval):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = ab.run_ablation(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=9,
                             candles_fn=candles_fn, daily_fn=empty_daily_fn,
                             funding_fn=funding_fn)
    assert "error" in result
    assert "BTC-Tagesdaten" in result["error"]


def test_run_ablation_reports_error_for_num_windows_below_one():
    result = ab.run_ablation(["BTC"], window_days=10, num_windows=0)
    assert "error" in result


def test_run_ablation_progress_callback_receives_daten_and_variante_stages():
    events = []
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=2)
    ab.run_ablation(["BTC", "ETH"], window_days=20, num_windows=2, risk_level=9,
                    candles_fn=candles_fn, daily_fn=daily_fn, funding_fn=funding_fn,
                    progress_fn=lambda *event: events.append(event))

    assert any(e[0] == "daten" for e in events)
    assert any(e[0] == "variante" for e in events)
    # Fortschritt bis zur letzten Variante gezaehlt.
    variant_events = [e for e in events if e[0] == "variante"]
    assert variant_events[-1][1] == len(ab.VARIANTS)


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
    metrics = ab._variant_metrics({"windows": windows})

    assert metrics["valid_windows"] == 2
    assert metrics["trades"] == 10   # nur die zwei gueltigen Fenster gepoolt
    assert metrics["trades_per_day"] == pytest.approx(10 / 40)   # 2 x 20 Tage
    assert metrics["avg_r"] is not None


def test_variant_metrics_passes_through_the_error():
    metrics = ab._variant_metrics({"error": "Keine Kursdaten geladen."})
    assert metrics == {"error": "Keine Kursdaten geladen."}


# --- verdict(): ABLATION_CRITERIA mechanisch angewandt ---

def test_verdict_keeps_a_gate_whose_removal_drops_avg_r_enough():
    baseline = {"disabled_gates": [], "avg_r": 1.0, "profit_factor": 2.0, "trades": 20}
    without_gate2 = {"disabled_gates": ["gate2"], "avg_r": 0.9, "profit_factor": 1.9, "trades": 20}
    variants = {"baseline": baseline, "ohne_gate2_ma_struktur": without_gate2}

    result = ab.verdict(variants)

    assert result["ohne_gate2_ma_struktur"]["keep_gate"] is True
    assert result["ohne_gate2_ma_struktur"]["avg_r_drop"] == pytest.approx(0.1)


def test_verdict_drops_a_gate_whose_removal_does_not_hurt_quality():
    baseline = {"disabled_gates": [], "avg_r": 1.0, "profit_factor": 2.0, "trades": 20}
    without_gate4 = {"disabled_gates": ["gate4"], "avg_r": 1.02, "profit_factor": 2.1, "trades": 25}
    variants = {"baseline": baseline, "ohne_gate4_momentum": without_gate4}

    result = ab.verdict(variants)

    assert result["ohne_gate4_momentum"]["keep_gate"] is False


def test_verdict_profit_factor_criterion_alone_can_justify_a_gate():
    """Eines der beiden Kriterien reicht (siehe ABLATION_CRITERIA-Docstring) -
    hier faellt Ø R kaum, aber der Profit-Faktor deutlich."""
    baseline = {"disabled_gates": [], "avg_r": 1.0, "profit_factor": 2.0, "trades": 20}
    without_gate6 = {"disabled_gates": ["gate6"], "avg_r": 0.99, "profit_factor": 1.5, "trades": 22}
    variants = {"baseline": baseline, "ohne_gate6_volumen": without_gate6}

    result = ab.verdict(variants)

    assert result["ohne_gate6_volumen"]["keep_gate"] is True
    assert result["ohne_gate6_volumen"]["profit_factor_drop_pct"] == pytest.approx(25.0)


def test_verdict_skips_multi_gate_combinations():
    """"ohne_gate4_und_5" betrifft ZWEI Gates gleichzeitig - verdict() trifft
    dafuer bewusst kein Einzel-Gate-Urteil (siehe Docstring)."""
    baseline = {"disabled_gates": [], "avg_r": 1.0, "profit_factor": 2.0, "trades": 20}
    combo = {"disabled_gates": ["gate4", "gate5"], "avg_r": 1.5, "profit_factor": 2.5, "trades": 30}
    variants = {"baseline": baseline, "ohne_gate4_und_5": combo}

    result = ab.verdict(variants)

    assert "ohne_gate4_und_5" not in result


def test_verdict_reports_error_without_a_valid_baseline():
    assert "error" in ab.verdict({})
    assert "error" in ab.verdict({"baseline": {"error": "kaputt"}})


def test_verdict_handles_missing_profit_factor_gracefully():
    """profit_factor ist None, wenn es keine Verlierer gibt (bot_walkforward.
    _pooled_metrics) - verdict() darf daran nicht mit ZeroDivisionError/TypeError
    scheitern, sondern faellt auf das avg_r-Kriterium zurueck."""
    baseline = {"disabled_gates": [], "avg_r": 1.0, "profit_factor": None, "trades": 10}
    without_gate3 = {"disabled_gates": ["gate3"], "avg_r": 0.5, "profit_factor": None, "trades": 12}
    variants = {"baseline": baseline, "ohne_gate3_trend_effizienz": without_gate3}

    result = ab.verdict(variants)

    assert result["ohne_gate3_trend_effizienz"]["profit_factor_drop_pct"] is None
    assert result["ohne_gate3_trend_effizienz"]["keep_gate"] is True   # avg_r-Abfall reicht allein
