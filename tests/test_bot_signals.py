"""core/bot_signals.py: die deterministische Entscheidungsebene des Bots (V2,
4h-Swing mit Tagesregime).

Alle Kursreihen sind synthetisch - kein Netz, keine DB (das Modul ist bewusst
frei von beidem, wie core/bot_guards.py). Geprueft wird vor allem, dass die
PFLICHTBEDINGUNGEN (Gates) unabhaengig voneinander greifen: ein Kandidat, der
alle bis auf EIN Gate erfuellt, muss genau an DIESEM Gate blockieren, nicht
an einem anderen zufaellig mitbetroffenen. Die Testreihen dafuer sind bewusst
gegen core.bot_signals selbst kalibriert (siehe Kommentare an den jeweiligen
Tests), nicht gegen eine angenommene Formel - Kalibrierungswerte wie
MIN_24H_VOLUME_USD aendern sich mit dem Ruecktest, ohne dass diese Tests
ihre Aussagekraft verlieren sollen.
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from core import bot_config, bot_signals, clock


def _candles(closes, wick_pct=None, freq="4h", start="2026-01-01") -> pd.DataFrame:
    """OHLCV aus einer Schlusskursreihe, tz-aware UTC wie data.hyperliquid.
    candles_df() es seit core.clock liefert. High/Low sind Pflicht, sonst
    kann analysis.technical.add_indicators() kein ATR rechnen.

    Die Dochte skalieren mit der TATSAECHLICHEN Bewegung der Reihe. Feste
    Prozentwerte haetten jeder Reihe dieselbe ATR aufgezwungen - ein voellig
    ruhiger Markt haette dann dieselbe Volatilitaet gemeldet wie ein wilder,
    und der ATR-Ausschluss in evaluate() waere nie gepruefte Theorie geblieben.
    """
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    if wick_pct is None:
        moves = np.abs(np.diff(closes, prepend=closes[0])) / closes * 100
        wick_pct = max(0.01, float(moves.mean()) * 0.6)
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    body_high = np.maximum(opens, closes)
    body_low = np.minimum(opens, closes)
    return pd.DataFrame({
        "Open": opens,
        "High": body_high * (1 + wick_pct / 100),
        "Low": body_low * (1 - wick_pct / 100),
        "Close": closes,
        "Volume": np.full(len(closes), 1000.0),
    }, index=index)


def _trend(n=160, start=100.0, per_candle_pct=0.5, noise=0.05, seed=7,
          wick_pct=None) -> pd.DataFrame:
    """Stetiger 4h-Trend mit leichtem Rauschen - erfuellt bei den
    Default-Parametern ALLE Gates (Effizienz, Momentum/ATR, ATR-Band) mit
    komfortablem Abstand (siehe test_trend_fixture_clears_every_gate).
    `per_candle_pct` negativ = abwaerts."""
    rng = np.random.default_rng(seed)
    steps = per_candle_pct / 100 + rng.normal(0, noise / 100, n)
    return _candles(start * np.cumprod(1 + steps), wick_pct=wick_pct)


def _daily_candles(closes, start="2025-09-01") -> pd.DataFrame:
    """Tageskerzen fuer regime()-Tests."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    index = pd.date_range(start, periods=len(closes), freq="1d", tz="UTC")
    return pd.DataFrame({
        "Open": opens, "High": np.maximum(opens, closes) * 1.01,
        "Low": np.minimum(opens, closes) * 0.99, "Close": closes,
        "Volume": np.full(len(closes), 1000.0),
    }, index=index)


# Risiko-Limits (vom Regler, hier Stufe 5) + feste Strategie-Konstanten
# zusammengemischt, wie core.bot_config.bot_limits() es auch tut - beides
# sind reine Funktionen ohne DB-Zugriff, deshalb hier auf Modulebene
# unbedenklich (bot_limits() selbst braucht risk_level()/db.get_meta() und
# damit die tmp_db-Fixture, die zum Importzeitpunkt noch nicht existiert).
LIMITS = {**bot_config.derive_limits(5), **bot_config.strategy_constants()}


# --- Marktregime (Tageskerzen BTC) ---

def test_regime_long_when_price_and_ma50_rise():
    df = _daily_candles(100.0 * (1.01 ** np.arange(120)))
    assert bot_signals.regime(df) == "long"


