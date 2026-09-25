"""analysis/bot_walkforward.py: Fenster-Zuschnitt, Regime-Klassifikation,
Verkettung und das eingefrorene Live-Freigabe-Kriterium (GATE_CRITERIA).

Die Fenster-Mechanik (run_walkforward) wird ueber echte, kleine synthetische
Reihen gepruft (dieselbe Konvention wie tests/test_bot_backtest.py); das
GATE selbst (_evaluate_gate) direkt mit handgebauten "windows"-Listen -
die Kriterien sind die sicherheitskritische Entscheidung hier, sie verdienen
eine isolierte, exakt kontrollierte Pruefung statt sich durch die komplette
Signal-Engine hindurch zufaellig zu ergeben."""
import numpy as np
import pandas as pd

from analysis import bot_walkforward as wf


# --- reine Bausteine ---

def test_window_bounds_are_chronological_and_non_overlapping():
    end = pd.Timestamp("2026-09-01", tz="UTC")
    bounds = wf._window_bounds(end, window_days=10, num_windows=3)
    assert len(bounds) == 3
    assert bounds[0][0] == end - pd.Timedelta(days=30)
    assert bounds[-1][1] == end
    for (s1, e1), (s2, e2) in zip(bounds, bounds[1:]):
        assert e1 == s2   # luecken- und ueberlappungsfrei aneinandergereiht
        assert s1 < e1


def test_window_slice_returns_none_without_enough_lookback():
    idx = pd.date_range("2026-01-01", periods=20, freq="4h", tz="UTC")
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)
    # nur 20 Kerzen vorhanden, 90 Vorlauf verlangt -> nie genug.
    piece = wf._window_slice(df, idx[15], idx[19], burn_in_candles=90)
    assert piece is None


def test_window_slice_includes_exact_burn_in_and_window_candles():
    idx = pd.date_range("2026-01-01", periods=200, freq="4h", tz="UTC")
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)
    window_start, window_stop = idx[150], idx[180]
    piece = wf._window_slice(df, window_start, window_stop, burn_in_candles=90)
    assert piece is not None
    assert len(piece) == 90 + 30   # 90 Vorlauf + 30 Kerzen INNERHALB des Fensters
    assert piece.index[0] == idx[150 - 90]
    assert piece.index[-1] == idx[179]   # window_stop selbst ist exklusiv


def test_classify_regime_bull_bear_sideways():
    assert wf._classify_regime(15.0) == "bull"
    assert wf._classify_regime(-15.0) == "bär"
    assert wf._classify_regime(3.0) == "seitwärts"
    assert wf._classify_regime(None) == "unbekannt"


# --- run_walkforward: Fenster-Mechanik ueber synthetische Reihen ---

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
    """Baut BTC/ETH-Stundenreihen, die den GESAMTEN von run_walkforward
    angefragten Zeitraum (num_windows*window_days + Burn-in-Vorlauf) decken,
    endend bei 'jetzt' - candles_fn liefert daraus sowohl 4h- als auch
    1h-Anfragen per Resample, wie es die echte Hyperliquid-Anbindung auch
    grundsaetzlich koennte."""
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


def test_run_walkforward_produces_one_result_per_window():
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    result = wf.run_walkforward(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=5,
                                starting_equity_usd=250.0, candles_fn=candles_fn,
                                daily_fn=daily_fn, funding_fn=funding_fn)
    assert "error" not in result
    assert len(result["windows"]) == 3
    # Aufwaertsreihen ueber den GESAMTEN Zeitraum -> jedes Fenster sollte
    # auswertbar sein (genug Vorlauf, keine Datenluecke).
    assert all("error" not in w for w in result["windows"])
    # chronologisch, luecken-/ueberlappungsfrei.
    for w1, w2 in zip(result["windows"], result["windows"][1:]):
        assert w1["stop"] == w2["start"]


def test_run_walkforward_chains_equity_between_windows():
    """Fenster N+1 startet bei der TATSAECHLICHEN Endsumme von Fenster N,
    nicht wieder bei starting_equity_usd (Modul-Docstring 'FENSTER WERDEN
    VERKETTET')."""
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)
    result = wf.run_walkforward(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=5,
                                starting_equity_usd=250.0, candles_fn=candles_fn,
                                daily_fn=daily_fn, funding_fn=funding_fn)
    windows = [w for w in result["windows"] if "error" not in w]
    assert len(windows) >= 2
    for w1, w2 in zip(windows, windows[1:]):
        assert w2["metrics"]["start_usd"] == w1["metrics"]["end_usd"]
    assert windows[0]["metrics"]["start_usd"] == 250.0
    assert result["ending_equity_usd"] == windows[-1]["metrics"]["end_usd"]


