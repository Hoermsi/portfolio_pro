"""Äquivalenztest Rücktest <-> Live (Plan-Phase 9, Verifikation Punkt 2).

`core/bot_cycle.py` (ein gemeinsamer decide()-Kern, den beide Pfade
aufrufen) wurde in Phase 6 BEWUSST nicht gebaut: die eigentlichen
Entscheidungsregeln (core.bot_signals.evaluate/scan, core.bot_guards.
evaluate_entry_order) sind bereits seit Phase 4/5 zu 100% gemeinsame reine
Funktionen, die core.bot.run_signal_cycle() UND analysis.bot_backtest.
run_backtest() beide aufrufen - eine weitere Schicht draufzusetzen haette
nur die duenne Kandidatenauswahl-Schleife DRY gemacht, ohne eine neue
Korrektheitsluecke zu schliessen (siehe Commit "Phase 6").

Dieser Test prueft die eigentliche BEHAUPTUNG dahinter direkt: gefuettert
mit EXAKT demselben Kerzenfenster, derselben Risikostufe und derselben
Ausgangs-Equity muessen Rücktest und Live-Pfad zur SELBEN Entscheidung
kommen (Symbol, Richtung, Nominale) - nicht weil ein gemeinsamer
Orchestrator es erzwingt, sondern weil die zugrundeliegenden Funktionen
tatsaechlich identisch sind. Eine kuenftige Drift zwischen den beiden
duennen Aufrufer-Schleifen wuerde hier auffallen.
"""
import numpy as np
import pandas as pd
import pytest

from analysis import bot_backtest
from core import bot, bot_config, bot_signals
from data.hyperliquid import PaperExchange


def _uptrend_series(n, drift=0.0022, noise=0.0032, seed=3, start=100.0):
    """Dieselbe Kalibrierung wie tests/test_bot_backtest.py._series() - klart
    verlaesslich alle 7 Pflichtbedingungen (core.bot_signals.evaluate)."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    op = np.concatenate([[start], close[:-1]])
    high = np.maximum(op, close) * (1 + abs(rng.normal(0, noise, n)))
    low = np.minimum(op, close) * (1 - abs(rng.normal(0, noise, n)))
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"Open": op, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(n, 1000.0)}, index=idx)


def _daily_bull(n_days, seed=90):
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(0.006 + rng.normal(0, 0.006, n_days)))
    op = np.concatenate([[100.0], close[:-1]])
    idx = pd.date_range("2025-01-01", periods=n_days, freq="1d", tz="UTC")
    return pd.DataFrame({"Open": op, "High": np.maximum(op, close) * 1.01,
                         "Low": np.minimum(op, close) * 0.99, "Close": close,
                         "Volume": np.full(n_days, 1000.0)}, index=idx)


def test_backtest_and_live_open_the_identical_position_from_the_same_data(tmp_db, monkeypatch):
    burn_in = bot_backtest.BURN_IN_CANDLES
    full_series = _uptrend_series(burn_in + 5)
    btc_daily = _daily_bull(200)

    # --- Rücktest: EIN Symbol, genau bis zur ersten handelbaren Kerze ---
    market = {"candles": {"BTC": full_series}, "funding": {}, "missing_funding": [],
             "btc_daily": btc_daily}
    bt_result = bot_backtest.run_backtest(market, risk_level=5, starting_equity_usd=250.0)
    assert bt_result.get("error") is None
    assert bt_result["metrics"]["trades"] > 0
    bt_trade = bt_result["trades"][0]

    # --- Live: EXAKT dasselbe Fenster (burn_in Kerzen + 1 "laufende", die
    # bot_signals.prepare() intern verwirft - siehe dessen Docstring) bis zum
    # selben Entscheidungspunkt, derselbe Live-Kurs wie der Rücktest-Fill
    # (Open der Entscheidungskerze, vor Slippage). ---
    decision_window = full_series.iloc[:burn_in + 1]
    entry_open = float(full_series["Open"].iloc[burn_in])
    exchange = PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: entry_open,
                             funding_fn=lambda s: None)
    bot.initialize(exchange)
    bot_config.save_risk_level(5)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    # bot_signals.scan() ermittelt das Tagesregime INTERN ueber regime(daily_fn(...))
    # (core.bot._current_btc_regime() dient nur core.bot.run_signal_cycle()s
    # eigenem Ausstiegs-Zweig) - ohne diesen Patch blockt die global in
    # conftest.py verdrahtete Netzwerksperre (hyperliquid.candles_df -> None)
    # jede Regime-Ermittlung, und Gate 1 wuerde ausnahmslos blocken.
    monkeypatch.setattr(bot_signals, "regime", lambda *a, **k: "long")
    monkeypatch.setattr(bot, "_current_btc_regime", lambda *a, **k: "long")
    # Gate 7 (Liquiditaet): dieselbe Netzwerksperre macht
    # data.hyperliquid.day_notional_volume_usd() sonst ueberall None.
    import data.hyperliquid as hl
    monkeypatch.setattr(hl, "day_notional_volume_usd", lambda *a, **k: 1e9)

    live_result = bot.run_signal_cycle(exchange, candles_fn=lambda s: decision_window)

    assert live_result["entry"] is not None, live_result["skipped"]
    assert live_result["entry"]["result"]["status"] == "filled"

    # --- Dieselbe Entscheidung: Symbol, Richtung, risikobasierte Nominale
    # (core.bot_signals.position_size_usd - beide Pfade rufen exakt dieselbe
    # Funktion mit denselben Argumenten auf, siehe Modul-Docstring). ---
    assert live_result["entry"]["symbol"] == bt_trade["symbol"] == "BTC"
    assert live_result["entry"]["side"] == bt_trade["side"]
    assert live_result["entry"]["notional_usd"] == pytest.approx(
        bt_trade["notional_usd"], rel=1e-6)