def test_regime_short_when_price_and_ma50_fall():
    df = _daily_candles(100.0 * (0.99 ** np.arange(120)))
    assert bot_signals.regime(df) == "short"


def test_regime_neutral_when_flat():
    df = _daily_candles(np.full(120, 100.0))
    assert bot_signals.regime(df) == "neutral"


def test_regime_neutral_with_too_little_history():
    df = _daily_candles(100.0 * (1.01 ** np.arange(30)))
    assert bot_signals.regime(df) == "neutral"


def test_regime_neutral_without_data():
    assert bot_signals.regime(None) == "neutral"


# --- Kernfrage: erfuellt ein sauberer Trend ALLE Pflichtbedingungen? ---

def test_trend_fixture_clears_every_gate():
    """Die Standard-Testreihe (_trend()) muss ALLE sieben Gates klar
    erfuellen - sonst ist jeder Test, der sie als "handelbaren Kandidaten"
    voraussetzt, auf Treibsand gebaut."""
    df = _trend()
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, funding_hourly=0.0, limits=LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is True, signal.blocked_by
    assert signal.side == "long"


def test_downtrend_is_tradeable_short_when_shorts_allowed():
    df = _trend(per_candle_pct=-0.5)
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("ETH", df, price, funding_hourly=0.0,
                                  limits=dict(LIMITS), btc_regime="short",
                                  btc_roc=0.0, volume_24h_usd=1e9)
    # ALLOW_SHORT ist False (Startprofil) - auch ein sauberer Downtrend im
    # Short-Regime bleibt deshalb blockiert, nicht handelbar.
    assert signal.tradeable is False
    assert "Short" in signal.blocked_by


# --- Gate 1: Tagesregime ---

def test_gate1_blocks_when_regime_neutral():
    df = _trend()
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="neutral", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is False
    assert "Tagesregime" in signal.blocked_by


def test_gate1_blocks_shorts_via_allow_short_flag():
    df = _trend(per_candle_pct=-0.5)
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="short", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is False
    assert "ALLOW_SHORT" in signal.blocked_by


# --- Gate 2: Kurs/MA20/MA50-Struktur ---

def test_gate2_blocks_when_ma_structure_disagrees():
    """Ein reiner Zickzack ohne Nettobewegung haelt weder price>MA50 noch
    MA20>MA50 zuverlaessig ein."""
    n = 160
    zigzag = _candles(100.0 + 3.0 * np.sin(np.arange(n) * 2 * np.pi / 4), wick_pct=0.3)
    price = float(zigzag["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", zigzag, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is False
    assert "Gate 2" in signal.blocked_by


# Gates 3 (Trend-Effizienz/Kaufman ER), 4 (Momentum vs. ATR) und 6 (Volumen)
# sind seit STRATEGY_VERSION "2.2-swing4h" ENTFERNT (Phase 3/4, Gate-
# Ablation - core/bot_signals.py's Konstanten-Block "PFLICHTBEDINGUNGEN"
# erklaert das Warum): eine Out-of-Sample-Messung zeigte, dass ihr Entfernen
# den Erwartungswert nicht messbar verschlechtert. Ihre Tests (inkl.
# test_efficiency_ratio_via_readings_straight_line_vs_oscillation, das
# readings()["efficiency"] pruefte) sind ersatzlos entfallen, nicht nur
# deaktiviert - die Felder/Konstanten, die sie prueften, existieren nicht
# mehr. Gate 5 (relative Staerke zu BTC) ist aus demselben Grund entfallen,
# ihre Tests standen vorher hier.


# --- Gate 7: Liquiditaet (24h-Volumen) ---

def test_gate7_blocks_thin_markets():
    df = _trend()
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=100.0)
    assert signal.tradeable is False
    assert "Gate 7" in signal.blocked_by


def test_gate7_blocks_on_unknown_liquidity():
    """Fehlende Daten sind kein Freibrief - Ohne-Daten-ist-auch-eine-Antwort-
    Prinzip wie ueberall sonst in diesem Modul."""
    df = _trend()
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=None)
    assert signal.tradeable is False
    assert "Gate 7" in signal.blocked_by