def test_run_walkforward_reports_error_without_any_candles():
    result = wf.run_walkforward(["BTC"], window_days=10, num_windows=2,
                                candles_fn=lambda *a, **k: None,
                                daily_fn=lambda *a, **k: None,
                                funding_fn=lambda *a, **k: None)
    assert "error" in result


def test_run_walkforward_reports_error_when_btc_daily_missing_for_all_windows():
    """Alle Fenster teilen dasselbe (ungeschnittene) btc_daily (Modul-
    Docstring) - ein Ausfall dort trifft sie GLEICHZEITIG. Ohne den Guard
    VOR der Fenster-Schleife haette JEDES einzelne Fenster mangels Regime
    0 Trades gemeldet, was wie ein durchgefallener, aber gueltiger Lauf
    aussieht statt wie das, was es ist: ein Datenausfall."""
    candles_fn, _, funding_fn = _synthetic_fetchers(window_days=20, num_windows=3)

    def empty_daily_fn(symbol, start_ms, end_ms, interval):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = wf.run_walkforward(["BTC", "ETH"], window_days=20, num_windows=3, risk_level=5,
                                starting_equity_usd=250.0, candles_fn=candles_fn,
                                daily_fn=empty_daily_fn, funding_fn=funding_fn)

    assert "error" in result
    assert "BTC-Tagesdaten" in result["error"]
    assert "windows" not in result   # keine einzelne Fenster-Berechnung mehr versucht


def test_run_walkforward_reports_loading_and_window_progress():
    candles_fn, daily_fn, funding_fn = _synthetic_fetchers(window_days=20, num_windows=2)
    events = []
    result = wf.run_walkforward(
        ["BTC", "ETH"], window_days=20, num_windows=2, risk_level=5,
        candles_fn=candles_fn, daily_fn=daily_fn, funding_fn=funding_fn,
        progress_fn=lambda *event: events.append(event))

    assert "error" not in result
    assert any(event[0] == "daten" for event in events)
    assert any(event[0] == "fenster" for event in events)


# --- GATE_CRITERIA: direkt gegen handgebaute Fenster ---

def _fake_trades(n_wins, win_pnl, win_r, n_losses, loss_pnl, loss_r):
    trades = [{"net_pnl_usd": win_pnl, "r_multiple": win_r} for _ in range(n_wins)]
    trades += [{"net_pnl_usd": -loss_pnl, "r_multiple": -loss_r} for _ in range(n_losses)]
    return trades


def _fake_window(index, regime, trades, start_usd, costs_usd=2.0):
    end_usd = start_usd + sum(t["net_pnl_usd"] for t in trades)
    base = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=index * 20)
    curve = pd.Series([start_usd, end_usd], index=[base, base + pd.Timedelta(days=19)])
    return {"index": index, "start": base, "stop": base + pd.Timedelta(days=20),
           "regime": regime, "btc_return_pct": 0.0,
           "metrics": {"start_usd": start_usd, "end_usd": end_usd, "costs_usd": costs_usd,
                      "trades": len(trades)},
           "trades": trades, "equity_curve": curve, "risk_level": 5, "limits": {}}


def _passing_windows():
    """4 Fenster, 12 Trades je Fenster (48 gesamt), 3 verschiedene
    Marktregime, alle nicht negativ - erfuellt bewusst ALLE GATE_CRITERIA
    gleichzeitig als Ausgangspunkt fuer die einzelnen Fehlschlag-Varianten
    unten (je EIN Kriterium gezielt verschlechtert)."""
    regimes = ["bull", "bär", "seitwärts", "bull"]
    windows = []
    equity = 250.0
    for i, regime in enumerate(regimes):
        trades = _fake_trades(n_wins=8, win_pnl=5.0, win_r=2.0,
                              n_losses=4, loss_pnl=2.0, loss_r=1.0)
        w = _fake_window(i, regime, trades, equity)
        windows.append(w)
        equity = w["metrics"]["end_usd"]
    return windows


def test_gate_passes_when_all_criteria_are_met():
    gate = wf._evaluate_gate(_passing_windows())
    assert gate["ok"] is True, gate["reasons"]
    assert gate["reasons"] == []
    assert gate["pooled"]["trades"] == 48


