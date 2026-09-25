"""analysis/bot_backtest.py: Walk-Forward-Ruecktest der Bot-Regeln.

Rein synthetische Kursreihen, kein Netz - dieselbe Konvention wie
tests/test_bot_signals.py. Der wichtigste Test hier ist NICHT die Rendite
(die haengt an der erfundenen Reihe), sondern der Lookahead-Nachweis: eine
Aenderung an spaeteren Kerzen darf frueher entstandene Trades nicht
beeinflussen. Ohne diese Zusicherung ist jede Rueckrechnung wertlos.
"""
import numpy as np
import pandas as pd
import pytest

from analysis import bot_backtest as bb
from data.hyperliquid import TAKER_FEE


def _series(n=500, drift=0.0022, noise=0.0032, seed=3, start=100.0):
    """drift/noise gegenueber V1 angehoben: die V2-Signal-Engine verlangt
    einen ATR-Boden von core.bot_signals._ATR_PCT_MIN (0,6 %, vorher 0,25 %)
    - eine ruhigere Reihe wuerde am ATR-Boden haengen bleiben, bevor sie
    ueberhaupt als Kandidat in Frage kaeme (das seit STRATEGY_VERSION
    "2.2-swing4h" entfernte Momentum-Gate spielte hier frueher zusaetzlich
    eine Rolle, siehe core.bot_signals's Konstanten-Block). `drift` bewusst
    moderat (nicht staerker, obwohl das die verbleibenden Gates lockerer
    erfuellen wuerde): bei MAX_HOLD_HOURS=336 (14 Tage) komprimiert ein zu
    starker stuendlicher Drift ueber die volle Haltedauer zu einem
    unrealistischen Vielfachen des Einstiegspreises."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    op = np.concatenate([[start], close[:-1]])
    high = np.maximum(op, close) * (1 + abs(rng.normal(0, noise, n)))
    low = np.minimum(op, close) * (1 - abs(rng.normal(0, noise, n)))
    # tz-aware UTC, wie data.hyperliquid.candles_range() es seit core.clock
    # tatsaechlich liefert - sonst kollidiert ts.to_pydatetime() (tz-aware)
    # in core.bot_signals.exit_signal() mit einem tz-naiven "now".
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"Open": op, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(n, 1000.0)}, index=idx)


def _daily_btc(n_days=150, start="2025-09-01", drift=0.006, noise=0.006, seed=90):
    """BTC-Tageskerzen fuer das Regime (Gate 1) - reicht deutlich vor den
    eigentlichen Ruecktest-Zeitraum zurueck (core.bot_signals.
    _REGIME_MIN_CANDLES + Vorlauf), sonst waere das Regime fuer den
    kompletten Testzeitraum mangels Historie 'neutral'."""
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, noise, n_days)))
    op = np.concatenate([[100.0], close[:-1]])
    idx = pd.date_range(start, periods=n_days, freq="1d", tz="UTC")
    return pd.DataFrame({"Open": op, "High": np.maximum(op, close) * 1.01,
                         "Low": np.minimum(op, close) * 0.99, "Close": close,
                         "Volume": np.full(n_days, 1000.0)}, index=idx)


def _market(btc_daily=None, **series_by_symbol):
    return {"candles": dict(series_by_symbol), "funding": {}, "missing_funding": [],
           "btc_daily": btc_daily if btc_daily is not None else _daily_btc()}


def _uptrend_market():
    return _market(BTC=_series(seed=1), ETH=_series(seed=2), SOL=_series(seed=5))


def test_uptrend_produces_long_trades():
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)
    assert result.get("error") is None
    assert result["metrics"]["trades"] > 0
    # Reine Aufwaertsreihen -> die Engine ist trendfolgend, also long.
    assert set(result["by_side"]) == {"long"}
    assert len(result["equity_curve"]) > 0


# --- _entry_candle_stop (Phase 6: Eroeffnungskerzen-Stop-Check) ---

def _long_pos(entry_px=100.0, stop_px=95.0):
    return bb._Position(symbol="BTC", side="long", size=1.0, entry_px=entry_px,
                        stop_px=stop_px, initial_stop_px=stop_px,
                        opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                        initial_risk_usd=abs(entry_px - stop_px))


def test_entry_candle_stop_hourly_precision_detects_hit_inside_the_bar():
    pos = _long_pos()
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    bar = pd.Series({"Open": 100.0, "High": 102.0, "Low": 98.0, "Close": 101.0})
    hourly = pd.DataFrame({
        "Open":  [100.0, 99.0, 94.0, 96.0],
        "High":  [100.5, 99.5, 95.0, 97.0],
        "Low":   [99.0, 94.0, 93.0, 95.5],
        "Close": [99.5, 94.5, 94.5, 96.5],
    }, index=pd.date_range(ts, periods=4, freq="1h", tz="UTC"))
    hit = bb._entry_candle_stop(pos, ts, bar, hourly)
    assert hit is not None
    hit_ts, hit_px = hit
    assert hit_ts == hourly.index[1]   # zweite 1h-Kerze reisst den Stop zuerst
    assert hit_px == 95.0              # kein Gap unter den Stop -> exakter Fill


def test_entry_candle_stop_hourly_gap_fills_at_the_gapped_open():
    pos = _long_pos()
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    bar = pd.Series({"Open": 100.0, "High": 100.0, "Low": 90.0, "Close": 91.0})
    hourly = pd.DataFrame({
        "Open": [100.0, 92.0], "High": [100.5, 93.0],
        "Low": [98.0, 90.0], "Close": [98.5, 91.0],
    }, index=pd.date_range(ts, periods=2, freq="1h", tz="UTC"))
    hit_ts, hit_px = bb._entry_candle_stop(pos, ts, bar, hourly)
    assert hit_ts == hourly.index[1]
    assert hit_px == 92.0   # Open der zweiten Unterkerze liegt schon unter dem Stop (95)


def test_entry_candle_stop_falls_back_to_4h_bar_without_hourly_data():
    """Fehlen 1h-Daten fuer dieses Symbol (aeltere/synthetische Tests, oder
    die Boerse liefert keine) - die Luecke bleibt trotzdem geschlossen,
    nur mit derselben 4h-Bar-Naeherung wie jede andere Kerze."""
    pos = _long_pos()
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    bar = pd.Series({"Open": 100.0, "High": 102.0, "Low": 90.0, "Close": 101.0})
    assert bb._entry_candle_stop(pos, ts, bar, None) == (ts, 95.0)
    assert bb._entry_candle_stop(pos, ts, bar, pd.DataFrame()) == (ts, 95.0)


def test_entry_candle_stop_none_when_not_hit():
    pos = _long_pos()
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    bar = pd.Series({"Open": 100.0, "High": 102.0, "Low": 96.0, "Close": 101.0})
    assert bb._entry_candle_stop(pos, ts, bar, None) is None


def test_entry_candle_stop_short_side_and_gap_fill():
    pos = bb._Position(symbol="BTC", side="short", size=1.0, entry_px=100.0,
                       stop_px=105.0, initial_stop_px=105.0,
                       opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                       initial_risk_usd=5.0)
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    # Gap: der Bar-Open selbst liegt schon UEBER dem Stop -> Fill dort.
    bar = pd.Series({"Open": 108.0, "High": 110.0, "Low": 107.0, "Close": 109.0})
    assert bb._entry_candle_stop(pos, ts, bar, None) == (ts, 108.0)


def test_entry_candle_stop_wiring_forces_immediate_stop_out():
    """Integrationstest der VERDRAHTUNG (nicht nur der reinen Funktion): mit
    1h-Unterkerzen, deren Low IMMER weit unter jedem plausiblen Long-Stop
    liegt, muss JEDE Position in genau der Kerze schliessen, in der sie
    eroeffnet wurde (hold_hours == 0, reason == 'stop') - vor Phase 6 waere
    sie erst eine Kerze (4h) zu spaet als Verlierer erkannt worden."""
    market = _uptrend_market()
    hourly = {}
    for symbol, df in market["candles"].items():
        idx = pd.date_range(df.index[0], df.index[-1] + pd.Timedelta(hours=4),
                            freq="1h", tz="UTC")
        hourly[symbol] = pd.DataFrame(
            {"Open": 1.0, "High": 1.0, "Low": 0.01, "Close": 1.0}, index=idx)
    market["hourly"] = hourly

    result = bb.run_backtest(market, risk_level=5, starting_equity_usd=250.0)
    assert result.get("error") is None
    assert result["metrics"]["trades"] > 0
    assert all(t["reason"] == "stop" for t in result["trades"])
    assert all(t["hold_hours"] == 0 for t in result["trades"])


# --- exit_variant="min_profit_stop" (Ausstiegsmechanik-Vergleich, Schritt 2) ---

def test_min_profit_stop_px_none_below_activation_threshold():
    """Vor +1R gilt wie bei der Referenz ausschliesslich der Anfangsstop -
    dieselbe TRAILING_ACTIVATION_R wie core.bot_signals.trailing_stop_px(),
    damit der Vergleich NUR die Stop-Formel isoliert."""
    pos = _long_pos(entry_px=100.0, stop_px=95.0)   # initial_risk_usd=5.0
    assert bb._min_profit_stop_px(pos, 104.0) is None   # R=0.8


def test_min_profit_stop_px_locks_at_cost_adjusted_breakeven_once_activated():
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    locked = bb._min_profit_stop_px(pos, 105.0)   # R=1.0, genau die Schwelle
    expected = 100.0 * (1 + bb.bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0)
    assert locked == pytest.approx(expected)
    assert locked > 100.0   # oberhalb des reinen Einstands - Kosten sind gedeckt


def test_min_profit_stop_px_short_side_locks_below_entry():
    pos = bb._Position(symbol="BTC", side="short", size=1.0, entry_px=100.0,
                       stop_px=105.0, initial_stop_px=105.0,
                       opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                       initial_risk_usd=5.0)
    locked = bb._min_profit_stop_px(pos, 95.0)   # R=(95-100)*-1/5=1.0
    expected = 100.0 * (1 - bb.bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0)
    assert locked == pytest.approx(expected)
    assert locked < 100.0


def test_min_profit_stop_px_is_a_one_time_lock_no_further_movement():
    """Bewusster Unterschied zur Referenz (Chandelier zieht IMMER weiter
    nach): einmal gesetzt, bleibt der Mindestgewinn-Stop stehen, auch wenn
    der Kurs danach weit darueber hinauslaeuft."""
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    locked = bb._min_profit_stop_px(pos, 105.0)
    pos.stop_px = locked
    assert bb._min_profit_stop_px(pos, 130.0) is None


def test_min_profit_stop_px_none_without_price_or_risk():
    assert bb._min_profit_stop_px(_long_pos(), None) is None
    assert bb._min_profit_stop_px(_long_pos(), 0.0) is None
    zero_risk = bb._Position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                             stop_px=100.0, initial_stop_px=100.0,
                             opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                             initial_risk_usd=0.0)
    assert bb._min_profit_stop_px(zero_risk, 110.0) is None


def _sized_long_pos(size, entry_px=100.0, stop_px=95.0):
    """Wie _long_pos(), aber mit einer explizit gewaehlten Stueckzahl -
    initial_risk_usd korrekt als GESAMT-Dollar-Risiko (Kursabstand * size),
    genau wie run_backtest() sie tatsaechlich befuellt (Zeile mit
    `initial_risk_usd=abs(fill - stop_px) * size`). _long_pos() selbst
    verschleiert den Einheitenfehler unten, weil es immer size=1.0 verwendet
    (Kursabstand und Dollar-Risiko fallen dort zufaellig zusammen)."""
    return bb._Position(symbol="BTC", side="long", size=size, entry_px=entry_px,
                        stop_px=stop_px, initial_stop_px=stop_px,
                        opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                        initial_risk_usd=abs(entry_px - stop_px) * size)


def test_min_profit_stop_px_activation_is_independent_of_position_size():
    """Reproduziert den vom externen Review (13.09.2026) gefundenen
    Einheitenfehler: der Zaehler (current_price - entry_px) ist ein
    Kurs-Delta PRO EINHEIT, initial_risk_usd aber das GESAMTE Dollar-Risiko
    der Position (Kursabstand * size). Bei identischer prozentualer
    Kursbewegung darf die Aktivierung nicht von der Stueckzahl abhaengen -
    vorher aktivierte eine kleine Stueckzahl VERFRUEHT (schien staerker im
    Plus, als sie tatsaechlich war) und eine grosse Stueckzahl GAR NICHT
    (schien schwaecher im Plus) - siehe die Reproduktionstabelle im Review
    (0,1/10/1 Stueck bei identischer Kursbewegung)."""
    entry_px, stop_px = 100.0, 95.0
    expected = entry_px * (1 + bb.bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0)
    # Genau +1R am Kurs (5 $ Bewegung / 5 $ Kursabstand) - muss bei JEDER
    # Stueckzahl gleich aktivieren, nicht nur bei size=1.0.
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        locked = bb._min_profit_stop_px(pos, 105.0)
        assert locked == pytest.approx(expected), f"size={size} aktivierte nicht identisch"
    # Deutlich UNTER +1R am Kurs (0,2R) - darf bei KEINER Stueckzahl
    # aktivieren (der Fehler liess kleine Stueckzahlen hier faelschlich
    # aktivieren, weil er die Bewegung relativ zum viel kleineren
    # initial_risk_usd ueberschaetzte).
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        assert bb._min_profit_stop_px(pos, 101.0) is None, f"size={size} aktivierte verfrueht"


# --- exit_variant="hourly_trailing" (Ausstiegsmechanik-Vergleich, Schritt 3a) ---

def _flat_window(n=150, level=100.0, wick_pct=0.3):
    """4h-Fenster mit EXAKT vorhersagbarem ATR: eine konstante Schlusskurs-
    reihe erzeugt eine konstante True Range (High-Low, da Open==Close==
    Vortages-Close), deren EWM-Mittel exakt dieser Konstante entspricht -
    keine Zufallsreihe, damit die Stop-Werte unten von Hand nachgerechnet
    werden koennen statt nur behauptet zu werden."""
    closes = np.full(n, level)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({"Open": opens, "High": closes * (1 + wick_pct / 100),
                       "Low": closes * (1 - wick_pct / 100), "Close": closes,
                       "Volume": np.full(n, 1000.0)}, index=idx)
    return bb.technical.add_indicators(df)


# ATR (Dollar) = wick_pct/100 * level = 0.3/100*100 = 0.3*2 = 0.6 (High-Low je Kerze,
# konstant -> EWM-Mittel ist exakt dieselbe Konstante). atr_pct = ATR/level*100 = 0.6.
_FLAT_ATR_PCT = 0.6


def test_hourly_trailing_stop_px_inactive_before_one_r():
    window = _flat_window()
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    assert bb._hourly_trailing_stop_px(pos, ts, window, None, 102.0) == (None, None)   # R=0.4


def test_hourly_trailing_stop_px_activation_is_independent_of_position_size():
    """Derselbe Einheitenfehler wie bei _min_profit_stop_px() (siehe dessen
    Test test_min_profit_stop_px_activation_is_independent_of_position_size
    fuer die vollstaendige Begruendung), hier fuer die stuendliche Variante."""
    window = _flat_window()
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    entry_px, stop_px = 100.0, 95.0
    # R=0.4 (current_price=102): darf bei KEINER Stueckzahl aktivieren.
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        assert bb._hourly_trailing_stop_px(pos, ts, window, None, 102.0) == (None, None), \
            f"size={size} aktivierte verfrueht"
    # R=1.2 (current_price=106, dieselbe stuendliche Kerze wie in
    # test_hourly_trailing_stop_px_tightens_within_the_4h_window_without_
    # hitting): muss bei JEDER Stueckzahl denselben neuen Stop ergeben.
    current_price = 106.0
    hourly = pd.DataFrame({
        "Open":  [106.0, 107.5, 108.5],
        "High":  [107.0, 108.0, 109.0],
        "Low":   [106.5, 107.5, 108.5],
        "Close": [106.5, 108.0, 109.0],
    }, index=pd.date_range(ts, periods=3, freq="1h", tz="UTC"))
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected = 107.0 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        new_stop, hit = bb._hourly_trailing_stop_px(pos, ts, window, hourly, current_price)
        assert hit is None
        assert new_stop == pytest.approx(expected), f"size={size} ergab einen anderen Stop"


def test_hourly_trailing_stop_px_none_without_hourly_data():
    window = _flat_window()
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    assert bb._hourly_trailing_stop_px(pos, ts, window, None, 110.0) == (None, None)
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    assert bb._hourly_trailing_stop_px(pos, ts, window, empty, 110.0) == (None, None)


def test_hourly_trailing_stop_px_tightens_within_the_4h_window_without_hitting():
    """Kernidee von Schritt 3a: der Stop wird SCHON INNERHALB der laufenden
    4h-Kerze straffer gezogen (anhand der 1h-Hochs), nicht erst beim
    naechsten 4h-Takt."""
    window = _flat_window()   # window["High"].max() == 100.3 - weit unter den 1h-Werten unten
    pos = _long_pos(entry_px=100.0, stop_px=95.0)   # initial_risk_usd=5.0
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    current_price = 106.0   # R=(106-100)/5=1.2 -> aktiviert
    hourly = pd.DataFrame({
        "Open":  [106.0, 107.5, 108.5],
        "High":  [107.0, 108.0, 109.0],
        "Low":   [106.5, 107.5, 108.5],
        "Close": [106.5, 108.0, 109.0],
    }, index=pd.date_range(ts, periods=3, freq="1h", tz="UTC"))

    new_stop, hit = bb._hourly_trailing_stop_px(pos, ts, window, hourly, current_price)

    assert hit is None
    # Stunde 1: extreme=107.0, candidate=107.0-3*0.006*106.0=105.092 (< 106.0, gueltig).
    # Stunden 2/3: candidate >= current_price (Sicherheitsklammer wie bei der
    # Referenz) -> kein weiteres Nachziehen DIESEN Takt.
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected = 107.0 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs
    assert new_stop == pytest.approx(expected)
    assert 95.0 < new_stop < current_price


def test_hourly_trailing_stop_px_detects_an_intrabar_reversal():
    """Genau das dokumentierte Live-Muster (LINK/ETHFI): ein Ruecksetzer
    INNERHALB derselben 4h-Kerze, in der der Stop gerade erst nachgezogen
    wurde - die Referenz (nur 1x je 4h-Takt) haette das erst eine Kerze
    spaeter gesehen."""
    window = _flat_window()
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    current_price = 106.0
    hourly = pd.DataFrame({
        "Open":  [106.0, 108.0],
        "High":  [107.0, 109.0],
        "Low":   [106.5, 84.0],    # zweite Stunde: heftiger Ruecksetzer
        "Close": [106.5, 85.0],
    }, index=pd.date_range(ts, periods=2, freq="1h", tz="UTC"))

    new_stop, hit = bb._hourly_trailing_stop_px(pos, ts, window, hourly, current_price)

    assert hit is not None
    hit_ts, hit_px = hit
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected_stop = 107.0 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs   # aus Stunde 1
    assert new_stop == pytest.approx(expected_stop)
    assert hit_ts == hourly.index[1]
    assert hit_px == pytest.approx(expected_stop)   # Fill am Stop, nicht am (hoeheren) Open


def test_hourly_trailing_stop_px_short_side():
    window = _flat_window()
    pos = bb._Position(symbol="BTC", side="short", size=1.0, entry_px=100.0,
                       stop_px=105.0, initial_stop_px=105.0,
                       opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                       initial_risk_usd=5.0)
    ts = pd.Timestamp("2026-06-01", tz="UTC")
    current_price = 94.0   # R=(94-100)*-1/5=1.2 -> aktiviert
    hourly = pd.DataFrame({
        "Open":  [94.0, 92.5],
        "High":  [93.5, 93.5],
        "Low":   [93.0, 92.0],
        "Close": [93.5, 92.5],
    }, index=pd.date_range(ts, periods=2, freq="1h", tz="UTC"))

    new_stop, hit = bb._hourly_trailing_stop_px(pos, ts, window, hourly, current_price)

    assert hit is None
    assert new_stop < 105.0   # straffer als der Anfangsstop
    assert new_stop > current_price   # Sicherheitsklammer: Stop nicht auf/unter dem Kurs


# --- exit_variant="chandelier_v2_2" (eingefrorene V2.2-Baseline, siehe
# EXIT_VARIANTS-Kommentar im Modul: core.bot_signals.trailing_stop_px() WAR
# diese Formel bis STRATEGY_VERSION "2.2-swing4h", ist es seit "2.3-swing4h"
# nicht mehr - diese Kopie bleibt unabhaengig davon korrekt) ---

def test_chandelier_stop_px_v2_2_none_below_activation_threshold():
    window = _flat_window()
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    assert bb._chandelier_stop_px_v2_2(pos, window, 104.0) is None   # R=0.8


def test_chandelier_stop_px_v2_2_matches_the_documented_formula_once_activated():
    window = _flat_window()   # window["High"].max() == 100.3, ATR% == _FLAT_ATR_PCT
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    current_price = 110.0   # R=(110-100)/5=2.0 -> aktiviert
    new_stop = bb._chandelier_stop_px_v2_2(pos, window, current_price)
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected = 100.3 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs
    assert new_stop == pytest.approx(expected)
    assert 95.0 < new_stop < current_price


def test_chandelier_stop_px_v2_2_ratchets_only_in_profit_direction():
    window = _flat_window()
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    current_price = 110.0
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    candidate = 100.3 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs   # ~98.32

    # Bereits guenstiger gesetzt (z.B. durch einen frueheren Takt) -> kein Rueckschritt.
    pos.stop_px = candidate + 1.0
    assert bb._chandelier_stop_px_v2_2(pos, window, current_price) is None

    # Noch beim Anfangsstop -> zieht auf den Kandidaten nach.
    pos.stop_px = 95.0
    new_stop = bb._chandelier_stop_px_v2_2(pos, window, current_price)
    assert new_stop == pytest.approx(candidate)


def test_chandelier_stop_px_v2_2_activation_is_independent_of_position_size():
    """Derselbe Einheitenfehler-Test wie bei den anderen beiden Varianten -
    siehe test_min_profit_stop_px_activation_is_independent_of_position_size
    fuer die Begruendung."""
    window = _flat_window()
    entry_px, stop_px = 100.0, 95.0
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        assert bb._chandelier_stop_px_v2_2(pos, window, 101.0) is None, \
            f"size={size} aktivierte verfrueht"
    current_price = 110.0
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected = 100.3 - bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs
    for size in (0.1, 1.0, 10.0):
        pos = _sized_long_pos(size, entry_px, stop_px)
        new_stop = bb._chandelier_stop_px_v2_2(pos, window, current_price)
        assert new_stop == pytest.approx(expected), f"size={size} ergab einen anderen Stop"


def test_chandelier_stop_px_v2_2_short_side():
    window = _flat_window()
    pos = bb._Position(symbol="BTC", side="short", size=1.0, entry_px=100.0,
                       stop_px=105.0, initial_stop_px=105.0,
                       opened_at=pd.Timestamp("2026-01-01", tz="UTC"),
                       initial_risk_usd=5.0)
    current_price = 90.0   # R=(90-100)*-1/5=2.0 -> aktiviert
    new_stop = bb._chandelier_stop_px_v2_2(pos, window, current_price)
    atr_abs = _FLAT_ATR_PCT / 100.0 * current_price
    expected = (100.0 * (1 - 0.003)) + bb.bot_signals.CHANDELIER_ATR_MULT * atr_abs   # window["Low"].min()
    assert new_stop == pytest.approx(expected)
    assert current_price < new_stop < 105.0


def test_run_backtest_chandelier_v2_2_variant_runs_end_to_end():
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0,
                             exit_variant="chandelier_v2_2")
    assert result.get("error") is None
    assert result["metrics"]["trades"] > 0


def test_run_backtest_hourly_trailing_variant_runs_end_to_end():
    """Verdrahtungs-Test - `_uptrend_market()` liefert kein `hourly`, die
    Verzweigung muss dann sauber auf die Referenzformel zurueckfallen statt
    zu crashen."""
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0,
                             exit_variant="hourly_trailing")
    assert result.get("error") is None
    assert result["metrics"]["trades"] > 0


def test_run_backtest_rejects_unknown_exit_variant():
    result = bb.run_backtest(_uptrend_market(), risk_level=5, exit_variant="erfunden")
    assert "error" in result
    assert "exit_variant" in result["error"]


def test_run_backtest_default_exit_variant_matches_explicit_reference():
    """Default aendert nichts am bestehenden Verhalten - dieselbe Zusicherung
    wie bei disabled_gates=frozenset()."""
    a = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)
    b = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0,
                        exit_variant="reference")
    assert a["metrics"] == b["metrics"]
    assert a["trades"] == b["trades"]


def test_run_backtest_min_profit_stop_variant_runs_end_to_end():
    """Reiner Verdrahtungs-Test (Feldzugriffe auf _Position, kein
    AttributeError) - die Rendite selbst haengt an der erfundenen Reihe,
    siehe Moduldoc."""
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0,
                             exit_variant="min_profit_stop")
    assert result.get("error") is None
    assert result["metrics"]["trades"] > 0


def test_flat_market_does_not_trade():
    """Trendfolge ohne Trend ist Muenzwurf mit Gebuehren - das Trend-Gate in
    bot_signals.evaluate() muss auch im Ruecktest greifen."""
    flat = _series(drift=0.0, noise=0.0008, seed=11)
    result = bb.run_backtest(_market(BTC=flat), risk_level=5)
    assert result.get("error") is None
    assert result["metrics"]["trades"] == 0


def test_no_lookahead_future_candles_cannot_change_past_trades():
    """DER Kerntest: die letzten Kerzen drastisch veraendern und pruefen, dass
    alle frueher GESCHLOSSENEN Trades unveraendert bleiben.

    Greift der Ruecktest versehentlich in die Zukunft (z.B. Indikatoren ueber
    die ganze Reihe statt bis t, oder Ausfuehrung zum Close statt zum Open der
    Entscheidungskerze), aendert dieser Eingriff die Vergangenheit - und der
    Test schlaegt fehl.
    """
    base = _uptrend_market()
    reference = bb.run_backtest(base, risk_level=5, starting_equity_usd=250.0)

    tampered = {s: df.copy() for s, df in base["candles"].items()}
    cut = tampered["BTC"].index[-60]
    for df in tampered.values():
        df.loc[cut:, ["Open", "High", "Low", "Close"]] *= 3.0
    # "btc_daily" MUSS mitgegeben werden - sonst faellt regime_at() im
    # manipulierten Lauf ueberall auf 'neutral' zurueck (Gate 1 blockt dann
    # ausnahmslos) und der Test vergleicht in Wahrheit "Trades" gegen "keine
    # Daten", nicht "Vergangenheit" gegen "manipulierte Zukunft".
    changed = bb.run_backtest(
        {"candles": tampered, "funding": {}, "missing_funding": [], "btc_daily": base["btc_daily"]},
        risk_level=5, starting_equity_usd=250.0)

    def before_cut(result):
        return [(t["symbol"], t["side"], t["opened_at"], t["closed_at"],
                 round(t["net_pnl_usd"], 9))
                for t in result["trades"] if t["closed_at"] < cut]

    assert before_cut(reference), "Testaufbau: vor dem Schnitt muss es Trades geben"
    assert before_cut(reference) == before_cut(changed)


def test_no_lookahead_a_mid_hold_stop_does_not_free_the_slot_within_the_same_candle(tmp_db):
    """Kritischer Fehler 2 (externe Pruefung, 13.09.2026): der Ruecktest
    pruefte Stop-Treffer frueher ueber das GANZE Kerzen-High/Low, BEVOR er
    ueber einen Einstieg entschied - ein Slot, der ERST SPAETER im
    Kerzenverlauf durch einen Stop frei wurde, stand der Einstiegs-
    entscheidung AM OPEN derselben Kerze schon zur Verfuegung (reproduziert
    im Review: identische Vergangenheit, nur ein spaeteres Tief aendert eine
    FRUEHERE Kaufentscheidung).

    Reproduktion: eine bereits offene Position wird KUENSTLICH mitten in
    ihrer Haltedauer ausgestoppt - nur das Low EINER Kerze faellt weit unter
    jeden plausiblen Stop, Open/High/Close und alle anderen Kerzen/Symbole
    bleiben unveraendert. `max_positions=1` macht den Slot-Engpass
    deterministisch: mit dem alten Fehler waere GENAU auf dieser Kerze ein
    Neueinstieg moeglich gewesen, weil der frei werdende Slot schon vor der
    eigenen Einstiegspruefung sichtbar war."""
    from core import bot_config
    bot_config.save_bot_limits(max_positions=1)
    market = _market(BTC=_series(n=3000, seed=1), S0=_series(n=3000, seed=2),
                     S1=_series(n=3000, seed=5), S2=_series(n=3000, seed=8))
    reference = bb.run_backtest(market, risk_level=9, starting_equity_usd=250.0)

    trades = sorted(reference["trades"], key=lambda t: t["opened_at"])
    candidate = None
    for t in trades:
        df = market["candles"][t["symbol"]]
        pos_open = df.index.get_loc(t["opened_at"])
        if pos_open + 1 < len(df.index) and df.index[pos_open + 1] < t["closed_at"]:
            candidate = (t, df.index[pos_open + 1])
            break
    assert candidate, ("Testaufbau: es muss einen Trade mit mindestens einer "
                       "Kerze Haltedauer vor dem eigentlichen Schluss geben")
    t0, ts_mid = candidate

    tampered = {s: df.copy() for s, df in market["candles"].items()}
    df0 = tampered[t0["symbol"]]
    # Nur das Low DIESER EINEN Kerze faellt drastisch - weit unter jeden
    # plausiblen Stop (hoechstens 30 % Abstand, core.bot_signals.
    # _STOP_PCT_CEILING). window_of() schneidet die LAUFENDE Kerze nie in
    # den Indikator-Ausschnitt FUER SICH SELBST ein (siehe dessen Docstring)
    # - die Manipulation wirkt also nur auf den Stop-Check dieser einen
    # Kerze, nicht auf die Einstiegsentscheidung irgendeines Symbols AN
    # dieser Kerze (die liest ausschliesslich VORHERIGE, abgeschlossene
    # Kerzen).
    df0.loc[ts_mid, "Low"] = df0.loc[ts_mid, "Open"] * 0.01

    changed = bb.run_backtest(
        {"candles": tampered, "funding": {}, "missing_funding": [], "btc_daily": market["btc_daily"]},
        risk_level=9, starting_equity_usd=250.0)

    changed_t0 = next(t for t in changed["trades"]
                      if t["symbol"] == t0["symbol"] and t["opened_at"] == t0["opened_at"])
    assert changed_t0["closed_at"] == ts_mid, \
        "Testaufbau: die Position muss durch die Manipulation jetzt auf dieser Kerze stoppen"

    same_candle_entries = [t for t in changed["trades"] if t["opened_at"] == ts_mid]
    assert same_candle_entries == [], (
        "Ein Slot, der erst WAEHREND dieser Kerze frei wird, darf nicht schon "
        "fuer einen Einstieg AM OPEN derselben Kerze zur Verfuegung stehen.")


def test_entry_sizing_uses_open_not_close_equity_of_the_same_bar(tmp_db):
    """EIN-BAR-LOOKAHEAD (Phase 1, Validierungs-Integritaet): ein Einstieg
    fuellt zum OPEN seiner Kerze, durfte vorher aber mit einer Kontostands-
    Bewertung zum CLOSE DERSELBEN Kerze bemessen werden - ein unrealisierter
    Gewinn/Verlust einer BEREITS offenen Position auf genau diesem Bar floss
    damit in die Groesse eines NEUEN Einstiegs ein, obwohl diese
    Kursbewegung zum Entscheidungszeitpunkt (Open) noch nicht stattgefunden
    hatte.

    Differenzieller Test nach demselben Muster wie
    test_no_lookahead_future_candles_cannot_change_past_trades: der CLOSE
    (nicht Open/High/Low) der BEREITS offenen Position wird auf genau dem
    Balken des zweiten Einstiegs drastisch veraendert - faellt die zweite
    Positionsgroesse trotzdem identisch aus, haengt sie nachweislich nicht
    mehr an dieser Close-Bewertung.

    `tmp_db` (isolierte DB) ist hier notwendig, nicht nur guter Stil:
    bot_backtest.run_backtest() liest ueber bot_config.limits_for_risk_level()
    gespeicherte Experten-Overrides aus core.db - ohne Isolation haette ein
    max_same_side_positions=1-Override auf DIESER Maschine echte Ueberlappung
    unmoeglich gemacht, ohne dass der Test das je gezeigt haette.
    Risikostufe 9 (statt 5) UND vier statt drei Symbole, weil die
    Standard-`_uptrend_market()` in der Praxis nie zwei Positionen
    gleichzeitig offen haelt (siehe Testaufbau-Recherche in Phase 1)."""
    market = _market(BTC=_series(n=3000, seed=1), S0=_series(n=3000, seed=2), S1=_series(n=3000, seed=5))
    reference = bb.run_backtest(market, risk_level=9, starting_equity_usd=250.0)

    trades = sorted(reference["trades"], key=lambda t: t["opened_at"])
    overlap_pair = None
    for t in trades:
        candidates = [o for o in trades if o is not t and o["opened_at"] <= t["opened_at"] < o["closed_at"]]
        if candidates:
            overlap_pair = (t, candidates[0])
            break
    assert overlap_pair, "Testaufbau: es muss zwei zeitlich ueberlappende Positionen geben"
    second, first = overlap_pair
    ts2, sym1, sym2 = second["opened_at"], first["symbol"], second["symbol"]

    tampered = {s: df.copy() for s, df in market["candles"].items()}
    df1 = tampered[sym1]
    row = df1.loc[ts2]
    inflated_close = row["Close"] * 3.0
    df1.loc[ts2, "Close"] = inflated_close
    df1.loc[ts2, "High"] = max(row["High"], inflated_close)

    changed = bb.run_backtest(
        {"candles": tampered, "funding": {}, "missing_funding": [], "btc_daily": market["btc_daily"]},
        risk_level=9, starting_equity_usd=250.0)
    changed_trades = sorted(changed["trades"], key=lambda t: t["opened_at"])
    changed_second = next(t for t in changed_trades
                          if t["symbol"] == sym2 and t["opened_at"] == ts2)

    assert changed_second["notional_usd"] == pytest.approx(second["notional_usd"])
    assert changed_second["size"] == pytest.approx(second["size"])


def test_fees_match_round_trip_taker_fee():
    """Gegenprobe der Kostenzeile: die Gebuehren muessen der Nominale mal
    TAKER_FEE auf BEIDEN Seiten entsprechen (Faktor ~2; deutlich darueber
    moeglich, weil die Schliessungs-Nominale bei MAX_HOLD_HOURS=336 (14 Tage,
    V2) im Aufwaertstrend um ein Vielfaches groesser sein kann als die
    Eroeffnungs-Nominale - anders als bei V1s kuerzerer Haltedauer)."""
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)
    opening = sum(t["notional_usd"] for t in result["trades"]) * TAKER_FEE
    assert opening > 0
    assert 1.9 < result["metrics"]["fees_usd"] / opening < 3.0


def test_slippage_is_charged_on_every_trade():
    """Ohne Slippage waere jedes Ergebnis zu optimistisch - sie muss sichtbar
    und positiv in der Kostenzeile stehen."""
    result = bb.run_backtest(_uptrend_market(), risk_level=5)
    assert result["metrics"]["slippage_usd"] > 0


def test_burn_in_is_enforced():
    """Zu kurze Historie -> gar kein Ergebnis statt eines Trades auf einem
    halb gefuellten Indikatorfenster."""
    short = _series(n=bb.BURN_IN_CANDLES - 10, seed=7)
    result = bb.run_backtest({"candles": {"BTC": short}, "funding": {}}, risk_level=5)
    assert "error" in result


def test_no_candles_reports_error_instead_of_empty_success():
    assert "error" in bb.run_backtest({"candles": {}, "funding": {}})


def test_missing_btc_daily_reports_error_instead_of_silent_zero_trades():
    """DIE Luecke, die Phase 1 (Validierungs-Integritaet) schliesst: ohne
    btc_daily liefert _daily_btc_regime_series() eine LEERE Reihe,
    regime_at() faellt fuer JEDEN Takt auf "neutral" zurueck, und mit
    ALLOW_SHORT=False blockt Gate 1 dann JEDEN Kandidaten - ein sauber
    formatiertes 0-Trade-Ergebnis, das von einem echten Nullbefund nicht zu
    unterscheiden war. Candles sind hier reichlich vorhanden (die Reihe
    selbst ist nicht das Problem), NUR btc_daily fehlt."""
    market = _uptrend_market()
    market["btc_daily"] = None

    result = bb.run_backtest(market, risk_level=5, starting_equity_usd=250.0)

    assert "error" in result
    assert "BTC-Tagesdaten" in result["error"]


def test_too_short_btc_daily_reports_error_instead_of_silent_zero_trades():
    """Derselbe Fall wie oben, aber mit einer VORHANDENEN, nur zu kurzen
    Reihe (z.B. ein abgebrochener Abruf) statt komplettem Fehlen."""
    market = _uptrend_market()
    from core import bot_signals
    too_short = _daily_btc(n_days=bot_signals._REGIME_MIN_CANDLES)  # genau 1 zu wenig
    market["btc_daily"] = too_short

    result = bb.run_backtest(market, risk_level=5, starting_equity_usd=250.0)

    assert "error" in result
    assert "BTC-Tagesdaten" in result["error"]


# --- _apply_funding(): tatsaechliche Stundenraten summieren statt samplen ---

def test_apply_funding_sums_actual_hourly_rates_within_the_window():
    """Bug (gefunden von einer externen Pruefung, 13.09.2026): die alte
    Fassung samplete EINE Stundenrate (series.asof(ts)) und multiplizierte
    sie mit 4 - das entspricht nicht der Summe der tatsaechlich angefallenen
    Stundenbuchungen. Vier unterschiedliche Stundenraten muessen sich zu
    ihrer TATSAECHLICHEN Summe addieren, nicht zur ersten mal 4."""
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    next_ts = ts + pd.Timedelta(hours=4)
    rates = pd.Series([0.0001, 0.0002, 0.0003, 0.0004],
                      index=pd.date_range(ts, periods=4, freq="1h", tz="UTC"))
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    state = bb._State(equity=250.0, cash=250.0, positions={"BTC": pos})

    bb._apply_funding(state, ts, next_ts, {"BTC": rates}, {"BTC": 100.0})

    expected_cost = float(rates.sum()) * 100.0 * pos.size   # long, positive Rate -> Kosten
    assert state.funding_usd == pytest.approx(expected_cost)
    wrong_old_approximation = float(rates.iloc[0]) * 4.0 * 100.0 * pos.size
    assert state.funding_usd != pytest.approx(wrong_old_approximation)


def test_apply_funding_only_sums_present_hours_not_missing_ones():
    """Fehlen einzelne Stunden innerhalb des Fensters, werden NUR die
    tatsaechlich vorhandenen summiert - nicht stillschweigend auf die volle
    Fensterlaenge hochgerechnet."""
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    next_ts = ts + pd.Timedelta(hours=4)
    # Nur 2 von 4 moeglichen Stunden vorhanden (Luecke bei Stunde 3 und 4).
    rates = pd.Series([0.0001, 0.0002], index=[ts, ts + pd.Timedelta(hours=1)])
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    state = bb._State(equity=250.0, cash=250.0, positions={"BTC": pos})

    bb._apply_funding(state, ts, next_ts, {"BTC": rates}, {"BTC": 100.0})

    expected_cost = (0.0001 + 0.0002) * 100.0 * pos.size
    assert state.funding_usd == pytest.approx(expected_cost)


def test_apply_funding_falls_back_to_the_sampled_rate_over_the_actual_window_length():
    """Faellt NICHTS in [ts, next_ts) - eine echte Datenluecke in den
    Fundingdaten selbst -, wird auf die letzte bekannte Rate zurueck-
    gefallen, hochgerechnet auf die TATSAECHLICHE Fensterlaenge (hier 4h,
    aus next_ts-ts), nicht hart auf 4 Stunden fixiert."""
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    next_ts = ts + pd.Timedelta(hours=4)
    rates = pd.Series([0.0001], index=[ts - pd.Timedelta(hours=10)])   # weit VOR dem Fenster
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    state = bb._State(equity=250.0, cash=250.0, positions={"BTC": pos})

    bb._apply_funding(state, ts, next_ts, {"BTC": rates}, {"BTC": 100.0})

    expected_cost = 0.0001 * 4.0 * 100.0 * pos.size
    assert state.funding_usd == pytest.approx(expected_cost)


def test_apply_funding_window_adapts_to_the_actual_next_decision_tick():
    """Kein hart codiertes ts+4h: bei einer Datenluecke (der naechste
    verfuegbare Takt folgt spaeter als 4h) wird das in der laengeren
    Zwischenzeit TATSAECHLICH angefallene Funding vollstaendig erfasst."""
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    next_ts = ts + pd.Timedelta(hours=7)   # laengerer Abstand als die ueblichen 4h
    rates = pd.Series([0.0001] * 7, index=pd.date_range(ts, periods=7, freq="1h", tz="UTC"))
    pos = _long_pos(entry_px=100.0, stop_px=95.0)
    state = bb._State(equity=250.0, cash=250.0, positions={"BTC": pos})

    bb._apply_funding(state, ts, next_ts, {"BTC": rates}, {"BTC": 100.0})

    expected_cost = 0.0007 * 100.0 * pos.size   # alle 7 Stunden, nicht nur die ersten 4
    assert state.funding_usd == pytest.approx(expected_cost)


def test_funding_history_is_charged_to_longs():
    """Positives Funding ist fuer Longs eine Kostenposition. Mit Fundingdaten
    muss das Endergebnis schlechter sein als ohne - sonst wird der groesste
    Kostenblock gehebelter Dauerpositionen stillschweigend unterschlagen."""
    market = _uptrend_market()
    without = bb.run_backtest(market, risk_level=5, starting_equity_usd=250.0)

    index = market["candles"]["BTC"].index
    # Deutlich positiv, aber unter core.bot_signals.FUNDING_BUDGET_HOURLY
    # (0.00005) - core.bot_guards.check_leverage prueft das Kostenbudget
    # seit der Korrektur (13.09.2026) bei JEDEM Hebel, nicht nur >1x; eine
    # zu teure Rate wuerde hier jede Order blockieren, bevor ueberhaupt
    # Funding anfallen koennte (siehe test_leverage_1x_still_checks_
    # funding_budget_when_rate_is_known in tests/test_bot_guards.py).
    # Ueber die volle Haltedauer (bis zu core.bot_signals.MAX_HOLD_HOURS)
    # summiert sich selbst dieser kleine Satz zu einem klar messbaren,
    # positiven Betrag.
    rate = pd.Series(0.00003, index=index)
    with_funding = bb.run_backtest(
        {"candles": market["candles"], "funding": {s: rate for s in market["candles"]},
         "missing_funding": [], "btc_daily": market["btc_daily"]},
        risk_level=5, starting_equity_usd=250.0)

    assert with_funding["metrics"]["funding_usd"] > 0
    assert with_funding["metrics"]["end_usd"] < without["metrics"]["end_usd"]


def test_run_backtest_respects_stored_expert_overrides(tmp_db):
    """Phase 6: run_backtest() muss dieselben gespeicherten Experten-
    Overrides sehen wie der Live-Pfad (core.bot_config.limits_for_risk_level,
    ersetzt das fruehere derive_limits() allein) - sonst testet der
    Rücktest eine andere Konfiguration als die, die live tatsaechlich gilt."""
    from core import bot_config
    bot_config.save_bot_limits(max_positions=1)
    result = bb.run_backtest(_uptrend_market(), risk_level=8, starting_equity_usd=250.0)
    assert result.get("error") is None
    assert result["limits"]["max_positions"] == 1
    assert result["overridden_limit_keys"] == ["max_positions"]


def test_benchmarks_are_reported():
    result = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)
    benchmarks = result["benchmarks"]
    assert benchmarks["btc_buy_hold_usd"] > 0
    assert benchmarks["btc_ma50_trend_usd"] > 0
    assert benchmarks["bot_usd"] == pytest.approx(result["metrics"]["end_usd"])


def test_open_positions_are_closed_at_the_end():
    """Sonst zaehlt ein zufaellig offener Gewinner voll und ein Verlierer gar
    nicht - der haeufigste Weg, einen Ruecktest unbemerkt zu schoenen."""
    result = bb.run_backtest(_uptrend_market(), risk_level=5)
    assert all(t["closed_at"] is not None for t in result["trades"])
    ends = [t for t in result["trades"] if t["reason"] == "backtest_ende"]
    assert len(ends) <= 5      # hoechstens die gleichzeitig offenen Positionen


def test_position_limit_is_respected():
    """Der Ruecktest laeuft durch die ECHTEN Guards - mehr gleichzeitige
    Positionen als max_positions darf es daher nie geben."""
    from core import bot_config
    limits = bot_config.derive_limits(5)
    market = _market(**{f"S{i}": _series(seed=20 + i) for i in range(8)})
    result = bb.run_backtest(market, risk_level=5, starting_equity_usd=250.0)

    events = []
    for t in result["trades"]:
        events.append((t["opened_at"], 1))
        events.append((t["closed_at"], -1))
    events.sort()
    open_count = peak = 0
    for _, delta in events:
        open_count += delta
        peak = max(peak, open_count)
    assert peak <= limits["max_positions"]


# --- load_market_data() (Phase 1, Validierungs-Integritaet: bisher ohne
# jede Testabdeckung, obwohl JEDES Rücktest-/Walk-Forward-Ergebnis auf ihr
# ruht - ein stiller Datenausfall hier sah vorher aus wie ein sauberes
# 0-Trade-Ergebnis, siehe test_missing_btc_daily_reports_error_instead_of_
# silent_zero_trades oben) ---

def _ok_4h(symbol, start_ms, end_ms, interval):
    assert interval == "4h"
    return _series(n=bb.BURN_IN_CANDLES + 24 + 10, seed=1)


def _ok_1h(symbol, start_ms, end_ms, interval):
    assert interval == "1h"
    return _series(n=50, seed=2)


def _ok_both_intervals(symbol, start_ms, end_ms, interval):
    """4h- UND 1h-Antworten fuer Tests, die nur eine ANDERE Abruf-Stufe
    (Funding, Tagesdaten) isoliert pruefen wollen und beide Kerzen-Intervalle
    nur "erfolgreich durchlaufen" muessen."""
    if interval == "1h":
        return _ok_1h(symbol, start_ms, end_ms, interval)
    return _ok_4h(symbol, start_ms, end_ms, interval)


def _ok_funding(symbol, start_ms, end_ms):
    return pd.Series(0.0001, index=_series(n=10, seed=3).index)


def test_load_market_data_skips_symbol_on_4h_fetch_exception():
    def candles_fn(symbol, start_ms, end_ms, interval):
        if interval == "4h":
            raise RuntimeError("API down")
        return _ok_1h(symbol, start_ms, end_ms, interval)

    market = bb.load_market_data(["BTC"], days=30, candles_fn=candles_fn,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=_ok_funding)

    assert market["candles"] == {}
    report = market["load_report"]
    assert "BTC" in report["skipped_symbols"]
    assert "4h-Abruf fehlgeschlagen" in report["skipped_symbols"]["BTC"]
    assert report["loaded_symbols"] == 0
    assert report["requested_symbols"] == 1


def test_load_market_data_skips_symbol_with_too_few_4h_candles():
    def candles_fn(symbol, start_ms, end_ms, interval):
        if interval == "4h":
            return _series(n=bb.BURN_IN_CANDLES, seed=1)  # zu kurz
        return _ok_1h(symbol, start_ms, end_ms, interval)

    market = bb.load_market_data(["BTC"], days=30, candles_fn=candles_fn,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=_ok_funding)

    assert market["candles"] == {}
    assert market["load_report"]["skipped_symbols"]["BTC"] == "zu wenige 4h-Kerzen"


def test_load_market_data_funding_failure_is_not_fatal_for_the_symbol():
    def funding_fn(symbol, start_ms, end_ms):
        raise RuntimeError("funding API down")

    market = bb.load_market_data(["BTC"], days=30, candles_fn=_ok_both_intervals,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=funding_fn)

    assert "BTC" in market["candles"]           # Symbol bleibt nutzbar
    assert "BTC" in market["missing_funding"]
    assert "BTC" not in market["funding"]
    assert "Funding-Abruf fehlgeschlagen" in market["load_report"]["fetch_notes"]["BTC"]


def test_load_market_data_empty_funding_series_counts_as_missing():
    def funding_fn(symbol, start_ms, end_ms):
        return pd.Series(dtype=float)   # leer, aber kein Fehler

    market = bb.load_market_data(["BTC"], days=30, candles_fn=_ok_both_intervals,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=funding_fn)

    assert "BTC" in market["missing_funding"]
    assert "BTC" not in market["funding"]
    # Kein Fehler mitgeloggt - eine leere Antwort ist kein Abruf-Fehlschlag.
    assert "BTC" not in market["load_report"]["fetch_notes"]


def test_load_market_data_1h_failure_is_not_fatal_and_omits_symbol_from_hourly():
    def candles_fn(symbol, start_ms, end_ms, interval):
        if interval == "1h":
            raise RuntimeError("1h API down")
        return _ok_4h(symbol, start_ms, end_ms, interval)

    market = bb.load_market_data(["BTC"], days=30, candles_fn=candles_fn,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=_ok_funding)

    assert "BTC" in market["candles"]            # 4h-Symbol bleibt nutzbar
    assert "BTC" not in market["hourly"]          # nur der Feinschliff fehlt
    assert "1h-Abruf fehlgeschlagen" in market["load_report"]["fetch_notes"]["BTC"]


def test_load_market_data_btc_daily_failure_is_reported_not_swallowed():
    def daily_fn(symbol, start_ms, end_ms, interval):
        raise RuntimeError("daily API down")

    market = bb.load_market_data(["BTC"], days=30, candles_fn=_ok_both_intervals,
                                 daily_fn=daily_fn, funding_fn=_ok_funding)

    assert market["btc_daily"] is None
    assert "Tagesdaten-Abruf fehlgeschlagen" in market["load_report"]["fetch_notes"]["BTC"]
    assert market["load_report"]["btc_daily_candles"] == 0


def test_load_market_data_candle_completeness_reports_ratio_to_expected():
    """6 4h-Kerzen/Tag ist der theoretische Idealwert - eine kuenstlich auf
    die Haelfte gekuerzte Antwort muss das im Report sichtbar machen, sonst
    ist ein abgebrochener Abruf nicht von einer vollstaendigen Historie zu
    unterscheiden (CLAUDE.md: "schwankende Vollstaendigkeit der Kerzen-
    historie")."""
    days = 90   # grosszuegig bemessen, damit "die Haelfte" (270) komfortabel
                # ueber dem Mindestwert BURN_IN_CANDLES+24 (115) liegt - sonst
                # wuerde die erzwungene Kuerzung von der Mindestlaenge selbst
                # verdeckt, siehe die Regression, die genau das aufgedeckt hat.
    full_len = days * 6

    def candles_fn(symbol, start_ms, end_ms, interval):
        if interval == "4h":
            return _series(n=full_len // 2, seed=1)
        return _ok_1h(symbol, start_ms, end_ms, interval)

    market = bb.load_market_data(["BTC"], days=days, candles_fn=candles_fn,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=_ok_funding)

    completeness = market["load_report"]["candle_completeness"]["BTC"]
    assert completeness["expected"] == full_len
    assert completeness["candles"] == len(market["candles"]["BTC"])
    assert completeness["completeness_pct"] == pytest.approx(
        completeness["candles"] / full_len * 100, abs=0.1)
    assert completeness["completeness_pct"] < 60.0   # deutlich unter voll


def test_load_market_data_progress_callback_receives_stage_events():
    events = []
    market = bb.load_market_data(
        ["BTC", "ETH"], days=30, candles_fn=_ok_both_intervals,
        daily_fn=lambda *a, **k: _daily_btc(), funding_fn=_ok_funding,
        progress_fn=lambda stage, completed, total, detail: events.append(stage))

    assert "Lade 4h-Kerzen" in events
    assert "Lade Funding-Historie" in events
    assert "Lade 1h-Kerzen" in events
    assert "Lade BTC-Tagesdaten" in events
    assert "Marktdaten geladen" in events
    assert len(market["candles"]) == 2


def test_load_market_data_progress_callback_exception_does_not_abort_load():
    """Eine Anzeige darf den fachlichen Datenabruf nie zum Absturz bringen
    (Docstring von _progress in load_market_data)."""
    def bad_progress(stage, completed, total, detail):
        raise RuntimeError("UI ist weg")

    market = bb.load_market_data(["BTC"], days=30, candles_fn=_ok_both_intervals,
                                 daily_fn=lambda *a, **k: _daily_btc(),
                                 funding_fn=_ok_funding, progress_fn=bad_progress)

    assert "BTC" in market["candles"]


# --- Wiedereinstiegssperre (externe Pruefung 23.09.2026) ---

def _sig(side="long", tradeable=True, readings=True):
    from types import SimpleNamespace
    return SimpleNamespace(side=side, tradeable=tradeable, readings={"x": 1} if readings else {})


def test_reentry_block_reason_cases():
    from datetime import datetime, timedelta, timezone
    from core.bot import reentry_block_reason
    closed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = closed + timedelta(hours=8)

    assert "abwarten" in reentry_block_reason(closed, "long", _sig(), closed + timedelta(hours=1))
    assert "weiterhin aktiv" in reentry_block_reason(closed, "long", _sig(), later)
    assert reentry_block_reason(closed, "long", _sig(tradeable=False), later) is None
    assert reentry_block_reason(closed, "long", _sig(side="short"), later) is None
    # blosser Datenfehler ist KEIN Nachweis, dass das Setup weg ist
    assert "ohne auswertbare" in reentry_block_reason(
        closed, "long", _sig(side=None, tradeable=False, readings=False), later)


def test_backtest_applies_reentry_lock_like_live(monkeypatch):
    import core.bot as core_bot
    baseline = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)
    monkeypatch.setattr(core_bot, "reentry_block_reason",
                        lambda *a, **k: "gesperrt (Test)")
    locked = bb.run_backtest(_uptrend_market(), risk_level=5, starting_equity_usd=250.0)

    per_symbol = {}
    for t in locked["trades"]:
        per_symbol[t["symbol"]] = per_symbol.get(t["symbol"], 0) + 1
    # Nach dem ersten Schluss bleibt jedes Symbol gesperrt.
    assert all(n == 1 for n in per_symbol.values())
    assert locked["blocked_reasons"].get("Signal-Reset (Wiedereinstiegssperre)", 0) > 0
    assert baseline["metrics"]["trades"] >= locked["metrics"]["trades"]