# --- disabled_gates (Phase 3, Gate-Ablation): jedes Gate einzeln abschaltbar,
# Default aendert nichts, ein abgeschaltetes Gate faellt durch zum naechsten ---

def test_disabled_gates_default_changes_nothing():
    """Der Live-Pfad ruft evaluate() nie mit einem nicht-leeren Wert auf -
    das MUSS mit dem Verhalten VOR der Ablations-Erweiterung identisch
    bleiben."""
    df = _trend()
    price = float(df["Close"].iloc[-1])
    baseline = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                    btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    explicit_empty = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                          btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9,
                                          disabled_gates=frozenset())
    assert baseline.tradeable == explicit_empty.tradeable
    assert baseline.blocked_by == explicit_empty.blocked_by
    assert baseline.score == explicit_empty.score


def test_disabled_gate2_lets_a_gate2_blocked_signal_through():
    n = 160
    zigzag = _candles(100.0 + 3.0 * np.sin(np.arange(n) * 2 * np.pi / 4), wick_pct=0.3)
    price = float(zigzag["Close"].iloc[-1])
    blocked = bot_signals.evaluate("BTC", zigzag, price, 0.0, LIMITS,
                                   btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert "Gate 2" in blocked.blocked_by

    signal = bot_signals.evaluate("BTC", zigzag, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9,
                                  disabled_gates=frozenset({"gate2"}))
    # Seit der Entfernung von Gate 3/4/5/6 ist Gate 7 (Liquiditaet, hier mit
    # 1e9 $ erfuellt) das EINZIGE verbleibende Gate nach Gate 2 - mit Gate 2
    # abgeschaltet und Gate 7 erfuellt ist der Kandidat tatsaechlich
    # handelbar, nicht nur "irgendein anderes Gate blockt nicht mehr unter
    # dem Namen Gate 2".
    assert signal.tradeable is True


def test_disabled_gate7_lets_a_gate7_blocked_signal_through():
    df = _trend()
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=100.0,
                                  disabled_gates=frozenset({"gate7"}))
    assert signal.tradeable is True   # hier ist Gate 7 tatsaechlich das einzige Hindernis
    assert signal.blocked_by is None


