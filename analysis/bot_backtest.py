"""Chronologischer No-Lookahead-Rücktest der BOT-Regeln
(core/bot_signals.py + core/bot_guards.py).

NAMENSKLARSTELLUNG: "Walk-Forward" impliziert im engeren quantitativen Sinn
oft ein rollierendes Entwicklungs-/Prüf-Fenster mit periodischer Neu-
Kalibrierung. Das leistet dieses Modul BEWUSST NICHT (siehe "KEINE
OPTIMIERUNGSSCHLEIFE" unten) - es ist ein einzelner chronologischer
Durchlauf ohne Lookahead, keine Serie getrennter Trainings-/Testfenster.
"Chronologischer No-Lookahead-Rücktest" ist die genauere Bezeichnung.

Bis hierhin gab es für den Trading-Bot keinerlei Rentabilitätsnachweis:
`analysis/backtest.py` ist ausdrücklich kein Regel-Rücktest (nur eine
Bestands-Rückrechnung), `analysis/cycle_backtest.py` testet die BTC-Verkaufs-
leiter, und `core.bot.performance_index_df()` liest bloß auf, was live schon
passiert ist. Die Bot-Tests wiederum beweisen, dass die Engine auf einen
künstlich erzeugten Trend erwartungsgemäß reagiert - nicht, dass die Strategie
Geld verdient. Genau diese Lücke schließt dieses Modul.

KEIN NACHBAU DER LOGIK: Der Rücktest ruft dieselben Funktionen auf, die auch
live entscheiden - `bot_signals.regime/evaluate/position_size_usd/
exit_signal/trailing_stop_px` und `bot_guards.evaluate_entry_order`. Ein
nachgebauter Zwilling würde über kurz oder lang eine andere Strategie testen
als die, die tatsächlich handelt. Das gilt seit dem V2-Umbau (Phase 4) AUCH
für das Tagesregime: `_daily_btc_regime_series()` ruft `bot_signals.regime()`
für jeden Tag einzeln mit einem wachsenden Datenausschnitt auf, statt die
Long/Short/Neutral-Regel selbst nachzubilden.

KEIN LOOKAHEAD, an fünf Stellen abgesichert:
1. Zum Zeitpunkt t sieht `evaluate()` nur Kerzen bis einschließlich t - und
   `bot_signals.prepare()` verwirft davon selbst noch die letzte (laufende).
   Bewertet wird also die abgeschlossene 4h-Kerze t-1, exakt wie im
   Live-Takt.
2. Ausgeführt wird zum OPEN der Kerze t (plus Slippage) - dem ersten Kurs,
   der zum Entscheidungszeitpunkt tatsächlich handelbar ist.
3. Stops werden INNERHALB der Kerze über High/Low geprüft, nicht am Close.
   Live liegt eine echte Stop-Order auf der Börse, die intrabar auslöst; der
   15-Minuten-Poll in core/bot.py ist nur die Nachkontrolle. Ein
   Close-basierter Rücktest wäre hier systematisch zu optimistisch.
4. Das Tagesregime für Tag T nutzt in `_daily_btc_regime_series()` nur Tage
   VOR T (bot_signals.regime()/prepare() verwerfen T selbst als "noch
   laufend") - dieselbe Kausalität wie live, wo das Regime am Morgen von T
   ebenfalls nur den Schlusskurs von T-1 kennt.
5. `_entry_candle_stop()` (Phase 6): die EROEFFNUNGSKERZE einer neuen
   Position wurde vorher NIE auf einen Stop-Hit geprüft - Schritt 1 jedes
   Takts prüft nur bereits offene Positionen, VOR dem Einstieg dieses Takts.
   Ein Trade, der in derselben Kerze eröffnet UND wieder ausgestoppt worden
   wäre, erschien bislang frühestens eine Kerze (4h) zu spät als Verlierer.
   Mit 1h-Unterkerzen (load_market_data(), Feld "hourly") so präzise wie
   live möglich, sonst Rückfall auf dieselbe 4h-Bar-Näherung wie Schritt 1.

BEKANNTE EINSCHRÄNKUNGEN:
- LIQUIDITÄT (Gate 7) IST NICHT REKONSTRUIERBAR: data.hyperliquid.
  day_notional_volume_usd() liefert nur das AKTUELLE 24h-Volumen, keine
  historische Zeitreihe. Der Rücktest nimmt Gate 7 deshalb IMMER als erfüllt
  an (siehe `_UNLIMITED_VOLUME` unten) - das macht das Ergebnis tendenziell
  OPTIMISTISCHER als live, wo ein zwischenzeitlich dünner Markt zusätzlich
  blockieren würde.
- SIGNAL-RESET (Wiedereinstiegssperre) WIRD SEIT 23.09.2026 SIMULIERT: über
  dieselbe reine Funktion wie live (core.bot.reentry_block_reason), Zustand im
  Rücktest-eigenen `_State.disarmed`. Vorher fehlte sie (externe Prüfung) und
  machte das Ergebnis optimistischer: ein wieder erlaubtes Comeback auf
  dasselbe Setup wäre live noch gesperrt gewesen. ZAHLEN VOR DIESER ÄNDERUNG
  enthalten die Sperre nicht. Die KI (agents/trader.py) kann seit Phase 8
  nichts mehr sperren oder schliessen.
- KANDIDATEN-UNIVERSUM IST EIN EINZIGER, HEUTIGER SCHNAPPSCHUSS: core.
  bot.candidate_symbols() liest core.bot_universe.pool_symbols(), einen
  taeglich aktualisierten, aber nur GEGENWAERTIGEN Filter (Marktkap. +
  Mindestalter + Hyperliquid-Liquiditaet). Es gibt keine historische
  Rekonstruktion, welcher Coin an einem vergangenen Datum qualifiziert
  gewesen waere - ein heute grosser, damals kleiner Coin erscheint ueber den
  GESAMTEN Rueckblick als durchgehend qualifiziert (Survivorship-/
  Look-ahead-Tendenz), dieselbe bewusste Einschraenkung wie bei Gate 7 oben.

KEINE OPTIMIERUNGSSCHLEIFE. Es wird genau EIN Parametersatz gerechnet (die
gewählte Risikostufe). Sobald man über Schwellen und Gewichte iteriert,
entsteht das überangepasste Ergebnis, das jeden Rücktest wertlos macht. Die
Frage hier lautet "hat diese Regel in diesem Zeitraum Geld verdient", nicht
"welche Parameter wären die besten gewesen".

GRANULARITÄT: 4h-Kerzen, wie live (core.bot._default_candles_fn, core.bot.
run_deterministic_cycle - Phase 3) - der Bot entscheidet seit dem V2-Umbau
nur noch einmal je abgeschlossenem 4h-Fenster, nicht mehr stündlich.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from analysis import technical
from core import bot_config, bot_guards, bot_signals, clock
from data.hyperliquid import MIN_NOTIONAL_USD, PAPER_SLIPPAGE_PCT, TAKER_FEE

# Ohne Burn-in liefe der erste Trade auf einem halb gefüllten Indikator-Fenster
# (analysis/cycle_backtest.py hat diese Lektion teuer gelernt: die BTC-Serie
# löste dort ohne Burn-in binnen drei Wochen eine volle Verkaufsstufe aus).
# bot_signals.prepare() verwirft zusätzlich die laufende Kerze, daher +1.
# Seit dem V2-Umbau in 4h-KERZEN gemessen (vorher 1h) - derselbe
# Kerzen-Vorlauf, jetzt in der Einheit, in der der Bot tatsächlich denkt.
BURN_IN_CANDLES = bot_signals.MIN_CANDLES + 1

# Vergleichsmaßstäbe. "BTC halten" ist die ehrliche Alternative für jemanden,
# der ohnehin Krypto hält; die MA50-Trendfolge zeigt, ob der vielteilige Score
# überhaupt etwas gegenüber einer Ein-Zeilen-Regel beiträgt. Dieselbe
# Konstante wie core.bot_signals.BENCHMARK_SYMBOL (Gate 5, regime()) - EIN
# Marktanker, nicht zwei unabhaengig gepflegte "BTC"-Literale.
BENCHMARK_SYMBOL = bot_signals.BENCHMARK_SYMBOL

# Groesse des Kerzenfensters, das die Signal-Engine je Takt zu sehen bekommt.
# NICHT die ganze Historie: live liefert core.bot._default_candles_fn()
# genau `_CANDLE_LOOKBACK_HOURS`/4h ≈ 125 4h-Kerzen, mehr sieht der Bot also
# nie. 400 Kerzen (statt 1:1 auf core.bot's Lookback zu spiegeln) bleibt
# bewusst grosszuegiger, damit trailing_stop_px()'s "hoechstes Hoch SEIT
# EROEFFNUNG"-Fenster (core.bot_signals) auch eine nahe an MAX_HOLD_HOURS
# (336h ≈ 84 4h-Kerzen) lang gehaltene Position noch vollstaendig sieht.
LOOKBACK_CANDLES = 400

# Liquiditaets-Gate (7) ist historisch nicht rekonstruierbar (siehe
# Modul-Docstring) - wird im Ruecktest immer als erfuellt behandelt.
_UNLIMITED_VOLUME = float("inf")

# --- AUSSTIEGS-VARIANTEN (Analyseplan "Ausstiegsmechanik ueberdenken",
# Schritt 2) ---
#
# `exit_variant` in run_backtest() waehlt zwischen der REFERENZ (core.
# bot_signals.trailing_stop_px() - der Live-Regel, unveraendert) und EINER
# experimentellen Alternative fuer den A/B-Vergleich. Beide teilen dieselbe
# Aktivierungsschwelle core.bot_signals.TRAILING_ACTIVATION_R, damit der
# Vergleich NUR die Stop-FORMEL nach der Aktivierung isoliert, nicht auch
# noch den Aktivierungszeitpunkt mitveraendert.
#
# `_min_profit_stop_px()` EXISTIERT AUSSCHLIESSLICH HIER - kein Live-Pfad
# ruft sie auf, core/bot_signals.py bleibt die alleinige Quelle der
# tatsaechlichen Handelsregeln. Regel: sobald +1R erreicht ist, wird der
# Stop EINMALIG und ENDGUELTIG auf einen kostenbereinigten Mindestgewinn
# gezogen (Einstand +/- des geschaetzten Rundreise-Kostensatzes) - kein
# weiteres Chandelier-Nachziehen danach. Testet die Gegenhypothese zur
# bestehenden Regel: lohnt sich ein hartes Einfrieren des Gewinns mehr als
# ein dem Trend folgender, aber im Rueckblick oft weit zurueckgezogener
# Stop (siehe die Live-Beobachtung bei LINK/ETHFI im Analysebericht)?
#
# `_hourly_trailing_stop_px()` (Schritt 3a): dieselbe Chandelier-Formel wie
# die Referenz (Hoch/Tief SEIT EROEFFNUNG minus/plus CHANDELIER_ATR_MULT x
# ATR, ab +1R), aber INNERHALB der laufenden 4h-Kerze anhand von
# 1h-Unterkerzen mehrfach statt nur einmal je 4h-Takt nachgezogen UND auf
# einen Treffer geprueft - eine Generalisierung von _entry_candle_stop()
# (das bislang NUR die Eroeffnungskerze so praezise behandelte) auf JEDE
# gehaltene Kerze. Die ATR bleibt bewusst die 4h-ATR (guenstige Naeherung -
# eine echte stuendliche ATR-Serie waere Schritt 3b, eine groessere, eigene
# Messung). Ohne 1h-Daten fuer ein Symbol ist die Funktion ein reines No-Op
# (kein Vorteil ohne Unterkerzen), der Aufrufer verhaelt sich dann wie die
# Referenz.
#
# `_chandelier_stop_px_v2_2()` (nachtraeglich ergaenzt, 13.09.2026): DIE
# EINGEFRORENE V2.2-BASELINE - core.bot_signals.trailing_stop_px() WAR bis
# STRATEGY_VERSION "2.2-swing4h" exakt diese Chandelier-Formel auf reiner
# 4h-Kadenz, ist seit "2.3-swing4h" aber die Mindestgewinn-Regel. "reference"
# unten ruft weiterhin die LIVE-Funktion auf (das ist per Definition richtig
# fuer "was der Bot gerade tatsaechlich tut"), war damit aber unbemerkt KEINE
# V2.2-Referenz mehr, sobald trailing_stop_px() sich aenderte - ein
# "V2.3 schlaegt V2.2"-Vergleich verglich V2.3 faktisch mit sich selbst.
# Existiert bewusst als EIGENE, von core/bot_signals.py komplett unabhaengige
# Kopie, damit ein A/B-Vergleich gegen "damals" nie wieder von einer
# spaeteren Aenderung an trailing_stop_px() unbemerkt entwertet wird.
EXIT_VARIANTS = ("reference", "min_profit_stop", "hourly_trailing", "chandelier_v2_2")


def _min_profit_stop_px(position: "_Position", current_price: float | None) -> float | None:
    if not current_price or current_price <= 0:
        return None
    # Kursabstand, nicht position.initial_risk_usd (= Kursabstand * size,
    # also das GESAMTE Dollar-Risiko der Position): der Zaehler unten ist ein
    # Kurs-Delta pro Einheit, keine Dollar-Groesse - eine Division durch das
    # Dollar-Risiko war nur bei size==1 zufaellig richtig (siehe core.
    # bot_signals.trailing_stop_px(), das exakt diesen Kursabstand nutzt).
    risk_per_unit = abs(position.entry_px - position.initial_stop_px)
    if risk_per_unit <= 0:
        return None
    direction = 1.0 if position.side == "long" else -1.0
    unrealized_r = (current_price - position.entry_px) * direction / risk_per_unit
    if unrealized_r < bot_signals.TRAILING_ACTIVATION_R:
        return None
    locked = position.entry_px * (1 + bot_signals._ROUND_TRIP_COST_PCT_ESTIMATE / 100.0 * direction)
    current_stop = position.stop_px
    if current_stop is not None and (locked - current_stop) * direction <= 0:
        return None   # bereits mindestens so guenstig (typischerweise: schon gesetzt)
    return locked


def _hourly_trailing_stop_px(position: "_Position", ts: pd.Timestamp, window,
                             hourly_df: pd.DataFrame | None,
                             current_price: float | None):
    """Schritt 3a: siehe EXIT_VARIANTS-Kommentar oben. `window` ist dieselbe
    4h-Indikator-Scheibe wie bei der Referenz (fuer ATR% - Naeherung,
    bewusst nicht stuendlich neu berechnet). Gibt (neuer_stop, hit) zurueck;
    `hit` ist (Zeitpunkt, Fill-Kurs) oder None. `neuer_stop` ist None, wenn
    nichts zu aendern ist (kein Aktivierung, keine 1h-Daten, kein ATR)."""
    if not current_price or current_price <= 0 or window is None or len(window) == 0:
        return None, None
    # Kursabstand, nicht position.initial_risk_usd - siehe _min_profit_stop_px() oben.
    risk_per_unit = abs(position.entry_px - position.initial_stop_px)
    if risk_per_unit <= 0:
        return None, None
    direction = 1.0 if position.side == "long" else -1.0
    unrealized_r = (current_price - position.entry_px) * direction / risk_per_unit
    if unrealized_r < bot_signals.TRAILING_ACTIVATION_R:
        return None, None
    if hourly_df is None or hourly_df.empty:
        return None, None   # kein Vorteil ohne Unterkerzen - Aufrufer faellt auf Referenz-Verhalten zurueck

    r = bot_signals.readings(window)
    if r is None or not r["atr_pct"]:
        return None, None
    atr_abs = r["atr_pct"] / 100.0 * current_price
    extreme = float(window["High"].max()) if position.side == "long" else float(window["Low"].min())
    stop = position.stop_px

    window_end = ts + pd.Timedelta(hours=4)
    sub = hourly_df[(hourly_df.index >= ts) & (hourly_df.index < window_end)]
    for sub_ts, row in sub.iterrows():
        high, low = float(row["High"]), float(row["Low"])
        if position.side == "long":
            extreme = max(extreme, high)
            candidate = extreme - bot_signals.CHANDELIER_ATR_MULT * atr_abs
            if candidate >= current_price:
                candidate = None
        else:
            extreme = min(extreme, low)
            candidate = extreme + bot_signals.CHANDELIER_ATR_MULT * atr_abs
            if candidate <= current_price:
                candidate = None
        if candidate is not None and (stop is None or (candidate - stop) * direction > 0):
            stop = candidate
        if stop is None:
            continue
        hit = (low <= stop) if position.side == "long" else (high >= stop)
        if hit:
            sub_open = float(row["Open"])
            fill_px = (min(stop, sub_open) if position.side == "long" else max(stop, sub_open))
            return stop, (sub_ts, fill_px)
    return stop, None


def _chandelier_stop_px_v2_2(position: "_Position", window,
                             current_price: float | None) -> float | None:
    """DIE EINGEFRORENE V2.2-BASELINE - siehe EXIT_VARIANTS-Kommentar oben
    fuer das Warum. Aktivierung ab TRAILING_ACTIVATION_R (Kursabstand, wie
    bei den anderen Varianten hier), `extreme` = hoechstes Hoch (long) bzw.
    tiefstes Tief (short) aus `window` (der Indikator-Scheibe VOR dieser
    Kerze, reine 4h-Kadenz - kein stuendliches Nachziehen, das bleibt
    "hourly_trailing" vorbehalten), Stop = extreme minus/plus
    CHANDELIER_ATR_MULT x ATR. Ratsche: wandert ausschliesslich in
    Gewinnrichtung."""
    if not current_price or current_price <= 0 or window is None or len(window) == 0:
        return None
    risk_per_unit = abs(position.entry_px - position.initial_stop_px)
    if risk_per_unit <= 0:
        return None
    direction = 1.0 if position.side == "long" else -1.0
    unrealized_r = (current_price - position.entry_px) * direction / risk_per_unit
    if unrealized_r < bot_signals.TRAILING_ACTIVATION_R:
        return None
    r = bot_signals.readings(window)
    if r is None or not r["atr_pct"]:
        return None
    atr_abs = r["atr_pct"] / 100.0 * current_price
    extreme = float(window["High"].max()) if position.side == "long" else float(window["Low"].min())
    candidate = (extreme - bot_signals.CHANDELIER_ATR_MULT * atr_abs if position.side == "long"
                else extreme + bot_signals.CHANDELIER_ATR_MULT * atr_abs)
    current_stop = position.stop_px
    if current_stop is not None and (candidate - current_stop) * direction <= 0:
        return None   # Ratsche: bereits mindestens so guenstig
    return candidate


@dataclass
class _Position:
    symbol: str
    side: str
    size: float
    entry_px: float
    stop_px: float
    # UNVERAENDERLICH ab Eroeffnung - dieselbe Rolle wie bot_positions.
    # initial_stop_px in der DB: core.bot_signals.trailing_stop_px() braucht
    # den URSPRUENGLICHEN Risikoabstand, um die +1R-Aktivierungsschwelle zu
    # pruefen, stop_px selbst wird beim Nachziehen ueberschrieben.
    initial_stop_px: float
    opened_at: pd.Timestamp
    initial_risk_usd: float
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    notional_at_entry: float = 0.0
    regime: str = "unbekannt"


@dataclass
class _State:
    """Alles, was der Rücktest über die Zeit mitführt. Bewusst ein eigener,
    kleiner Zustand statt der DB: der Rücktest darf nie in `bot_positions`
    schreiben, sonst vermischen sich Simulation und echter Handel."""
    equity: float
    cash: float
    positions: dict[str, _Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    slippage_usd: float = 0.0
    trades_today: int = 0
    day: object = None
    last_closed_at: dict = field(default_factory=dict)
    equity_start_of_day: float = 0.0
    blocked_reasons: dict = field(default_factory=dict)
    # Wiedereinstiegssperre wie core.bot_symbol_state: {symbol: {"at", "side"}},
    # gesetzt bei JEDEM Schluss, geleert sobald reentry_block_reason() frei gibt.
    disarmed: dict = field(default_factory=dict)


def _slipped(price: float, side: str, closing: bool) -> float:
    """Ausführungskurs immer zum Nachteil - dieselbe Konvention wie
    data.hyperliquid.slipped_price, hier nachgebildet, damit dieses Modul
    ohne Börsenobjekt auskommt."""
    opening_buy = (side == "long") if not closing else (side == "short")
    return price * (1 + PAPER_SLIPPAGE_PCT if opening_buy else 1 - PAPER_SLIPPAGE_PCT)


def _daily_btc_regime_series(btc_daily: pd.DataFrame | None) -> pd.Series:
    """Fuer jeden Tag T (ab genug Vorlauf) das Tagesregime, das
    bot_signals.regime() SEHEN WUERDE, wenn T der aktuelle (noch laufende,
    also von prepare() intern verworfene) Tag waere - ruft regime() bewusst
    fuer JEDEN Tag einzeln mit einem wachsenden Datenausschnitt auf (siehe
    Modul-Docstring "KEIN NACHBAU"), damit kein spaeterer Tag das Regime
    eines frueheren beeinflussen kann. Performanceueberlegung unnoetig:
    MA50 auf hoechstens ein paar hundert Tagen ist auch mehrere hundert Mal
    berechnet Millisekundenarbeit.

    Index der Ergebnis-Reihe ist der jeweilige Tag T selbst - .asof(ts_4h)
    im Haupt-Takt liefert damit fuer eine 4h-Kerze AM Tag T exakt das
    Regime, das T als "heute" (noch nicht abgeschlossen) behandelt, genau
    wie live."""
    if btc_daily is None or btc_daily.empty:
        return pd.Series(dtype=object)
    min_len = bot_signals._REGIME_MIN_CANDLES + 1
    if len(btc_daily) < min_len:
        return pd.Series(dtype=object)
    out = {}
    for i in range(min_len, len(btc_daily) + 1):
        ts = btc_daily.index[i - 1]
        out[ts] = bot_signals.regime(btc_daily.iloc[:i])
    return pd.Series(out)


def _apply_funding(state: _State, ts: pd.Timestamp, next_ts: pd.Timestamp | None,
                   funding: dict, prices: dict):
    """Funding auf alle offenen Positionen, je Takt einmal (Hyperliquid
    verrechnet stuendlich, aber der Bot-Takt selbst ist 4h - dieselbe
    Naeherung wie live: core.bot._apply_funding_if_paper() rechnet ebenfalls
    nur zum Zeitpunkt des Aufrufs ab). Vorzeichen wie in core/bot.py: positiv
    = Kosten, negativ = Einnahme; Longs zahlen bei positiver Rate, Shorts
    bekommen.

    SUMMIERT die TATSAECHLICHEN Stundenraten aus `data.hyperliquid.
    funding_history()` innerhalb `[ts, next_ts)` - dem tatsaechlichen Abstand
    zum NAECHSTEN Entscheidungstakt -, statt EINE Rate zu samplen
    (`series.asof(ts)`) und mit 4 zu multiplizieren (Korrektur, gefunden von
    einer externen Pruefung 13.09.2026: Hyperliquid rechnet echt stuendlich
    ab, eine einzelne Stunde repraesentiert nicht zuverlaessig die anderen
    drei). `next_ts` statt eines hart codierten `ts + 4h`: bei genau 4h
    Abstand (der Normalfall echter 4h-Kerzen) ist das dasselbe Fenster: bei
    einer Datenluecke (der naechste verfuegbare Takt folgt spaeter) wird das
    in der laengeren Zwischenzeit TATSAECHLICH angefallene Funding trotzdem
    vollstaendig erfasst, statt nur die ersten 4h davon zu zaehlen ODER (bei
    einem hart auf 4h fixierten Fenster und Takten, die aus einem anderen
    Grund NICHT exakt 4h auseinanderliegen) Stundenraten doppelt ueber
    mehrere Takte hinweg zu zaehlen. Fehlt fuer dieses Fenster JEDE
    Stundenrate (Datenluecke in den Fundingdaten selbst), faellt die
    Funktion auf die alte Sample-Naeherung zurueck - die zuletzt bekannte
    Rate ueber die TATSAECHLICHE Fensterlaenge (in Stunden) hochgerechnet,
    besser als gar kein Funding zu verrechnen. Fehlen nur EINZELNE Stunden
    innerhalb des Fensters, werden ausschliesslich die tatsaechlich
    vorhandenen summiert, nicht stillschweigend hochgerechnet."""
    if next_ts is None or next_ts <= ts:
        next_ts = ts + pd.Timedelta(hours=4)
    window_hours = (next_ts - ts).total_seconds() / 3600.0
    for pos in state.positions.values():
        series = funding.get(pos.symbol)
        if series is None:
            continue
        hours_in_window = series[(series.index >= ts) & (series.index < next_ts)].dropna()
        if not hours_in_window.empty:
            total_rate = float(hours_in_window.sum())
        else:
            try:
                sampled = float(series.asof(ts))
            except (KeyError, ValueError, TypeError):
                continue
            if pd.isna(sampled):
                continue
            total_rate = sampled * window_hours
        price = prices.get(pos.symbol) or pos.entry_px
        direction = 1 if pos.side == "long" else -1
        cost = total_rate * price * pos.size * direction
        state.cash -= cost
        state.funding_usd += cost
        pos.funding_usd += cost


def _loss_streak_state(trades: list[dict]) -> tuple[int, object | None]:
    """Wie core.bot._loss_streak_state(), hier auf state.trades statt der DB -
    dieselbe Definition (aufeinanderfolgende Verlierer ueber ALLE Symbole,
    juengste zuerst) fuer core.bot_guards.check_loss_streak(). state.trades
    entsteht chronologisch (jede Schliessung wird angehaengt, sobald sie
    passiert) - rueckwaerts iterieren reicht, kein Sortieren noetig."""
    streak = 0
    last_loss_at = None
    for t in reversed(trades):
        pnl = t.get("net_pnl_usd")
        if pnl is None or pnl >= 0:
            break
        streak += 1
        if last_loss_at is None:
            last_loss_at = t["closed_at"]
    return streak, last_loss_at


def _close(state: _State, pos: _Position, ts: pd.Timestamp, raw_px: float, reason: str):
    fill = _slipped(raw_px, pos.side, closing=True)
    state.slippage_usd += abs(fill - raw_px) * pos.size
    notional = fill * pos.size
    fee = notional * TAKER_FEE
    direction = 1 if pos.side == "long" else -1
    gross = (fill - pos.entry_px) * pos.size * direction
    net = gross - fee
    state.cash += gross - fee
    state.fees_usd += fee
    pos.fees_usd += fee
    state.last_closed_at[pos.symbol] = ts.to_pydatetime()
    state.disarmed[pos.symbol] = {"at": ts.to_pydatetime(), "side": pos.side}
    # core.bot.trades_today() zaehlt ALLE gefuellten Orders, Eroeffnungen UND
    # Schliessungen ("core/bot.py trades_today"-Docstring) - vorher zaehlte
    # der Ruecktest nur Eroeffnungen und liess das Tageslimit dadurch
    # grosszuegiger erscheinen als es live tatsaechlich ist.
    state.trades_today += 1
    state.trades.append({
        "symbol": pos.symbol, "side": pos.side, "opened_at": pos.opened_at,
        "closed_at": ts, "entry_px": pos.entry_px, "close_px": fill,
        "size": pos.size, "notional_usd": pos.notional_at_entry,
        "gross_pnl_usd": gross, "fees_usd": pos.fees_usd, "funding_usd": pos.funding_usd,
        # Netto = Kursergebnis abzüglich ALLER Gebühren und Funding dieser
        # Position - dieselbe Definition wie core.bot.close_position() sie
        # seit der Gebührenkorrektur in bot_positions schreibt.
        "net_pnl_usd": net - pos.funding_usd - (pos.fees_usd - fee),
        "r_multiple": ((net - pos.funding_usd - (pos.fees_usd - fee)) / pos.initial_risk_usd
                       if pos.initial_risk_usd > 0 else None),
        "hold_hours": (ts - pos.opened_at).total_seconds() / 3600,
        "reason": reason, "regime": pos.regime,
    })
    del state.positions[pos.symbol]


def _entry_candle_stop(pos: "_Position", ts: pd.Timestamp, bar,
                       hourly_df: pd.DataFrame | None):
    """Prueft, ob der Stop von `pos` noch INNERHALB des Rests der Kerze `ts`
    gerissen waere - fuer eine soeben in `ts` eroeffnete Position (die
    einzige echte Luecke im alten Ruecktest, siehe Modul-Docstring Punkt 3
    unter "KEIN LOOKAHEAD") GENAUSO wie fuer eine bereits laenger offene
    Position, deren Stop-Level in Schritt 3 dieser Kerze neu gesetzt wurde -
    Schritt 5 der Hauptschleife ruft diese Funktion fuer BEIDE Faelle
    einheitlich auf, NACH der Einstiegsentscheidung (siehe dessen Kommentar).

    Mit 1h-Unterkerzen (load_market_data(), 'hourly' - ein Nebengewinn der
    4h-Umstellung, keine 15-Minuten-Daten noetig) so praezise wie moeglich:
    die erste 1h-Kerze INNERHALB von `ts`, die den Stop reisst, entscheidet.
    Fehlen sie fuer dieses Symbol (aeltere/synthetische Tests, oder die
    Boerse liefert keine), faellt der Check auf dieselbe 4h-Bar-High/Low-
    Naeherung zurueck wie fuer alle anderen Kerzen - die Luecke ist damit
    IMMER geschlossen, nie nur wenn 1h-Daten vorliegen. `bar=None` (fehlende
    4h-Kerze fuer dieses Symbol zu `ts`, moeglich bei einer laenger offenen
    Bestandsposition mit Datenluecke) ergibt ohne Unterkerzen KEINEN Treffer,
    statt abzustuerzen - dieselbe Vorsicht wie das fruehere `if bar is None:
    continue` in der alten Schritt-4-Schleife.

    Gibt (Zeitpunkt, Fill-Kurs) zurueck, wenn der Stop noch innerhalb von
    `ts` gerissen waere, sonst None."""
    if hourly_df is not None and not hourly_df.empty:
        window_end = ts + pd.Timedelta(hours=4)
        sub = hourly_df[(hourly_df.index >= ts) & (hourly_df.index < window_end)]
        for sub_ts, row in sub.iterrows():
            hit = (float(row["Low"]) <= pos.stop_px if pos.side == "long"
                  else float(row["High"]) >= pos.stop_px)
            if hit:
                sub_open = float(row["Open"])
                fill_px = (min(pos.stop_px, sub_open) if pos.side == "long"
                          else max(pos.stop_px, sub_open))
                return sub_ts, fill_px
        return None
    if bar is None:
        return None
    hit = (float(bar["Low"]) <= pos.stop_px if pos.side == "long"
          else float(bar["High"]) >= pos.stop_px)
    if not hit:
        return None
    bar_open = float(bar["Open"])
    fill_px = (min(pos.stop_px, bar_open) if pos.side == "long"
              else max(pos.stop_px, bar_open))
    return ts, fill_px


def _mark_to_market(state: _State, prices: dict) -> float:
    unrealized = 0.0
    for pos in state.positions.values():
        price = prices.get(pos.symbol)
        if price is None:
            continue
        direction = 1 if pos.side == "long" else -1
        unrealized += (price - pos.entry_px) * pos.size * direction
    return state.cash + unrealized


def load_market_data(symbols: list[str], days: int,
                     candles_fn=None, daily_fn=None, funding_fn=None,
                     progress_fn=None) -> dict:
    """Kerzen (4h), Tageskerzen (nur BTC, fuer das Regime), 1h-Kerzen (nur
    fuer den Eroeffnungskerzen-Stop-Check, siehe _entry_candle_stop) und
    Fundinghistorie einmal laden - der eigentliche Rücktest arbeitet danach
    rein offline und ist damit exakt wiederholbar."""
    if candles_fn is None or daily_fn is None or funding_fn is None:
        from data import hyperliquid
        candles_fn = candles_fn or hyperliquid.candles_range
        daily_fn = daily_fn or hyperliquid.candles_range
        funding_fn = funding_fn or hyperliquid.funding_history
    end_ms = int(clock.now_utc().timestamp() * 1000)
    start_ms = end_ms - int(days * 86_400_000)
    # Fuer das Regime braucht BTC zusaetzlichen Vorlauf VOR dem eigentlichen
    # Ruecktest-Zeitraum (core.bot_signals._REGIME_MIN_CANDLES Tage) - sonst
    # waere das Regime fuer die ersten Wochen des Zeitraums selbst "neutral"
    # mangels Daten, statt eine echte Einschaetzung zu liefern.
    daily_start_ms = start_ms - int(
        (bot_signals._REGIME_MIN_CANDLES + 30) * 86_400_000)

    # Der Live-Datenabruf kann bei einem größeren Kandidatenpool deutlich
    # länger dauern als die anschließende reine Berechnung. Der optionale
    # Callback hält die UI dabei sichtbar lebendig, ohne die Testbarkeit der
    # Rücktest-Engine an Streamlit zu koppeln.
    def _progress(stage: str, completed: int, total: int, detail: str):
        if not callable(progress_fn):
            return
        try:
            progress_fn(stage, completed, total, detail)
        except Exception:
            # Eine Anzeige darf den fachlichen Rücktest nie abbrechen.
            pass

    candles, funding, missing_funding, hourly = {}, {}, [], {}
    skipped_symbols: dict[str, str] = {}
    fetch_notes: dict[str, str] = {}
    total_steps = len(symbols) * 3 + 1
    completed = 0
    _progress("Lade Marktdaten", completed, total_steps,
              f"Bereite {len(symbols)} Symbole vor …")
    for symbol in symbols:
        _progress("Lade 4h-Kerzen", completed, total_steps, symbol)
        try:
            df = candles_fn(symbol, start_ms, end_ms, "4h")
        except Exception as exc:
            skipped_symbols[symbol] = f"4h-Abruf fehlgeschlagen ({type(exc).__name__})"
            completed += 3
            _progress("Symbol übersprungen", completed, total_steps, symbol)
            continue
        completed += 1
        if df is None or len(df) < BURN_IN_CANDLES + 24:
            skipped_symbols[symbol] = "zu wenige 4h-Kerzen"
            completed += 2
            _progress("Symbol übersprungen", completed, total_steps, symbol)
            continue
        candles[symbol] = df
        _progress("Lade Funding-Historie", completed, total_steps, symbol)
        try:
            series = funding_fn(symbol, start_ms, end_ms)
        except Exception as exc:
            series = None
            fetch_notes[symbol] = f"Funding-Abruf fehlgeschlagen ({type(exc).__name__})"
        completed += 1
        if series is None or series.empty:
            missing_funding.append(symbol)
        else:
            funding[symbol] = series
        # NICHT fatal, wenn das fehlschlaegt oder leer bleibt -
        # _entry_candle_stop() faellt dann auf dieselbe 4h-Bar-Naeherung
        # zurueck wie jede andere Kerze (siehe dessen Docstring).
        _progress("Lade 1h-Kerzen", completed, total_steps, symbol)
        try:
            hourly_df = candles_fn(symbol, start_ms, end_ms, "1h")
        except Exception as exc:
            hourly_df = None
            fetch_notes.setdefault(symbol, f"1h-Abruf fehlgeschlagen ({type(exc).__name__})")
        completed += 1
        if hourly_df is not None and not hourly_df.empty:
            hourly[symbol] = hourly_df

    _progress("Lade BTC-Tagesdaten", completed, total_steps, BENCHMARK_SYMBOL)
    try:
        btc_daily = daily_fn(BENCHMARK_SYMBOL, daily_start_ms, end_ms, "1d")
    except Exception as exc:
        btc_daily = None
        fetch_notes[BENCHMARK_SYMBOL] = f"Tagesdaten-Abruf fehlgeschlagen ({type(exc).__name__})"
    completed += 1
    _progress("Marktdaten geladen", completed, total_steps,
              f"{len(candles)} von {len(symbols)} Symbolen auswertbar")

    # Vollstaendigkeit je Symbol: 6 4h-Kerzen/Tag ist der theoretische
    # Idealwert (24h/4h) - eine erhebliche Unterschreitung ist entweder eine
    # kuerzere Listungs-Historie (legitim) ODER eine luecken-/abbruchbehaftete
    # Antwort von data.hyperliquid.candles_range (das bei einem Fehler MITTEN
    # im Abruf still abbricht und den bisherigen Teil zurueckgibt, siehe
    # dessen Docstring) - ohne diese Zahl ist ein Ergebnis nicht von einem
    # anderen mit stillschweigend unvollstaendiger Historie zu unterscheiden
    # (CLAUDE.md dokumentiert genau dieses Symptom: "zwischen 0 und 30 Trades
    # ... schwankende Vollstaendigkeit"). Kein Fehlschlag, nur Transparenz -
    # die Interpretation bleibt beim Betrachter (UI/Walk-Forward).
    expected_per_symbol = max(1, int(days * 6))
    candle_completeness = {
        symbol: {"candles": len(df), "expected": expected_per_symbol,
                "completeness_pct": round(len(df) / expected_per_symbol * 100, 1)}
        for symbol, df in candles.items()
    }

    return {"candles": candles, "funding": funding, "missing_funding": missing_funding,
           "btc_daily": btc_daily, "hourly": hourly,
           "load_report": {
               "requested_symbols": len(symbols),
               "loaded_symbols": len(candles),
               "skipped_symbols": skipped_symbols,
               "fetch_notes": fetch_notes,
               "candle_completeness": candle_completeness,
               "btc_daily_candles": 0 if btc_daily is None else len(btc_daily),
           }}


def run_backtest(market: dict, risk_level: int | None = None,
                 starting_equity_usd: float = 250.0,
                 disabled_gates: frozenset[str] = frozenset(),
                 exit_variant: str = "reference") -> dict:
    """Der eigentliche Durchlauf. `market` kommt aus load_market_data() (oder
    aus einem Test mit synthetischen Reihen) - so bleibt diese Funktion
    deterministisch und ohne Netzzugriff.

    `disabled_gates` reicht unveraendert an core.bot_signals.evaluate() durch
    - AUSSCHLIESSLICH fuer analysis/bot_ablation.py (Phase 3, Gate-Ablation).
    Default = leere Menge aendert nichts am Verhalten fuer jeden bestehenden
    Aufrufer (UI-Rücktest, Walk-Forward, alle Tests).

    `exit_variant` waehlt die Trailing-Stop-Regel - "reference" (Default,
    core.bot_signals.trailing_stop_px(), UNVERAENDERT der Live-Pfad) oder
    "min_profit_stop" (siehe EXIT_VARIANTS/_min_profit_stop_px() oben,
    AUSSCHLIESSLICH fuer analysis/bot_exit_variants.py). Wie bei
    `disabled_gates` aendert der Default nichts am Verhalten fuer jeden
    bestehenden Aufrufer."""
    if exit_variant not in EXIT_VARIANTS:
        return {"error": f"Unbekannte exit_variant '{exit_variant}' - erlaubt: {EXIT_VARIANTS}."}
    candles: dict = market["candles"]
    funding: dict = market.get("funding", {})
    hourly: dict = market.get("hourly", {})
    if not candles:
        return {"error": "Keine Kursdaten geladen - Rücktest nicht möglich."}

    # Ohne ausreichende BTC-Tagesdaten liefert _daily_btc_regime_series()
    # eine LEERE Reihe, regime_at() faellt fuer JEDEN Takt auf "neutral"
    # zurueck, und mit ALLOW_SHORT=False blockt Gate 1 dann JEDEN Kandidaten.
    # Das Ergebnis sah vorher aus wie ein sauberer 0-Trade-Lauf ("die
    # Strategie fand nichts"), obwohl in Wahrheit der Datenabruf fuer BTC
    # fehlgeschlagen oder zu kurz war - nicht von einem echten Nullbefund zu
    # unterscheiden. Ein Rücktest, der still das Falsche misst, ist
    # schlimmer als gar keiner: er sieht aus wie ein Beleg.
    _min_daily = bot_signals._REGIME_MIN_CANDLES + 1
    btc_daily = market.get("btc_daily")
    if btc_daily is None or btc_daily.empty or len(btc_daily) < _min_daily:
        got = 0 if btc_daily is None else len(btc_daily)
        return {"error": (f"BTC-Tagesdaten unzureichend für das Marktregime "
                          f"({got} von mindestens {_min_daily} Kerzen) - Rücktest "
                          f"nicht möglich. Ein 0-Trade-Ergebnis wäre hier nicht von "
                          f"einem echten Nullbefund zu unterscheiden."),
               "load_report": market.get("load_report", {})}

    # bot_config.limits_for_risk_level() - GENAU dieselbe Semantik wie
    # bot_limits() fuer den Live-Pfad (Bounds-Klammerung, Experten-Overrides,
    # feste Strategie-Konstanten), nur fuer eine frei waehlbare Stufe statt
    # der gerade aktiven (Phase 6, CLAUDE.md-Feedback "Rücktest live-
    # identisch machen"). Vorher liess derive_limits() allein gespeicherte
    # Experten-Overrides UNSICHTBAR aussen vor - der Rücktest testete damit
    # faktisch eine andere Konfiguration als die, die live tatsaechlich galt.
    effective_risk_level = risk_level if risk_level is not None else bot_config.risk_level()
    limits = bot_config.limits_for_risk_level(effective_risk_level)
    overridden = bot_config.overridden_keys()
    index = sorted(set().union(*[set(df.index) for df in candles.values()]))
    if len(index) <= BURN_IN_CANDLES:
        return {"error": f"Zu wenige Kerzen ({len(index)}) für den Burn-in "
                         f"von {BURN_IN_CANDLES}."}

    btc = candles.get(BENCHMARK_SYMBOL)
    daily_regime = _daily_btc_regime_series(market.get("btc_daily"))
    # Positionsindex je Symbol: das Fenster wird per iloc geschnitten
    # (konstante Zeit) statt per df.loc[:ts] (kopiert die wachsende Historie).
    positions_of = {s: {ts: i for i, ts in enumerate(df.index)} for s, df in candles.items()}
    # Indikatoren EINMAL je Symbol statt einmal je Takt. Zulaessig, weil alle
    # Indikatoren kausal sind (siehe bot_signals.evaluate's `ind`-Parameter);
    # das Ergebnis ist zahlengleich, die Laufzeit faellt von quadratisch auf
    # linear.
    indicators = {s: technical.add_indicators(df) for s, df in candles.items()}

    def window_of(symbol: str, ts):
        """Indikator-Fenster GENAU so, wie prepare() es im Live-Takt liefert:
        die letzten LOOKBACK_CANDLES Kerzen OHNE die laufende (i selbst).

        `.get(symbol)` statt `[symbol]` fuer den AEUSSEREN Zugriff: BTC ist
        das Referenzsymbol fuer Gate 5 (btc_roc_at unten), muss aber nicht
        zwingend Teil der gehandelten `candles` sein (z.B. ein Ruecktest ohne
        BTC in der Symbolliste) - ohne das wuerde jeder Takt mit einem
        KeyError abbrechen, statt Gate 5 mangels Daten korrekt zu blockieren."""
        i = positions_of.get(symbol, {}).get(ts)
        if i is None:
            return None
        return indicators[symbol].iloc[max(0, i - LOOKBACK_CANDLES + 1):i]

    def regime_at(ts) -> str:
        if daily_regime.empty:
            return "neutral"
        try:
            value = daily_regime.asof(ts)
        except (KeyError, ValueError):
            return "neutral"
        return value if isinstance(value, str) else "neutral"

    def btc_roc_at(ts) -> float | None:
        window = window_of(BENCHMARK_SYMBOL, ts)
        if window is None:
            return None
        r = bot_signals.readings(window)
        return r["roc_n"] if r else None

    state = _State(equity=starting_equity_usd, cash=starting_equity_usd,
                   equity_start_of_day=starting_equity_usd)

    loop_index = index[BURN_IN_CANDLES:]
    for i, ts in enumerate(loop_index):
        # naechster Entscheidungstakt (oder None am allerletzten) - siehe
        # _apply_funding()'s Docstring fuer das Warum statt eines hart
        # codierten ts+4h.
        next_ts = loop_index[i + 1] if i + 1 < len(loop_index) else None
        bars = {s: df.loc[ts] for s, df in candles.items() if ts in df.index}
        opens = {s: float(b["Open"]) for s, b in bars.items()}
        closes = {s: float(b["Close"]) for s, b in bars.items()}

        day = ts.date()
        if day != state.day:
            state.day = day
            state.trades_today = 0
            state.equity_start_of_day = _mark_to_market(state, opens)

        _apply_funding(state, ts, next_ts, funding, opens)
        btc_regime = regime_at(ts)

        # 1. Gaps am Kerzen-Open treffen den *vorherigen* Exchange-Stop,
        #    bevor der Live-Runner zu diesem Zeitpunkt ein Signal oder einen
        #    Trailing-Stop berechnen kann. Das ist strikt pessimistischer als
        #    den späteren, bereits nachgezogenen Stop rückwirkend zu benutzen.
        for pos in list(state.positions.values()):
            bar = bars.get(pos.symbol)
            if bar is None:
                continue
            bar_open = float(bar["Open"])
            gap_hit = (bar_open <= pos.stop_px if pos.side == "long"
                       else bar_open >= pos.stop_px)
            if gap_hit:
                fill_px = bar_open
                _close(state, pos, ts, fill_px, "stop")

        # 2. Aktive Ausstiege (Regimewechsel / MA-Kreuz / maximale Haltedauer)
        #    kennen wie live nur die VOR der laufenden Kerze abgeschlossenen
        #    Daten und handeln zum aktuellen Open.
        for pos in list(state.positions.values()):
            window = window_of(pos.symbol, ts)
            if window is None:
                continue
            reason = bot_signals.exit_signal(
                {"side": pos.side, "opened_at": pos.opened_at.isoformat()},
                None, limits, btc_regime=btc_regime, now=ts.to_pydatetime(), ind=window)
            if reason:
                _close(state, pos, ts, opens.get(pos.symbol, pos.entry_px), "signal")

        # 3. Stop-LEVEL nachziehen am Kerzenanfang mit gleichem Informations-
        #    stand wie der Live-Runner: vorherige, abgeschlossene 4h-Kerzen
        #    plus aktueller Open-Preis. NUR der Level wird hier gesetzt - OB
        #    er im weiteren Kerzenverlauf getroffen wird, prueft Schritt 5
        #    (NACH der Einstiegsentscheidung, siehe dessen Kommentar fuer das
        #    Warum - die alte Reihenfolge prüfte das ganze Kerzen-High/Low
        #    schon VOR der Einstiegsentscheidung, ein struktureller Lookahead).
        for pos in list(state.positions.values()):
            if exit_variant == "min_profit_stop":
                new_stop = _min_profit_stop_px(pos, opens.get(pos.symbol))
                if new_stop:
                    pos.stop_px = new_stop
                continue
            if exit_variant == "chandelier_v2_2":
                window = window_of(pos.symbol, ts)
                if window is None:
                    continue
                new_stop = _chandelier_stop_px_v2_2(pos, window, opens.get(pos.symbol))
                if new_stop:
                    pos.stop_px = new_stop
                continue
            if exit_variant == "hourly_trailing":
                # Level UND Treffer haengen hier von der stuendlichen Lage
                # INNERHALB der laufenden Kerze ab, die am Open noch nicht
                # feststeht - komplett Schritt 5 ueberlassen, sonst genau
                # derselbe Lookahead wie beim frueheren, jetzt entfernten
                # Schritt "4. Stop innerhalb des Rests der Kerze".
                continue
            window = window_of(pos.symbol, ts)
            if window is None:
                continue
            new_stop = bot_signals.trailing_stop_px(
                {"side": pos.side, "stop_px": pos.stop_px, "entry_px": pos.entry_px,
                 "initial_stop_px": pos.initial_stop_px, "opened_at": pos.opened_at.isoformat()},
                None, limits, opens.get(pos.symbol), ind=window)
            if new_stop:
                pos.stop_px = new_stop

        # 4. Einstieg - höchstens EINER je Takt, wie in core.bot.run_signal_cycle.
        #
        # EIN-BAR-LOOKAHEAD (gefunden bei der Phase-1-Haertung): ein Einstieg
        # fuellt zum OPEN dieser Kerze (siehe `fill = _slipped(entry_raw, ...)`
        # unten), aber `equity` oben ist bereits zum CLOSE DERSELBEN Kerze
        # bewertet - Positionsgroesse und alle Guards (position_size_usd,
        # evaluate_entry_order) saehen damit einen Kontostand, der die
        # Kursbewegung dieser Kerze BEREITS einpreist, obwohl diese Bewegung
        # zum Entscheidungszeitpunkt (Open) noch nicht stattgefunden hat. Bei
        # offenen Positionen kann das die Groesse einer NEUEN Order von einem
        # unrealisierten Gewinn/Verlust abhaengig machen, der live schlicht
        # noch nicht existiert. `entry_equity` bewertet stattdessen zum OPEN -
        # exakt der Kontostand, den core.bot.run_signal_cycle zum selben
        # Zeitpunkt tatsaechlich sehen wuerde. `equity`/state.equity/die
        # Kurve bleiben bewusst Close-basiert (das ist die korrekte
        # End-of-Bar-Bewertung fuer Chart/Metriken, kein Lookahead dort).
        entry_equity = _mark_to_market(state, opens)

        open_count = len(state.positions)
        btc_roc = btc_roc_at(ts)
        signals = []
        for symbol in candles:
            if symbol in state.positions:
                continue
            window = window_of(symbol, ts)
            if window is None:
                continue
            series = funding.get(symbol)
            rate = None
            if series is not None:
                try:
                    rate = float(series.asof(ts))
                except (KeyError, ValueError, TypeError):
                    rate = None
                if rate is not None and pd.isna(rate):
                    rate = None
            signals.append(bot_signals.evaluate(
                symbol, None, opens.get(symbol), rate, limits, btc_regime, btc_roc,
                volume_24h_usd=_UNLIMITED_VOLUME, ind=window,
                disabled_gates=disabled_gates))
        # Wiedereinstiegssperre - dieselbe reine Funktion wie live
        # (core.bot.reentry_block_reason), und wie live in einem eigenen
        # Durchgang ueber ALLE bewerteten Symbole, nicht nur bis zum ersten
        # Kandidaten.
        from core.bot import reentry_block_reason
        reentry_blocked = set()
        for sig in signals:
            dis = state.disarmed.get(sig.symbol)
            if dis is None:
                continue
            reason = reentry_block_reason(dis["at"], dis["side"], sig, ts.to_pydatetime())
            if reason is None:
                del state.disarmed[sig.symbol]
            else:
                reentry_blocked.add(sig.symbol)
                key = "Signal-Reset (Wiedereinstiegssperre)"
                state.blocked_reasons[key] = state.blocked_reasons.get(key, 0) + 1
        signals.sort(key=lambda s: s.score, reverse=True)

        for signal in signals:
            if signal.symbol in reentry_blocked:
                continue   # wie live: naechster Kandidat
            # `tradeable` ist der ALLEINIGE Gate-Entscheid seit der
            # V2-Signal-Engine (core.bot_signals.evaluate) - keine
            # Punktzahl-Schwelle mehr, siehe dessen Modul-Docstring. GENAU
            # wie core.bot.run_signal_cycle: ein nicht handelbares Signal
            # wird UEBERSPRUNGEN, nicht die ganze Suche beendet.
            if not signal.tradeable:
                if signal.blocked_by:
                    key = signal.blocked_by.split(" -")[0][:60]
                    state.blocked_reasons[key] = state.blocked_reasons.get(key, 0) + 1
                continue
            # ZU AKTUELLEN Kursen, nicht zum Einstiegsnominal - core.bot.
            # _open_notional_usd() fragt live den aktuellen mid_price ab.
            # Bei einer trendfolgenden Strategie kann die aktuelle Nominale
            # deutlich vom Einstiegswert abweichen, und ein eingefrorener
            # Wert wuerde das Gesamt-Exposure-Limit falsch einschaetzen.
            open_notional = sum((opens.get(p.symbol) or p.entry_px) * p.size
                                for p in state.positions.values())
            stop_dist = signal.stop_distance_pct
            notional, _ = bot_signals.position_size_usd(
                entry_equity, limits, MIN_NOTIONAL_USD, None, stop_distance_pct=stop_dist)
            if notional is None:
                break
            entry_raw = opens.get(signal.symbol)
            if not entry_raw:
                break
            fill = _slipped(entry_raw, signal.side, closing=False)
            stop_px = (fill * (1 - stop_dist / 100.0) if signal.side == "long"
                       else fill * (1 + stop_dist / 100.0))
            # GENAU dieselbe Funktion wie live (core.bot.run_signal_cycle) -
            # vorher stand hier fest 1.0, unabhaengig von der Risikostufe.
            from core.bot import _entry_leverage
            leverage = _entry_leverage(signal, limits)
            # Dieselben drei Guards wie live (core.bot.open_position,
            # Phase 5) - ohne sie waere der Ruecktest optimistischer als der
            # tatsaechliche Bot, der Portfolio-Heat, Richtungs-Konzentration
            # und Verlustserien seit Phase 5 aktiv sperrt.
            open_heat = sum(p.initial_risk_usd for p in state.positions.values())
            order_heat = notional * stop_dist / 100.0
            same_side_count = sum(1 for p in state.positions.values() if p.side == signal.side)
            consecutive_losses, last_loss_at = _loss_streak_state(state.trades)
            evaluation = bot_guards.evaluate_entry_order(
                kill_switch_active=False, live_trading_allowed=True,
                order_notional_usd=notional,
                max_reasonable_notional_usd=entry_equity * (limits["max_position_pct"] / 100.0) * 2.0,
                equity_usd=entry_equity,
                equity_start_usd=starting_equity_usd,
                equity_start_of_day_usd=state.equity_start_of_day,
                leverage=leverage, funding_rate_hourly=signal.funding_hourly,
                stop_px=stop_px, side=signal.side, entry_px=fill,
                current_open_count=open_count, is_new_symbol=True,
                last_closed_at=state.last_closed_at.get(signal.symbol),
                now=ts.to_pydatetime(), trades_today=state.trades_today, limits=limits,
                min_notional_usd=MIN_NOTIONAL_USD, equity_trusted=True,
                open_notional_usd=open_notional,
                order_heat_usd=order_heat, open_heat_usd=open_heat,
                same_side_open_count=same_side_count,
                consecutive_losses=consecutive_losses, last_loss_closed_at=last_loss_at)
            if not evaluation.allowed:
                key = evaluation.failed_checks[0][:60]
                state.blocked_reasons[key] = state.blocked_reasons.get(key, 0) + 1
                break

            size = notional / fill
            fee = notional * TAKER_FEE
            state.cash -= fee
            state.fees_usd += fee
            state.slippage_usd += abs(fill - entry_raw) * size
            state.trades_today += 1
            new_pos = _Position(
                symbol=signal.symbol, side=signal.side, size=size, entry_px=fill,
                stop_px=stop_px, initial_stop_px=stop_px, opened_at=ts,
                initial_risk_usd=abs(fill - stop_px) * size,
                fees_usd=fee, notional_at_entry=notional, regime=btc_regime)
            state.positions[signal.symbol] = new_pos
            # Kein sofortiger Eroeffnungskerzen-Stop-Check mehr hier - Schritt
            # 6 unten prueft JEDE offene Position (Bestand UND diese neue)
            # einheitlich fuer den Rest der Kerze, siehe dessen Kommentar.
            break

        # 5. Stop-Treffer fuer den REST der Kerze - fuer JEDE jetzt offene
        #    Position (Bestand aus Schritt 1-3 UND ein ggf. eben in Schritt 4
        #    NEU eroeffneter Trade), in EINEM einheitlichen Durchgang NACH der
        #    Einstiegsentscheidung.
        #
        #    WARUM ERST HIER (gefunden von einer externen Pruefung,
        #    13.09.2026): die Einstiegsentscheidung in Schritt 4 faellt am
        #    OPEN dieser Kerze - open_count/entry_equity/Heat/Guards duerfen
        #    deshalb nur Ereignisse bis einschliesslich Schritt 1-3 kennen.
        #    Der fruehere Schritt 4 (volles Kerzen-High/Low) lief VOR der
        #    Einstiegsentscheidung und liess einen Slot, der ERST SPAETER im
        #    Kerzenverlauf durch einen Stop frei wurde, schon am Open zur
        #    Verfuegung stehen - ein struktureller Lookahead (reproduziert:
        #    identische Vergangenheit, nur ein spaeteres Tief aendert eine
        #    FRUEHERE Kaufentscheidung). Bestandspositionen bekommen hier
        #    jetzt dieselbe _entry_candle_stop()-Praezision (1h-Unterkerzen
        #    mit 4h-Bar-Fallback) wie bisher schon Neueinstiege allein.
        for pos in list(state.positions.values()):
            if exit_variant == "hourly_trailing":
                # Level UND Treffer wurden in Schritt 3 bewusst uebersprungen
                # (siehe dortiger Kommentar) - beides passiert hier, in
                # derselben Verzweigung wie frueher in Schritt 3, nur
                # zeitlich NACH statt VOR der Einstiegsentscheidung.
                hourly_df = hourly.get(pos.symbol)
                window = window_of(pos.symbol, ts)
                if hourly_df is not None and not hourly_df.empty and window is not None:
                    new_stop, hit = _hourly_trailing_stop_px(
                        pos, ts, window, hourly_df, opens.get(pos.symbol))
                    if new_stop is not None:
                        pos.stop_px = new_stop
                else:
                    if window is not None:
                        # Kein Vorteil ohne Unterkerzen fuer DIESES Symbol -
                        # dieselbe Referenz-Formel wie der reference-Zweig,
                        # Treffer unten wie jede andere Position.
                        new_stop = bot_signals.trailing_stop_px(
                            {"side": pos.side, "stop_px": pos.stop_px, "entry_px": pos.entry_px,
                             "initial_stop_px": pos.initial_stop_px,
                             "opened_at": pos.opened_at.isoformat()},
                            None, limits, opens.get(pos.symbol), ind=window)
                        if new_stop:
                            pos.stop_px = new_stop
                    hit = _entry_candle_stop(pos, ts, bars.get(pos.symbol), hourly.get(pos.symbol))
            else:
                hit = _entry_candle_stop(pos, ts, bars.get(pos.symbol), hourly.get(pos.symbol))
            if hit:
                hit_ts, hit_px = hit
                _close(state, pos, hit_ts, hit_px, "stop")

        equity = _mark_to_market(state, closes)
        state.equity = equity
        state.equity_curve.append((ts, equity))

    # Am Ende offene Positionen zum letzten Kurs glattstellen, sonst zählt ein
    # zufällig offener Gewinner voll und ein Verlierer gar nicht.
    final_ts = index[-1]
    final_closes = {s: float(df["Close"].iloc[-1]) for s, df in candles.items()}
    for pos in list(state.positions.values()):
        _close(state, pos, final_ts, final_closes.get(pos.symbol, pos.entry_px), "backtest_ende")
    state.equity = state.cash
    state.equity_curve.append((final_ts, state.cash))

    curve = pd.Series(dict(state.equity_curve)).sort_index()
    return {
        "trades": state.trades,
        "equity_curve": curve,
        "metrics": _metrics(state, starting_equity_usd, curve),
        "by_side": _group(state.trades, "side"),
        "by_regime": _group(state.trades, "regime"),
        "benchmarks": _benchmarks(candles, curve, starting_equity_usd, index[BURN_IN_CANDLES]),
        "blocked_reasons": dict(sorted(state.blocked_reasons.items(),
                                       key=lambda kv: kv[1], reverse=True)[:8]),
        "risk_level": effective_risk_level,
        "limits": limits,
        # Welche Limits gerade vom Regler abweichen (core.bot_config.
        # overridden_keys()) - der Rücktest uebernimmt seit Phase 6 dieselben
        # gespeicherten Experten-Overrides wie der Live-Pfad; ohne diese
        # Beschriftung waere im Report nicht erkennbar, dass ein Ergebnis
        # NICHT die reinen Regler-Standardwerte der gewaehlten Stufe zeigt.
        "overridden_limit_keys": overridden,
        "period": (index[BURN_IN_CANDLES], index[-1]),
        "candles_per_symbol": {s: len(df) for s, df in candles.items()},
        "hourly_candles_per_symbol": {s: len(df) for s, df in hourly.items()},
        "missing_funding": market.get("missing_funding", []),
        "load_report": market.get("load_report", {}),
    }


def _max_drawdown_pct(curve: pd.Series) -> float | None:
    if curve.empty:
        return None
    peak = curve.cummax()
    return float(((curve - peak) / peak).min() * 100)


def _metrics(state: _State, start: float, curve: pd.Series) -> dict:
    trades = state.trades
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] <= 0]
    gross_win = sum(t["net_pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["net_pnl_usd"] for t in losses))
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {
        "start_usd": start,
        "end_usd": float(state.equity),
        "return_pct": (state.equity / start - 1) * 100 if start else None,
        "trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else None,
        # Profit-Faktor ist bei Trendfolge aussagekräftiger als die
        # Trefferquote: die Strategie lebt von wenigen grossen Gewinnern.
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "max_drawdown_pct": _max_drawdown_pct(curve),
        "fees_usd": state.fees_usd,
        "funding_usd": state.funding_usd,
        "slippage_usd": state.slippage_usd,
        "costs_usd": state.fees_usd + max(state.funding_usd, 0.0) + state.slippage_usd,
    }


def _group(trades: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for t in trades:
        bucket = out.setdefault(str(t.get(key)), {"trades": 0, "net_pnl_usd": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["net_pnl_usd"] += t["net_pnl_usd"]
        bucket["wins"] += 1 if t["net_pnl_usd"] > 0 else 0
    for bucket in out.values():
        bucket["win_rate_pct"] = bucket["wins"] / bucket["trades"] * 100
    return out


def _ma_trend_benchmark(btc: pd.DataFrame, start: float, period_start) -> float | None:
    """Ein-Zeilen-Trendfolge auf BTC: investiert, solange der Kurs über dem
    MA50 (auf 4h-Basis) liegt. Der Maßstab, an dem sich ein vielteiliger
    Score messen lassen muss - schlägt er das nicht, trägt die zusätzliche
    Komplexität nichts.

    Der MA50 wird über die VOLLE Historie berechnet (er braucht 50 echte
    Vorlauf-Kerzen, genau wie beim Bot der Burn-in), das Aufzinsen der
    Rendite beginnt aber erst bei `period_start` - sonst vergliche sich der
    Bot gegen einen Zeitraum, der Wochen vor seinem eigenen Start beginnt."""
    if btc is None or len(btc) < 60:
        return None
    close = btc["Close"]
    ma50 = close.rolling(50).mean()
    # shift(1): die Entscheidung für Kerze t fällt mit den Daten von t-1.
    invested = (close > ma50).shift(1, fill_value=False).astype(bool)
    returns = close.pct_change().fillna(0.0)
    returns = returns.where(returns.index >= period_start, 0.0)
    equity = start * (1 + returns.where(invested, 0.0)).cumprod()
    return float(equity.iloc[-1])


def _benchmarks(candles: dict, curve: pd.Series, start: float, period_start) -> dict:
    """`period_start` ist der ERSTE Zeitpunkt, den der Bot-Rücktest selbst
    bewertet (Burn-in bereits abgezogen) - beide Benchmarks starten AB DORT,
    sonst verglichen sie sich gegen einen längeren, meist güngstigeren
    Zeitraum als der Bot ihn hatte."""
    btc = candles.get(BENCHMARK_SYMBOL)
    hold = None
    if btc is not None:
        aligned = btc.loc[btc.index >= period_start, "Close"]
        if len(aligned) >= 2:
            hold = float(start * (aligned.iloc[-1] / aligned.iloc[0]))
    return {
        "btc_buy_hold_usd": hold,
        "btc_ma50_trend_usd": _ma_trend_benchmark(btc, start, period_start),
        "bot_usd": float(curve.iloc[-1]) if len(curve) else None,
    }