def test_gate_fails_with_too_few_windows():
    gate = wf._evaluate_gate(_passing_windows()[:2])
    assert gate["ok"] is False
    assert any("Fenster" in r for r in gate["reasons"])
    assert gate["pooled"] is None


def test_gate_fails_with_too_few_trades():
    windows = _passing_windows()
    for w in windows:
        w["trades"] = w["trades"][:3]   # 4*3=12 < 40
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("Trades" in r for r in gate["reasons"])


def test_gate_fails_on_low_profit_factor():
    windows = _passing_windows()
    for w in windows:
        # Verluste massiv erhoehen -> Profit-Faktor faellt unter 1,25.
        w["trades"] = _fake_trades(8, 5.0, 2.0, 4, 20.0, 1.0)
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("Profit-Faktor" in r for r in gate["reasons"])


def test_gate_fails_on_non_positive_avg_r():
    windows = _passing_windows()
    for w in windows:
        w["trades"] = _fake_trades(8, 5.0, 0.5, 4, 2.0, 1.5)   # Ø R <= 0
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("R-Multiple" in r for r in gate["reasons"])


def test_gate_fails_on_deep_drawdown():
    windows = _passing_windows()
    # Ein Fenster mit einem harten Einbruch, ohne dass die anderen ihn
    # ausgleichen - > 20% auf der VERKETTETEN Kurve.
    windows[1]["equity_curve"] = pd.Series(
        [windows[1]["equity_curve"].iloc[0], windows[1]["equity_curve"].iloc[0] * 0.7],
        index=windows[1]["equity_curve"].index)
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("Rückgang" in r for r in gate["reasons"])


def test_gate_fails_on_high_cost_share():
    windows = _passing_windows()
    for w in windows:
        w["metrics"]["costs_usd"] = 100.0   # weit ueber 40% des Bruttogewinns
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("Kosten" in r for r in gate["reasons"])


def test_gate_fails_when_fewer_than_two_regimes_are_nonnegative():
    windows = _passing_windows()
    # Alle Fenster demselben (einzigen) Regime zuordnen -> hoechstens 1
    # Regime kann "nicht negativ" sein, das Kriterium verlangt 2.
    for w in windows:
        w["regime"] = "bull"
    gate = wf._evaluate_gate(windows)
    assert gate["ok"] is False
    assert any("Marktregimen" in r for r in gate["reasons"])


def test_gate_regime_bucket_ignores_zero_trade_windows():
    """DIE Luecke, die _regime_breakdown()'s Trades-Filter schliesst: mit
    ALLOW_SHORT=False produziert ein Baer-Fenster typischerweise 0 Trades
    (regime() sagt "short", evaluate() blockt jeden Kandidaten mangels
    Short-Faehigkeit) - end_usd - start_usd == 0 waere ">= 0" und zaehlte
    OHNE diesen Filter als "nicht negativ", selbst wenn es fuer dieses
    Regime ueberhaupt keine Handelsevidenz gibt. Hier haben ALLE echten
    Trades nur EIN Regime ("bull") - ohne den Filter wuerde das leere
    "baer"-Fenster als zweites "nicht negatives" Regime durchgehen und das
    Kriterium (min_nonnegative_regimes=2) truegerisch erfuellen."""
    windows = _passing_windows()
    for w in windows:
        w["regime"] = "bull"
    empty_trades = []
    empty_window = _fake_window(len(windows), "bär", empty_trades,
                                windows[-1]["metrics"]["end_usd"])
    windows.append(empty_window)

    gate = wf._evaluate_gate(windows)

    assert gate["ok"] is False
    assert any("Marktregimen" in r for r in gate["reasons"])
    # Das leere Baer-Fenster darf ueberhaupt nicht im Regime-Bucket auftauchen -
    # nur "bull" hat echte Handelsevidenz.
    assert set(gate["by_regime"].keys()) == {"bull"}


def test_gate_treats_error_windows_as_invalid():
    windows = _passing_windows()
    windows[0] = {"index": 0, "start": windows[0]["start"], "stop": windows[0]["stop"],
                 "error": "Keine Kursdaten geladen.", "regime": "unbekannt"}
    gate = wf._evaluate_gate(windows)
    # Nur noch 3 gueltige Fenster - GATE_CRITERIA['min_windows']=3 reicht
    # gerade noch, der Fehlergrund muss aber trotzdem auftauchen.
    assert any("Keine Kursdaten" in r for r in gate["reasons"])