def test_disabling_one_gate_leaves_other_gates_enforced():
    """Ablation heisst EIN Gate entfernen, Rest unveraendert - ein Zickzack
    (verletzt Gate 2) mit gleichzeitig duennem Markt (verletzt Gate 7) darf
    beim Abschalten von NUR Gate 2 weiterhin an Gate 7 scheitern."""
    n = 160
    zigzag = _candles(100.0 + 3.0 * np.sin(np.arange(n) * 2 * np.pi / 4), wick_pct=0.3)
    price = float(zigzag["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", zigzag, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=100.0,
                                  disabled_gates=frozenset({"gate2"}))
    assert signal.tradeable is False
    assert "Gate 7" in signal.blocked_by   # das naechste Gate greift weiterhin


# --- ATR-Band (weiterhin ein Ausschlusskriterium, keine Gate-Nummer) ---

def test_dead_market_is_blocked_by_atr_floor():
    df = _candles(np.full(160, 100.0), wick_pct=0.01)
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is False
    assert "ATR" in signal.blocked_by


def test_wild_market_is_blocked_by_atr_ceiling():
    df = _trend(wick_pct=15.0)
    price = float(df["Close"].iloc[-1])
    signal = bot_signals.evaluate("BTC", df, price, 0.0, LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.tradeable is False
    assert "ATR" in signal.blocked_by


def test_overbought_uptrend_still_scores_well():
    """Ein starker Aufwaertstrend hat einen hohen RSI. Der alte Dip-Kauf-
    Score hat genau deshalb abgelehnt ('RSI 79, ueberkauft') - hier muss er
    trotzdem gut ranken (Score ist Rangfolge, keine Schwelle mehr)."""
    df = _trend(per_candle_pct=0.25, noise=0.02)
    ind = bot_signals.prepare(df)
    readings = bot_signals.readings(ind)
    assert readings["rsi"] > 70, "Testaufbau: Reihe soll ueberkauft sein"
    score, _, _ = bot_signals.score_side(readings, "long", funding_hourly=0.0)
    assert score >= 55


# --- Datenlage ---

def test_too_few_candles_is_reported_not_silently_skipped():
    signal = bot_signals.evaluate("BTC", _trend(n=30), price=100.0, funding_hourly=0.0,
                                  limits=LIMITS, btc_regime="long", btc_roc=0.0,
                                  volume_24h_usd=1e9)
    assert signal.blocked_by is not None
    assert signal.tradeable is False


def test_missing_candles_do_not_raise():
    signal = bot_signals.evaluate("BTC", None, price=100.0, funding_hourly=0.0, limits=LIMITS,
                                  btc_regime="long", btc_roc=0.0, volume_24h_usd=1e9)
    assert signal.blocked_by is not None


def test_forming_candle_is_dropped_before_scoring():
    """Sonst haetten zwei Ticks desselben 4h-Fensters unterschiedliche
    Signale."""
    df = _trend()
    prepared = bot_signals.prepare(df)
    assert len(prepared) == len(df) - 1
    assert prepared.index[-1] == df.index[-2]


# --- Funding-Asymmetrie: der Kern des alten Guard-Fehlers ---

def test_positive_funding_helps_shorts_and_hurts_longs():
    df = _trend()
    readings = bot_signals.readings(bot_signals.prepare(df))
    long_free, _, _ = bot_signals.score_side(readings, "long", funding_hourly=0.0)
    long_paid, _, _ = bot_signals.score_side(readings, "long", funding_hourly=0.0002)
    short_free, _, _ = bot_signals.score_side(readings, "short", funding_hourly=0.0)
    short_paid, _, _ = bot_signals.score_side(readings, "short", funding_hourly=0.0002)
    assert long_paid < long_free
    assert short_paid > short_free


# --- Stop-Abstand ---

def test_stop_distance_scales_with_volatility():
    calm = bot_signals.stop_distance_pct(0.7, LIMITS)
    wild = bot_signals.stop_distance_pct(3.0, LIMITS)
    assert wild > calm


def test_stop_distance_is_risk_level_independent():
    """INITIAL_STOP_ATR_MULT ist eine feste Strategie-Konstante
    (core/bot_signals.py) - der Risikoregler darf den Stop-Abstand nicht mehr
    aendern, nur noch Positionsgroesse/Hebel/Exposure."""
    low = {**bot_config.derive_limits(1), **bot_config.strategy_constants()}
    high = {**bot_config.derive_limits(10), **bot_config.strategy_constants()}
    assert (bot_signals.stop_distance_pct(2.0, low)
            == bot_signals.stop_distance_pct(2.0, high))


def test_stop_distance_has_a_floor_above_round_trip_costs():
    """Ein Stop dichter als die Handelskosten waere ein garantierter
    Verlust - Boden ist ein Vielfaches der geschaetzten Round-Trip-Kosten."""
    floor = bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE * bot_signals._STOP_PCT_FLOOR_MULT
    assert bot_signals.stop_distance_pct(0.001, LIMITS) == pytest.approx(floor)


def test_stop_distance_has_a_ceiling():
    assert bot_signals.stop_distance_pct(1000.0, LIMITS) == bot_signals._STOP_PCT_CEILING


def test_stop_distance_without_atr_is_none():
    assert bot_signals.stop_distance_pct(None, LIMITS) is None


# --- Positionsgroesse ---

def test_position_size_refuses_below_exchange_minimum():
    small_limits = {**bot_config.derive_limits(1), **bot_config.strategy_constants()}
    size, reason = bot_signals.position_size_usd(44.57, small_limits, min_notional_usd=10.0)
    assert size is None
    assert "Mindestordergröße" in reason


def test_position_size_uses_max_position_pct():
    size, reason = bot_signals.position_size_usd(200.0, dict(LIMITS, max_position_pct=25.0),
                                                 min_notional_usd=10.0)
    assert size == 50.0
    assert reason is None


def test_position_size_respects_first_live_order_cap():
    size, _ = bot_signals.position_size_usd(200.0, dict(LIMITS, max_position_pct=25.0),
                                            min_notional_usd=10.0, cap_usd=12.0)
    assert size == 12.0


# --- Positionsgroesse: Risikobudget statt fester Equity-Prozentsatz ---
# (Nominale = Equity * risk_per_trade_pct / stop_distance_pct, gedeckelt
# durch max_position_pct als Konzentrationsgrenze)

def test_position_size_uses_risk_budget_when_stop_given():
    limits = dict(LIMITS, max_position_pct=90.0, risk_per_trade_pct=1.0)
    size, reason = bot_signals.position_size_usd(200.0, limits, min_notional_usd=10.0,
                                                 stop_distance_pct=2.0)
    assert size == pytest.approx(100.0)  # 200 * 1.0 / 2.0
    assert reason is None


def test_position_size_doubling_stop_distance_halves_the_size():
    limits = dict(LIMITS, max_position_pct=90.0, risk_per_trade_pct=1.0)
    size, _ = bot_signals.position_size_usd(200.0, limits, min_notional_usd=10.0,
                                            stop_distance_pct=4.0)
    assert size == pytest.approx(50.0)  # doppelter Stop-Abstand -> halbe Nominale


def test_position_size_concentration_cap_wins_over_risk_budget():
    """Bei einem sehr engen Stop waere das Risikobudget allein eine
    unplausibel grosse Position - max_position_pct bleibt die harte
    Konzentrationsgrenze."""
    limits = dict(LIMITS, max_position_pct=10.0, risk_per_trade_pct=5.0)
    size, _ = bot_signals.position_size_usd(200.0, limits, min_notional_usd=10.0,
                                            stop_distance_pct=1.0)
    assert size == pytest.approx(20.0)  # 200 * 10% (Konzentrationsgrenze), nicht 1000


def test_position_size_risk_budget_below_minimum_reports_reason():
    limits = dict(LIMITS, max_position_pct=90.0, risk_per_trade_pct=0.5)
    size, reason = bot_signals.position_size_usd(50.0, limits, min_notional_usd=10.0,
                                                  stop_distance_pct=5.0)
    assert size is None  # 50 * 0.5 / 5.0 = 5.0 < 10.0
    assert "Mindestordergröße" in reason


def test_position_size_raises_hard_error_without_risk_per_trade_pct():
    """Kein stiller Rueckgriff auf max_position_pct mehr (haette bei
    Risikostufe 9 einen 23-fachen Groessenfehler ergeben koennen)."""
    limits = dict(LIMITS, risk_per_trade_pct=0.0)
    with pytest.raises(ValueError):
        bot_signals.position_size_usd(200.0, limits, min_notional_usd=10.0,
                                      stop_distance_pct=2.0)


# --- Ausstiege ---

def test_exit_on_trend_reversal_for_long():
    position = {"side": "long", "opened_at": clock.iso_utc()}
    reason = bot_signals.exit_signal(position, _trend(per_candle_pct=-0.5), LIMITS,
                                     btc_regime="long")
    assert reason is not None
    assert "Trend" in reason


def test_no_exit_while_trend_and_regime_intact():
    position = {"side": "long", "opened_at": clock.iso_utc()}
    assert bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="long") is None


def test_exit_on_regime_change():
    """Ein gegenteiliges Regime schliesst, neutral ist die Halte-Hysterese."""
    position = {"side": "long", "opened_at": clock.iso_utc()}
    reason = bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="short")
    assert reason is not None
    assert "Tagesregime" in reason
    reason_neutral = bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="neutral")
    assert reason_neutral is None


def test_exit_ignores_regime_when_not_supplied():
    """Rueckwaertskompatibel: ohne btc_regime (z.B. ein Aufrufer, der es
    nicht kennt) greift nur noch Zeitablauf/MA-Kreuz, kein Regime-Exit."""
    position = {"side": "long", "opened_at": clock.iso_utc()}
    assert bot_signals.exit_signal(position, _trend(), LIMITS) is None


def test_time_stop_closes_a_stale_position():
    opened = clock.iso_utc(clock.now_utc() - timedelta(hours=bot_signals.MAX_HOLD_HOURS + 10))
    position = {"side": "long", "opened_at": opened}
    reason = bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="long")
    assert reason is not None
    assert "Haltedauer" in reason


def test_broken_opened_at_does_not_raise():
    position = {"side": "long", "opened_at": "irgendwann"}
    assert bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="long") is None


def test_time_stop_prefers_exchange_opened_at_over_opened_at():
    """Eine uebernommene Position: opened_at ist der UEBERNAHME-Zeitpunkt
    (frisch), exchange_opened_at die tatsaechliche, laengst ueberfaellige
    Eroeffnung auf der Boerse. Ohne den Vorrang bekaeme die Position beim
    Uebernehmen eine neue Uhr, obwohl sie laengst haette auslaufen sollen."""
    stale = clock.iso_utc(clock.now_utc() - timedelta(hours=bot_signals.MAX_HOLD_HOURS + 10))
    fresh = clock.iso_utc()
    position = {"side": "long", "opened_at": fresh, "exchange_opened_at": stale}
    reason = bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="long")
    assert reason is not None
    assert "Haltedauer" in reason


def test_time_stop_falls_back_to_opened_at_without_exchange_opened_at():
    fresh = clock.iso_utc()
    position = {"side": "long", "opened_at": fresh, "exchange_opened_at": None}
    assert bot_signals.exit_signal(position, _trend(), LIMITS, btc_regime="long") is None


# --- Nachgezogener Stop: erst ab +1R, dann Mindestgewinn-Stop
# (STRATEGY_VERSION "2.3-swing4h", ersetzt die fruehere Chandelier-Regel -
# siehe trailing_stop_px()'s Docstring fuer die Begruendung/Messung) ---

def test_trailing_inactive_without_entry_or_initial_stop():
    position = {"side": "long", "stop_px": 95.0}
    assert bot_signals.trailing_stop_px(position, _trend(), LIMITS, current_price=105.0) is None


def test_trailing_inactive_before_one_r():
    """Vor +1R gilt ausschliesslich der Anfangsstop - kein Nachziehen."""
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0, "stop_px": 95.0}
    # Unrealisiert nur +0,4R (100 -> 102, Risiko 5) - unter TRAILING_ACTIVATION_R.
    assert bot_signals.trailing_stop_px(position, _trend(), LIMITS, current_price=102.0) is None


def test_trailing_locks_a_cost_adjusted_minimum_profit_past_one_r():
    """Ab +1R wird der Stop auf Einstand plus geschaetzte Rundreise-Kosten
    gezogen - ein exakter, von Kerzen/ATR unabhaengiger Wert (anders als
    die fruehere Chandelier-Regel)."""
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0, "stop_px": 95.0}
    new_stop = bot_signals.trailing_stop_px(position, None, LIMITS, current_price=105.0)
    expected = 100.0 * (1 + bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0)
    assert new_stop == pytest.approx(expected)
    assert new_stop > 100.0   # oberhalb des reinen Einstands - Kosten sind gedeckt


def test_trailing_stop_does_not_depend_on_candles_or_history():
    """Bewusster Unterschied zur fruaeheren Chandelier-Regel: das Ergebnis
    haengt nur von entry_px/initial_stop_px/side/current_price/stop_px ab -
    `df`/`ind` werden nicht mehr ausgewertet (siehe Docstring)."""
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0, "stop_px": 95.0}
    with_candles = bot_signals.trailing_stop_px(position, _trend(), LIMITS, current_price=105.0)
    without_candles = bot_signals.trailing_stop_px(position, None, LIMITS, current_price=105.0)
    assert with_candles == pytest.approx(without_candles)


def test_trailing_stop_is_a_one_time_lock_not_a_continuous_trail():
    """Bewusster Unterschied zur fruaeheren Chandelier-Regel: einmal
    gesetzt, bleibt der Mindestgewinn-Stop stehen, auch wenn der Kurs
    danach weit darueber hinauslaeuft - kein fortlaufendes Nachziehen mehr."""
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0, "stop_px": 95.0}
    locked = bot_signals.trailing_stop_px(position, None, LIMITS, current_price=105.0)
    position["stop_px"] = locked
    assert bot_signals.trailing_stop_px(position, None, LIMITS, current_price=200.0) is None


def test_trailing_stop_never_moves_back_down():
    """Ein zurueckweichender Stop waere kein Verlustbegrenzer mehr, sondern
    ein bewegliches Ziel - gilt weiterhin (Ratsche), auch fuer die neue
    Regel."""
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0,
               "stop_px": 101.0}   # bereits guenstiger als der Mindestgewinn-Stop (100.19)
    assert bot_signals.trailing_stop_px(position, None, LIMITS, current_price=105.0) is None


def test_trailing_stop_moves_down_for_short():
    position = {"side": "short", "entry_px": 100.0, "initial_stop_px": 105.0, "stop_px": 105.0}
    new_stop = bot_signals.trailing_stop_px(position, None, LIMITS, current_price=95.0)
    expected = 100.0 * (1 - bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0)
    assert new_stop == pytest.approx(expected)
    assert new_stop < 100.0


def test_trailing_stop_without_price_is_none():
    position = {"side": "long", "entry_px": 100.0, "initial_stop_px": 95.0, "stop_px": 95.0}
    assert bot_signals.trailing_stop_px(position, _trend(), LIMITS, current_price=None) is None


# --- Scan ---

class _Exchange:
    def __init__(self, prices, funding=None):
        self.prices = prices
        self.funding = funding or {}

    def mid_price(self, symbol):
        return self.prices.get(symbol)

    def funding_rate_hourly(self, symbol):
        return self.funding.get(symbol, 0.0)


def _scan_daily_fn(regime_df):
    return lambda symbol: regime_df


def test_scan_sorts_by_score_and_reports_every_candidate():
    up = _trend()
    flat = _candles(100.0 + 3.0 * np.sin(np.arange(160) * 2 * np.pi / 4), wick_pct=0.3)
    btc_own = _trend(per_candle_pct=0.1, noise=0.02, seed=13)   # schwaecher als UP -> Gate 5 erfuellt
    frames = {"UP": up, "FLAT": flat, "NODATA": None, "BTC": btc_own}
    exchange = _Exchange({"UP": float(up["Close"].iloc[-1]), "FLAT": 100.0, "NODATA": 5.0,
                          "BTC": float(btc_own["Close"].iloc[-1])})
    btc_daily = _daily_candles(100.0 * (1.01 ** np.arange(120)))
    signals = bot_signals.scan(exchange, ["FLAT", "NODATA", "UP"], LIMITS,
                               candles_fn=lambda s: frames[s],
                               daily_fn=_scan_daily_fn(btc_daily),
                               volume_fn=lambda s: 1e9)
    assert [s.symbol for s in signals][0] == "UP"
    assert len(signals) == 3          # auch "keine Daten" ist eine Antwort
    up_signal = next(s for s in signals if s.symbol == "UP")
    assert up_signal.tradeable is True
    nodata = next(s for s in signals if s.symbol == "NODATA")
    assert nodata.tradeable is False


def test_scan_survives_a_broken_candle_source():
    def boom(symbol):
        raise RuntimeError("API down")

    btc_daily = _daily_candles(100.0 * (1.01 ** np.arange(120)))
    signals = bot_signals.scan(_Exchange({"BTC": 100.0}), ["BTC"], LIMITS, candles_fn=boom,
                               daily_fn=_scan_daily_fn(btc_daily), volume_fn=lambda s: 1e9)
    assert signals[0].blocked_by is not None
    assert signals[0].tradeable is False


def test_scan_survives_a_broken_daily_source():
    """Ein Ausfall des Regime-Datenpfads darf den Scan nicht crashen -
    regime() faellt bei None-Daten selbst auf 'neutral' zurueck."""
    def boom_daily(symbol):
        raise RuntimeError("keine Tageskerzen")

    df = _trend()
    signals = bot_signals.scan(_Exchange({"BTC": float(df["Close"].iloc[-1])}), ["BTC"], LIMITS,
                               candles_fn=lambda s: df, daily_fn=boom_daily,
                               volume_fn=lambda s: 1e9)
    assert signals[0].tradeable is False
    assert "Tagesregime" in signals[0].blocked_by


def test_signal_as_row_is_json_serialisable():
    import json

    btc_daily = _daily_candles(100.0 * (1.01 ** np.arange(120)))
    signal = bot_signals.evaluate("BTC", _trend(), 130.0, 0.0, LIMITS,
                                  btc_regime=bot_signals.regime(btc_daily),
                                  btc_roc=0.0, volume_24h_usd=1e9)
    assert json.loads(json.dumps(signal.as_row()))["symbol"] == "BTC"
