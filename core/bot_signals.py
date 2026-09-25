"""Trading-Bot: deterministische Signal-Engine V2 - 4h-Swing mit Tagesregime.

WARUM V2: Der Ruecktest von V1 (1h-Scoring, Punkte-Schwelle als einziges
Einstiegskriterium) sagte ueber sechs Monate klar NEIN zur Strategie
(-38,0 % gegen BTC-Halten +6,8 %, 997 Trades, Trefferquote 33 %). Die
CLAUDE.md-Feedback-Analyse fand sechs zusammenhaengende Ursachen: der
Risikoregler veraenderte unbemerkt die Strategie statt nur das Risiko
(siehe core/bot_config.py), der Bot handelte mehrfach auf dieselbe
stuendliche Information (siehe core/bot.py: Entscheidungstakt jetzt an eine
neu geschlossene 4h-Kerze gebunden), der Score massss keine nachgewiesene
Erfolgswahrscheinlichkeit (Scores ab 70 brachten NUR 3 Gewinner aus 16
Trades), der Bot war faktisch ein Stop-out-System (nur 2 von 33 Trades
erreichten +1R), Short und Long wurden zu symmetrisch behandelt (84 % des
Verlusts kam von Shorts), und der alte Ruecktest war nicht live-identisch.
V2 antwortet auf die ersten vier Punkte direkt in diesem Modul; die letzten
beiden sind in core/bot.py (Signal-Reset, Phase 3) bzw. analysis/
bot_backtest.py (Phase 6) geloest bzw. werden dort geloest.

DIE ZENTRALE VERSCHIEBUNG: Bis V1 entschied ein PUNKTESTAND, OB gehandelt
wird ("Score >= Schwelle"). Das zwingt eine Kalibrierungsfrage, die niemand
beantworten kann, ohne sie zu erfinden: WELCHE Schwelle ist richtig? V2
trennt das in zwei unabhaengige Fragen. ERSTENS: Qualifiziert sich das
Symbol UEBERHAUPT (Pflichtbedingungen als harte UND-Gatter, siehe
evaluate())? Ein Gate ist wahr oder falsch, keine Kalibrierung noetig.
ZWEITENS: Welcher von mehreren qualifizierten Kandidaten wird zuerst
genommen (score_side(), reine Rangfolge)? Eine Rangfolge muss nicht
kalibriert sein, nur konsistent - score_side() ist deshalb bewusst
groesstenteils die alte Bewertung, nur ohne ihre fruehere Rolle als
Schwelle.

WARUM TAGESREGIME + 4h STATT 1h: 1h-Indikatoren reagieren auf Rauschen, das
innerhalb eines geplanten Halte-Horizonts von 3-14 Tagen bedeutungslos ist -
das erklaert die hohe Handelsfrequenz (997 Trades/6 Monate) und die
Stop-out-Statistik oben. regime() liest die TAGES-Struktur (bewusst nur
BTC als Marktanker, nicht je Symbol - ein Long in ETH gegen ein fallendes
BTC-Tagesregime ist ein Wetten gegen den staerksten Markttreiber). Die
eigentlichen Signale werden auf 4h-Kerzen bewertet: genug Abstand zum
15-Minuten-Sicherheitstakt (core.bot.run_deterministic_cycle, Phase 3),
kurz genug fuer einen 3-14-Tage-Horizont.

Bewusst reine Funktionen ohne DB-/Streamlit-Import (dasselbe Prinzip wie
core/bot_guards.py): Kerzen, Preise, Funding und Marktkontext (Regime,
BTC-Momentum, Liquiditaet) kommen als Parameter herein, damit jede Regel mit
synthetischen Kursreihen testbar ist, ohne Netz oder DB. Ausgefuehrt wird
hier NICHTS - core/bot.py uebersetzt Signale in Orders und
core/bot_guards.py entscheidet dort, was tatsaechlich durchgeht.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analysis import technical
from core import clock

# Version des Scorings/der Gates. Von HAND erhoehen, sobald sich an
# score_side(), evaluate(), regime() oder einem der Gates etwas aendert -
# ohne sie sind Zeilen aus bot_signal_log, die zu verschiedenen Code-
# Staenden gehoeren, spaeter nicht mehr trennbar. "2.0-swing4h" markiert den
# Bruch mit dem 1h-Punkte-Schwellen-Modell (V1, siehe core/db.py-Migration
# _migrate_bot_config_hash und das git-Tag bot-v1). "2.2-swing4h" (Phase 3/4,
# Gate-Ablation): Gates 3/4/5/6 entfernt, siehe der Konstanten-Block bei
# "PFLICHTBEDINGUNGEN" fuer die Messung dahinter.
#
# "2.3-swing4h" (12.09.2026, Ausstiegsmechanik-Analyse): trailing_stop_px()
# auf den Mindestgewinn-Stop umgestellt, siehe dessen Docstring fuer die
# vollstaendige Begruendung/Messung (analysis/bot_exit_variants.py, drei
# unabhaengige Walk-Forward-Laeufe). Erhoeht bewusst auch den
# core.bot_config.config_fingerprint()-Hash, damit ein bestehendes
# Zertifikat/Signal-Log als zu einer aelteren Ausstiegsregel gehoerig
# erkennbar bleibt.
STRATEGY_VERSION = "2.3-swing4h"

# Mindestzahl abgeschlossener 4h-Kerzen fuer ein belastbares Signal: MA50 UND
# das Trend-Effizienz-/Momentum-Fenster (TREND_WINDOW, 20 Kerzen) brauchen
# Vorlauf, sonst bewertet die Engine Rauschen.
MIN_CANDLES = 90

# --- STRATEGIE-KONSTANTEN (frueher Teil des 1-10-Risikoreglers) ---
#
# WARUM HIER STATT IM REGLER: core.bot_config._RISK_ANCHORS steuert seit dem
# Umbau NUR NOCH Positionsgroesse, Hebel, Exposure und Verlustgrenzen - nie
# Signalqualitaet, Stoplogik oder Handelsfrequenz. Diese Werte sind deshalb
# feste Konstanten: aenderbar nur per Code-Aenderung (git diff), nie per
# Regler oder Experten-Override - core.bot_config.bot_limits() fuehrt sie
# unveraendert in das Limits-Dict ein (core.bot_config.strategy_constants()).
#
# ENTRY_SCORE_MIN ist mit V2 ERSATZLOS gestrichen (nicht nur umbenannt): der
# Score entscheidet nicht mehr, OB gehandelt wird (siehe Modul-Docstring) -
# eine Schwelle dafuer waere bedeutungslos.
INITIAL_STOP_ATR_MULT = 2.75
MAX_HOLD_HOURS = 336.0   # 14 Tage - siehe Modul-Docstring (3-14 Tage Zielhorizont)
SYMBOL_COOLDOWN_HOURS = 8.0   # harte Untergrenze; core.bot._reentry_check() (Phase 3) verlangt zusaetzlich ein nachweislich totes Setup
MAX_TRADES_PER_DAY = 6
FUNDING_BUDGET_HOURLY = 0.00005
# KI-Aufsichts-Takt (agents/trader.py) lebt NICHT hier: er ist eine eigene,
# per Schalter in den Einstellungen wählbare Kosten-/Frequenz-Einstellung
# (core.bot_config.ai_report_interval_hours()), keine Strategie-Konstante -
# anders als alles hier beeinflusst er keine einzige Handelsentscheidung.
# Deterministischer Ersatz fuer das, was bisher implizit die KI haette
# leisten sollen (649 bot_decisions, aber nur 1 Veto - siehe CLAUDE.md):
# core.bot_guards.check_loss_streak() sperrt neue Einstiege bis zur naechsten
# Tageskerze (UTC), sobald so viele BOT-Positionen (origin='bot') in Folge
# mit Verlust geschlossen wurden.
LOSS_STREAK_LIMIT = 4

# Marktanker fuer das Tagesregime (Gate 1) - dieselbe Konstante wie
# analysis.bot_backtest.BENCHMARK_SYMBOL. Diente bis STRATEGY_VERSION
# "2.1-swing4h" zusaetzlich als Referenz fuer Gate 5 (relative Staerke,
# seither entfernt, siehe Konstanten-Block bei "PFLICHTBEDINGUNGEN").
BENCHMARK_SYMBOL = "BTC"

# --- MARKTREGIME (core.bot_signals.regime(), Tageskerzen BTC) ---
#
# BEWUSST NUR BTC, nicht je Symbol: das Tagesregime beschreibt die
# uebergeordnete Marktrichtung, gegen die ein einzelnes Symbol-Long/-Short
# sonst eine verdeckte zusaetzliche Wette waere.
REGIME_MA_PERIOD = 50
# Vergleichsabstand fuer "steigend/fallend": das MA50 von HEUTE gegen das
# MA50 von vor REGIME_SLOPE_LOOKBACK_DAYS - eine reine Tag-zu-Tag-Differenz
# waere zu verrauscht (das MA selbst glaettet zwar den Kurs, aber nicht
# zwangslaeufig seine eigene erste Ableitung).
REGIME_SLOPE_LOOKBACK_DAYS = 5
_REGIME_MIN_CANDLES = REGIME_MA_PERIOD + REGIME_SLOPE_LOOKBACK_DAYS + 5
# Oeffentlich (nicht _-prefixed): core.bot._current_btc_regime() und
# scan() unten sollen denselben Lookback verwenden, ohne dass core.bot ein
# privates Modul-Attribut anfassen muss.
REGIME_LOOKBACK_HOURS = (_REGIME_MIN_CANDLES + 30) * 24

# Shorts bleiben komplett aus, bis eigens entwickelt und validiert - siehe
# CLAUDE.md-Feedback: Shorts verursachten ~84 % des V1-Strategieverlusts,
# und ein Short in einem uebergeordnet steigenden BTC-Tagesregime braucht
# grundsaetzlich strengere Voraussetzungen als ein trendkonformer Long.
# regime() selbst bleibt trotzdem symmetrisch (siehe dessen Docstring): es
# beschreibt die MARKTLAGE, nicht die Handelsentscheidung.
ALLOW_SHORT = False

# --- PFLICHTBEDINGUNGEN (Gate 2, Gate 7, siehe evaluate()) ---
#
# Gates 3 (Trend-Effizienz), 4 (Momentum vs. ATR) und 6 (Volumen) sind seit
# STRATEGY_VERSION "2.2-swing4h" ENTFERNT, nicht nur deaktiviert (Phase 3/4,
# Gate-Ablation): eine Out-of-Sample-Messung ueber 4x45-Tage-Fenster (18
# Symbole, Risikostufe 9) zeigte, dass ihr Entfernen den Erwartungswert NICHT
# messbar verschlechtert - Gate 3 verbesserte Ø R sogar um +0,142 (Profit-
# Faktor +19,9 %) ohne. Gate 5 (relative Staerke zu BTC) zeigte dasselbe Bild
# (Ø R +0,021, PF +7,4 % ohne) und ist ebenfalls entfernt. Nur Gate 2 (Kurs/
# MA20/MA50-Struktur) verdiente seine Komplexitaet messbar (Ø R -0,082, PF
# -17,7 % ohne) und bleibt. Gate 7 (Liquiditaet) konnte die Ablation nicht
# pruefen - der Ruecktest kennt keine historische 24h-Volumen-Reihe und
# rechnet dort immer mit unendlichem Volumen (siehe analysis/bot_backtest.py)
# - und bleibt aus Risiko-, nicht aus Ablations-Gruenden bestehen. Die
# Luecke in der Gate-Nummerierung (2, dann 7) ist ABSICHTLICH: historische
# bot_signal_log-Zeilen und CLAUDE.md referenzieren die ursprüngliche
# Nummerierung, ein Umnummerieren wuerde die Historie nur verwirrender
# machen, ohne etwas Funktionales zu gewinnen.
#
# TREND_WINDOW bleibt (roc_n fuer Momentum-SCORING in score_side() braucht es
# weiterhin, nur nicht mehr als Gate).
TREND_WINDOW = 20

# Liquiditaet: Hyperliquid dayNtlVlm (data.hyperliquid.day_notional_volume_usd).
# AUSGANGSWERT, zu kalibrieren im Ruecktest.
MIN_24H_VOLUME_USD = 5_000_000.0

# ATR-Band, in dem ein 4h-Markt handelbar ist - deutlich hoeher als V1s
# 1h-Werte (0,25 %/4,0 %), weil eine 4h-Kerze naturgemaess mehr Bewegung
# einsammelt als eine 1h-Kerze. AUSGANGSWERT, zu kalibrieren im Ruecktest.
_ATR_PCT_MIN = 0.6
_ATR_PCT_MAX = 10.0

# --- STOP & TRAILING ---
#
# Fester Prozent-Boden (V1: 0,8 %) entfaellt zugunsten eines Vielfachen der
# TATSAECHLICHEN Round-Trip-Kosten (2x TAKER_FEE + PAPER_SLIPPAGE_PCT, siehe
# data/hyperliquid.py, ca. 0,19 %) - ein Stop naeher als das Dreifache davon
# waere ein Stop, der im Mittel schon an Gebuehren/Slippage haengen bleibt,
# bevor ueberhaupt Kursbewegung dazukommt.
_ROUND_TRIP_COST_PCT_ESTIMATE = 0.19
_STOP_PCT_FLOOR_MULT = 3.0
_STOP_PCT_CEILING = 30.0

# Trailing greift erst, nachdem der unrealisierte Gewinn mindestens das
# urspruengliche Risiko (R = |Einstieg - Anfangsstop|) erreicht hat - bis
# dahin gilt ausschliesslich der Anfangsstop. V1 zog jeden Takt sofort nach,
# ohne dass der Trade je im Plus gewesen sein musste (CLAUDE.md-Feedback:
# nur 2 von 33 Trades erreichten ueberhaupt +1R).
TRAILING_ACTIVATION_R = 1.0
# Chandelier-Abstand vom hoechsten Hoch (Long) bzw. tiefsten Tief (Short)
# SEIT EROEFFNUNG - seit STRATEGY_VERSION "2.3-swing4h" NICHT MEHR von
# trailing_stop_px() verwendet (siehe dessen Docstring: durch den
# Mindestgewinn-Stop ersetzt). Bewusst NICHT geloescht: analysis.
# bot_backtest._hourly_trailing_stop_px() (die im Vergleich unterlegene,
# aber als Werkzeug erhaltene Alternative "stuendliches Nachziehen") nutzt
# ihn weiterhin fuer eine moegliche kuenftige Messung mit echter
# stuendlicher ATR.
CHANDELIER_ATR_MULT = 3.0


@dataclass(frozen=True)
class Signal:
    """Bewertung EINES Symbols. `side` folgt dem Tagesregime (siehe
    evaluate()); `score` ist eine reine RANGFOLGE unter bereits
    qualifizierten Kandidaten, keine Schwelle. `blocked_by` erklaert, warum
    trotz Punktzahl kein Einstieg entsteht - genau diese Erklaerung fehlte
    vor V1 komplett und ist der Grund, warum "der Bot macht nichts" nicht
    diagnostizierbar war."""
    symbol: str
    side: str | None = None
    score: float = 0.0
    long_score: float = 0.0
    short_score: float = 0.0
    price: float | None = None
    atr_pct: float | None = None
    stop_distance_pct: float | None = None
    funding_hourly: float | None = None
    reasons: list[str] = field(default_factory=list)
    blocked_by: str | None = None
    components: dict = field(default_factory=dict)
    # Rohwerte hinter dem Score. Bewusst NICHT in as_row(): dort wuerde es
    # bot_decisions.signals_json bei jedem Takt aufblaehen. Ziel ist
    # core.db.bot_signal_log, das genau diese Rohdaten sammeln soll.
    readings: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.side is not None and self.blocked_by is None

    def as_row(self) -> dict:
        """Kompakte, JSON-faehige Form fuer bot_decisions.signals_json und die
        UI-Tabelle - bewusst ohne DataFrame, damit dieses Modul pandas nur
        indirekt (ueber analysis.technical) braucht."""
        return {"symbol": self.symbol, "side": self.side, "score": round(self.score, 1),
                "long": round(self.long_score, 1), "short": round(self.short_score, 1),
                "atr_pct": round(self.atr_pct, 2) if self.atr_pct is not None else None,
                "stop_pct": round(self.stop_distance_pct, 2) if self.stop_distance_pct else None,
                "funding": self.funding_hourly, "components": self.components,
                "blocked_by": self.blocked_by, "reasons": self.reasons}


# --- KERZEN-AUFBEREITUNG ---

def prepare(df):
    """Indikatoren auf abgeschlossene Kerzen rechnen.

    Die letzte Kerze von data.hyperliquid.candles_df() laeuft noch. Wuerde sie
    mitbewertet, haetten zwei Ticks desselben 4h-Fensters unterschiedliche
    Signale. Deshalb faellt sie hier weg, bevor irgendetwas berechnet wird -
    unveraendert dasselbe Prinzip fuer 4h- wie zuvor fuer 1h-Kerzen, UND fuer
    die Tageskerzen in regime()."""
    if df is None or len(df) < MIN_CANDLES + 1:
        return None
    closed = df.iloc[:-1]
    return technical.add_indicators(closed)


def _last(series):
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None


def readings(ind) -> dict | None:
    """Rohwerte EINES Symbols auf 4h-Basis, aus denen Score UND Gates
    entstehen.

    `volume`/`volume_ma20`/`efficiency` sind seit STRATEGY_VERSION
    "2.2-swing4h" ENTFERNT (Phase 3/4, Gate-Ablation) - sie dienten
    ausschliesslich den entfernten Gates 3/6, score_side() nutzt keins von
    beiden. Weiterberechnen ohne jeden Verbraucher waere genau die
    Komplexitaet-ohne-Beleg, die die Ablation aufdecken sollte."""
    if ind is None or len(ind) < MIN_CANDLES:
        return None
    close = ind["Close"].dropna()
    if len(close) < MIN_CANDLES:
        return None
    price = float(close.iloc[-1])
    if price <= 0:
        return None
    atr = _last(ind["ATR"]) if "ATR" in ind else None
    macd_hist_series = ind["MACD_Hist"].dropna()
    macd_hist = float(macd_hist_series.iloc[-1]) if not macd_hist_series.empty else None
    macd_prev = float(macd_hist_series.iloc[-2]) if len(macd_hist_series) >= 2 else None
    roc_n = None
    if len(close) > TREND_WINDOW:
        roc_n = (price / float(close.iloc[-1 - TREND_WINDOW]) - 1) * 100
    return {
        "candle_ts": str(ind.index[-1]) if len(ind.index) else None,
        "price": price,
        "ma20": _last(ind["MA20"]),
        "ma50": _last(ind["MA50"]),
        "rsi": _last(ind["RSI"]),
        "macd_hist": macd_hist,
        "macd_hist_prev": macd_prev,
        "atr_pct": (atr / price * 100) if atr else None,
        "roc_n": roc_n,
    }


def _ramp(value: float, lo: float, hi: float) -> float:
    """0 unterhalb `lo`, 1 ab `hi`, linear dazwischen."""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


# --- MARKTREGIME ---

def regime(btc_daily_df) -> str:
    """Tagesregime aus BTC-Tageskerzen: 'long', wenn der Kurs UEBER dem
    MA50(1d) liegt UND das MA50 selbst steigt; 'short' spiegelbildlich;
    sonst 'neutral' (kein neuer Einstieg, siehe evaluate()). Bewusst
    symmetrisch fuer long/short - ALLOW_SHORT schaltet die Short-Seite
    separat in evaluate() ab, damit regime() die Marktlage ehrlich
    beschreibt, statt sie an die aktuelle Handelsfreigabe anzupassen.

    Nutzt prepare() wie jedes andere Symbol - die aktuelle (unvollstaendige)
    Tageskerze darf das Regime nicht mitbestimmen, sonst wechselt es
    innerhalb desselben Kalendertags je nach Abrufzeitpunkt."""
    ind = prepare(btc_daily_df)
    if ind is None or len(ind) < _REGIME_MIN_CANDLES:
        return "neutral"
    close = ind["Close"].dropna()
    ma50 = ind["MA50"].dropna()
    if close.empty or len(ma50) <= REGIME_SLOPE_LOOKBACK_DAYS:
        return "neutral"
    price = float(close.iloc[-1])
    current_ma = float(ma50.iloc[-1])
    prior_ma = float(ma50.iloc[-1 - REGIME_SLOPE_LOOKBACK_DAYS])
    if price > current_ma and current_ma > prior_ma:
        return "long"
    if price < current_ma and current_ma < prior_ma:
        return "short"
    return "neutral"


# --- RICHTUNGS-SCORING (0-100, reine Rangfolge - siehe Modul-Docstring) ---

def score_side(r: dict, side: str, funding_hourly: float | None,
              hold_hours: float = MAX_HOLD_HOURS) -> tuple[float, list[str], dict]:
    """Rang-Punktzahl fuer EINE Richtung. Entscheidet NICHT mehr, OB
    gehandelt wird - das leisten seit V2 die Pflichtbedingungen in
    evaluate(). Gewichte wie in V1: Richtung (35) und Momentum (35) tragen
    am meisten, MACD bestaetigt (18), RSI verfeinert (12) - dieselbe
    Trendfolge-These, jetzt nur noch fuer die Reihenfolge unter bereits
    qualifizierten Kandidaten statt fuer die Zulassung selbst."""
    sign = 1.0 if side == "long" else -1.0
    reasons: list[str] = []
    trend = momentum = confirmation = rsi_points = funding_points = 0.0

    # 1. Richtung (max 35): Lage zum MA50 und Struktur MA20 vs MA50.
    price, ma20, ma50 = r["price"], r["ma20"], r["ma50"]
    if ma50:
        # ZWEI Groessen, bewusst getrennt: `raw_above` ist die tatsaechliche Lage
        # zum MA50 (negativ = darunter), `above` dieselbe Zahl in Richtung der
        # Wette gedreht. Der Text MUSS `raw_above` nutzen - `above` ist bei einem
        # Short immer dann positiv, wenn der Kurs UNTER dem MA50 liegt.
        raw_above = (price - ma50) / ma50 * 100
        above = raw_above * sign
        gained = 20.0 * _ramp(above, 0.0, 2.5)
        trend += gained
        if gained >= 13:
            reasons.append(f"Kurs {abs(raw_above):.1f} % "
                           f"{'über' if raw_above > 0 else 'unter'} MA50")
    if ma20 and ma50:
        spread = (ma20 - ma50) / ma50 * 100 * sign
        gained = 15.0 * _ramp(spread, 0.0, 1.5)
        trend += gained
        if gained >= 10:
            reasons.append("MA20/MA50 bestätigen die Richtung")

    # 2. Momentum (max 35): EIN Fenster (TREND_WINDOW Kerzen) statt V1s
    # zweier unabhaengiger Fenster (roc24/roc72) - dasselbe Fenster, das bis
    # STRATEGY_VERSION "2.1-swing4h" auch den seither entfernten Gates
    # 3/4/5 zugrunde lag.
    if r["roc_n"] is not None:
        gained = 35.0 * _ramp(r["roc_n"] * sign, 0.0, 8.0)
        momentum += gained
        if gained >= 20:
            reasons.append(f"{TREND_WINDOW}-Kerzen-Momentum {r['roc_n']:+.1f} %")

    # 3. Bestaetigung (max 18): MACD-Histogramm im Vorzeichen der Richtung,
    # zusaetzlich Punkte, wenn es sich weiter in diese Richtung ausdehnt.
    hist, prev = r["macd_hist"], r["macd_hist_prev"]
    if hist is not None and hist * sign > 0:
        confirmation += 12.0
        if prev is not None and (hist - prev) * sign > 0:
            confirmation += 6.0
            reasons.append("MACD dehnt sich in Richtung aus")

    # 4. RSI als FORTSETZUNGS-Band, nicht als Veto: Ein Long darf und soll
    # bei RSI 60-70 laufen; abgestraft wird erst echte Ueberdehnung ab ~82.
    rsi = r["rsi"]
    if rsi is not None:
        directional = rsi if side == "long" else 100 - rsi
        if directional < 45:
            rsi_points = 0.0
        elif directional <= 72:
            rsi_points = 12.0
        elif directional <= 82:
            rsi_points = 6.0
        else:
            rsi_points = -8.0
            reasons.append(f"RSI {rsi:.0f} überdehnt - Abschlag")

    # 5. Funding richtungsabhaengig: fuer Longs eine Kostenposition, fuer
    # Shorts ein Ertrag.
    if funding_hourly is not None:
        cost_pct = funding_hourly * hold_hours * 100 * sign
        funding_points = -min(10.0, max(-10.0, cost_pct * 4.0))
        if abs(funding_points) >= 3:
            reasons.append(f"Funding {'kostet' if funding_points < 0 else 'bringt'} "
                           f"~{abs(cost_pct):.2f} % über {hold_hours:.0f} h")

    total = trend + momentum + confirmation + rsi_points + funding_points
    components = {"trend": trend, "momentum": momentum, "confirmation": confirmation,
                  "rsi": rsi_points, "funding": funding_points}
    return min(100.0, max(0.0, total)), reasons, components


def stop_distance_pct(atr_pct: float | None, limits: dict) -> float | None:
    """Stop-Abstand als Vielfaches des ATR statt als fester Prozentwert: In
    einem ruhigen Markt liegt der Stop damit eng, in einem hektischen weit
    genug, um nicht vom normalen Rauschen ausgeloest zu werden. Boden = ein
    Vielfaches der TATSAECHLICHEN Round-Trip-Kosten (siehe
    _ROUND_TRIP_COST_PCT_ESTIMATE oben), Obergrenze verhindert einen Stop,
    der ein einzelner Treffer das Tagesverlust-Limit reissen liesse."""
    if not atr_pct or atr_pct <= 0:
        return None
    raw = atr_pct * float(limits["atr_stop_mult"])
    floor = _ROUND_TRIP_COST_PCT_ESTIMATE * _STOP_PCT_FLOOR_MULT
    return min(_STOP_PCT_CEILING, max(floor, raw))


def position_size_usd(equity_usd: float, limits: dict, min_notional_usd: float,
                      cap_usd: float | None = None,
                      stop_distance_pct: float | None = None) -> tuple[float | None, str | None]:
    """Zielgroesse einer neuen Position in USD-Nominale.

    OHNE stop_distance_pct (Rueckfall fuer Aufrufer ohne bekannten Stop):
    fester Prozentsatz der Equity.

    MIT stop_distance_pct wird das Risiko pro Trade auf risk_per_trade_pct
    der Equity begrenzt (Nominale = Equity * risk_per_trade_pct /
    stop_distance_pct). max_position_pct bleibt zusaetzlich eine harte
    Konzentrationsgrenze und gewinnt, wenn sie enger ist.

    `limits["risk_per_trade_pct"]` ist seit V2 ein HARTER Fehler, wenn er
    fehlt oder 0 ist - kein stiller Ruecksprung auf max_position_pct mehr
    (der haette bei Risikostufe 9 einen 23-fachen Groessenfehler ergeben
    koennen, siehe CLAUDE.md-Feedback zu core.bot_signals.position_size_usd).

    Gibt (None, Grund) zurueck, wenn die Groesse unter der Mindestordergroesse
    der Boerse laege - dann ist der Einstieg unmoeglich und der Grund gehoert
    sichtbar ins Protokoll, statt dass die Order spaeter an der API scheitert.
    `cap_usd` deckelt die erste Live-Order (core.bot_config.FIRST_LIVE_ORDER_CAP_USD).
    """
    if equity_usd <= 0:
        return None, "Kein Kontostand verfügbar."
    concentration_cap = equity_usd * float(limits["max_position_pct"]) / 100.0
    if stop_distance_pct:
        if not limits.get("risk_per_trade_pct"):
            raise ValueError(
                "position_size_usd() benötigt limits['risk_per_trade_pct'] - kein "
                "stiller Rückgriff auf max_position_pct mehr (siehe Docstring).")
        risk_pct = float(limits["risk_per_trade_pct"])
        risk_based = equity_usd * risk_pct / stop_distance_pct
        target = min(concentration_cap, risk_based)
    else:
        target = concentration_cap
    if cap_usd is not None:
        target = min(target, float(cap_usd))
    if target < min_notional_usd:
        return None, (f"Zielgröße {target:,.2f} $ unter der Mindestordergröße "
                      f"{min_notional_usd:,.0f} $ der Börse.")
    return target, None


# --- SCAN ---

def evaluate(symbol: str, df, price: float | None, funding_hourly: float | None,
            limits: dict, btc_regime: str, btc_roc: float | None,
            volume_24h_usd: float | None = None, ind=None,
            disabled_gates: frozenset[str] = frozenset()) -> Signal:
    """Ein Symbol bewerten - ohne jede Kenntnis von Depot oder Guards.

    `btc_roc` wird seit der Entfernung von Gate 5 (relative Staerke zu BTC,
    STRATEGY_VERSION "2.1-swing4h") NICHT MEHR ausgewertet - Parameter
    bewusst nicht entfernt, um core.bot_signals.scan()/analysis.
    bot_backtest.py/alle bestehenden Aufrufer nicht anzufassen, nur um einen
    inert gewordenen Wert loszuwerden.

    `disabled_gates` dient AUSSCHLIESSLICH analysis/bot_ablation.py (Phase 3,
    Gate-Ablation): eine Menge aus {"gate2".."gate7"}, deren Bedingung als
    erfuellt behandelt wird, unabhaengig vom tatsaechlichen Messwert - eine
    Ablationsstudie ("ein Gate entfernen, Rest unveraendert"), KEINE
    Parametersuche (kein Schwellenwert wird verschoben, ein Gate faellt
    entweder ganz weg oder bleibt exakt wie es ist). Default = leere Menge
    aendert NICHTS am Verhalten - der Live-Pfad (core.bot.run_signal_cycle)
    ruft evaluate() nie mit einem nicht-leeren Wert auf.

    Die Richtung folgt ZWINGEND dem Tagesregime (Gate 1) - anders als V1
    wird nicht mehr "long oder short, je nachdem was besser scoret"
    verglichen: bei 'neutral' oder einer per ALLOW_SHORT gesperrten
    Short-Seite gibt es UEBERHAUPT keine handelbare Richtung fuer dieses
    Symbol, unabhaengig von seinem eigenen Chart.

    `ind` ist ein bereits aufbereitetes Indikator-Fenster (Ergebnis von
    prepare()) und dient allein analysis/bot_backtest.py: dort wuerde die
    Aufbereitung sonst in JEDEM Takt erneut ueber dasselbe Kerzenfenster
    laufen. Alle Indikatoren in analysis.technical.add_indicators sind
    kausal (rolling/ewm/shift), deshalb ist ein einmal ueber die ganze Reihe
    berechnetes und dann geschnittenes Fenster ZAHLENGLEICH mit einem pro
    Takt frisch berechneten."""
    ind = prepare(df) if ind is None else ind
    r = readings(ind)
    if r is None:
        return Signal(symbol=symbol, price=price, funding_hourly=funding_hourly,
                      blocked_by=f"Zu wenige Kerzen (mindestens {MIN_CANDLES} nötig).")

    if btc_regime == "long":
        side = "long"
    elif btc_regime == "short" and ALLOW_SHORT:
        side = "short"
    else:
        side = None

    hold_hours = float(limits.get("max_hold_hours", MAX_HOLD_HOURS))
    if side is None:
        # Trotzdem beide Seiten fuer die Anzeige berechnen ("was WUERDE das
        # Symbol hergeben") - nur handelbar ist keine davon.
        long_score, long_reasons, long_parts = score_side(r, "long", funding_hourly, hold_hours)
        short_score, short_reasons, short_parts = score_side(r, "short", funding_hourly, hold_hours)
        display_side = "long" if long_score >= short_score else "short"
        score = max(long_score, short_score)
        reasons = long_reasons if display_side == "long" else short_reasons
        parts = long_parts if display_side == "long" else short_parts
        if btc_regime == "short":
            blocked = "Shorts sind deaktiviert (ALLOW_SHORT=False)."
        else:
            blocked = f"Kein handelbares Tagesregime (BTC: {btc_regime})."
        return Signal(symbol=symbol, side=display_side, score=score, long_score=long_score,
                      short_score=short_score, price=price if price else r["price"],
                      atr_pct=r["atr_pct"], funding_hourly=funding_hourly, reasons=reasons,
                      blocked_by=blocked, components={k: round(v, 1) for k, v in parts.items()},
                      readings=r)

    score, reasons, parts = score_side(r, side, funding_hourly, hold_hours)

    stop_pct = stop_distance_pct(r["atr_pct"], limits)
    atr_pct = r["atr_pct"]
    blocked = None
    if stop_pct is None:
        blocked = "Kein ATR verfügbar - Stop-Abstand nicht berechenbar."
    elif price is None or price <= 0:
        blocked = "Kein Live-Kurs verfügbar."
    elif atr_pct < _ATR_PCT_MIN:
        blocked = (f"ATR {atr_pct:.2f} % zu niedrig - Bewegung deckt die "
                   f"Handelskosten nicht (mindestens {_ATR_PCT_MIN:.2f} %).")
    elif atr_pct > _ATR_PCT_MAX:
        blocked = (f"ATR {atr_pct:.2f} % zu hoch - der nötige Stop läge so weit "
                   f"entfernt, dass ein einzelner Treffer das Tagesverlust-Limit reisst "
                   f"(höchstens {_ATR_PCT_MAX:.2f} %).")
    else:
        # r["price"] (letzter GESCHLOSSENER 4h-Kerzenschluss), nicht der
        # `price`-Parameter (Live-Kurs): Gate 2 wertet DASSELBE Kerzenfenster
        # aus wie der Rest der Bewertung - ein live abweichender Kurs (Sekunden bis
        # Minuten nach Kerzenschluss, siehe Modul-Docstring "genug Abstand
        # zum 15-Minuten-Sicherheitstakt") darf diese strukturelle Pruefung
        # nicht verzerren. Der `price`-Parameter bleibt fuer die "Kein
        # Live-Kurs verfuegbar"-Pruefung oben UND fuer die Stop-/Notional-
        # Berechnung in core.bot.open_position massgeblich.
        ma20, ma50 = r["ma20"], r["ma50"]
        candle_price = r["price"]
        gate2_ok = bool(ma20 and ma50 and (
            (side == "long" and candle_price > ma50 and ma20 > ma50) or
            (side == "short" and candle_price < ma50 and ma20 < ma50)))
        # `"gateN" not in disabled_gates` bei leerer Menge (Live-Pfad,
        # Default) ist immer True - AENDERT NICHTS an der Bedingung von
        # vorher. Nur Gate 2 und Gate 7 existieren noch (siehe Konstanten-
        # Block oben fuer das Warum von Gates 3/4/5/6's Entfernung); die
        # Nummerierungsluecke dazwischen ist beabsichtigt. Gate 7 bleibt
        # ueber disabled_gates ablatierbar, obwohl analysis/bot_backtest.py
        # es wegen _UNLIMITED_VOLUME nie tatsaechlich pruefen kann - fuer
        # einen direkten evaluate()-Aufruf mit echtem volume_24h_usd
        # (Tests, ein kuenftiger Live-Datensatz) bleibt die Faehigkeit sonst
        # grundlos verloren.
        if "gate2" not in disabled_gates and not gate2_ok:
            blocked = "Kurs/MA20/MA50 bestätigen die Richtung nicht (Gate 2)."
        elif "gate7" not in disabled_gates and (
                volume_24h_usd is None or volume_24h_usd < MIN_24H_VOLUME_USD):
            vol_txt = "unbekannt" if volume_24h_usd is None else f"{volume_24h_usd:,.0f} $"
            blocked = (f"24h-Handelsvolumen {vol_txt} unter der Liquiditätsschwelle von "
                       f"{MIN_24H_VOLUME_USD:,.0f} $ (Gate 7).")

    return Signal(symbol=symbol, side=side,
                  score=score, long_score=score if side == "long" else 0.0,
                  short_score=score if side == "short" else 0.0,
                  price=price if price else r["price"], atr_pct=r["atr_pct"],
                  stop_distance_pct=stop_pct, funding_hourly=funding_hourly, reasons=reasons,
                  blocked_by=blocked, components={k: round(v, 1) for k, v in parts.items()},
                  readings=r)


def scan(exchange, candidates: list[str], limits: dict, candles_fn=None,
        daily_fn=None, volume_fn=None) -> list[Signal]:
    """Alle Kandidaten bewerten, beste Rang-Punktzahl zuerst.

    `candles_fn`/`daily_fn`/`volume_fn` sind injizierbar (Defaults:
    data.hyperliquid), damit Tests synthetische Kursreihen einspeisen
    koennen - dasselbe Muster wie PaperExchange's `price_fn`. Marktregime
    (BTC-Tageskerzen) und BTC-eigenes Momentum werden HIER, EINMAL fuer den
    ganzen Scan berechnet, nicht je Kandidat - dieselbe Vergleichsbasis fuer
    alle. Ein einzelner Kandidat ohne Daten wird als Signal MIT `blocked_by`
    zurueckgegeben statt uebersprungen: Auch "keine Daten" ist eine Antwort
    auf die Frage, warum nicht gehandelt wurde."""
    if candles_fn is None:
        from data.hyperliquid import candles_df as candles_fn
    if daily_fn is None:
        from data.hyperliquid import candles_df as _candles_df

        def daily_fn(symbol):
            return _candles_df(symbol, interval="1d", lookback_hours=REGIME_LOOKBACK_HOURS)
    if volume_fn is None:
        from data.hyperliquid import day_notional_volume_usd as volume_fn

    try:
        btc_daily = daily_fn(BENCHMARK_SYMBOL)
    except Exception:
        btc_daily = None
    btc_regime = regime(btc_daily)

    btc_roc = None
    try:
        btc_df = candles_fn(BENCHMARK_SYMBOL)
        btc_ind = prepare(btc_df)
        btc_r = readings(btc_ind) if btc_ind is not None else None
        btc_roc = btc_r["roc_n"] if btc_r else None
    except Exception:
        btc_roc = None

    signals = []
    for symbol in candidates:
        try:
            df = candles_fn(symbol)
        except Exception as e:
            signals.append(Signal(symbol=symbol, blocked_by=f"Kursdaten nicht abrufbar: {e!r}"))
            continue
        try:
            price = exchange.mid_price(symbol)
        except Exception:
            price = None
        try:
            funding = exchange.funding_rate_hourly(symbol)
        except Exception:
            funding = None
        try:
            volume_24h = volume_fn(symbol)
        except Exception:
            volume_24h = None
        signals.append(evaluate(symbol, df, price, funding, limits, btc_regime, btc_roc,
                                volume_24h_usd=volume_24h))
    return sorted(signals, key=lambda s: s.score, reverse=True)


# --- AUSSTIEGE ---

def exit_signal(position: dict, df, limits: dict, btc_regime: str | None = None,
                now: datetime | None = None, ind=None) -> str | None:
    """Grund fuer einen aktiven Ausstieg, sonst None.

    HYSTERESE ("hohe Einstiegs-, niedrigere Haltehuerde"): Ausstieg nur bei
    Zeitablauf, einem *gegenteiligen* Tagesregime oder MA-Kreuz (Gate 2) -
    ein neutrales Regime schliesst nicht mehr reflexartig eine ansonsten
    intakte Position. Die staerkeren Gates 3-7 (Effizienz, Momentum,
    relative Staerke, Volumen, Liquiditaet) muessen fuer den Einstieg gelten,
    nicht fuers Halten. Ein Trade, der die Einstiegshuerde einmal genommen
    hat, soll nicht bei jeder kleinen Volumen- oder Momentum-Schwankung
    schon wieder rausfliegen.

    Gewinne laufen zu lassen ist Aufgabe des nachgezogenen Stops
    (trailing_stop_px), nicht eines festen Kursziels: ein Take-Profit
    deckelt genau die wenigen grossen Trades, aus denen eine
    Trendfolge-Strategie ihre Ueberrendite zieht.
    """
    now = now or clock.now_utc()
    side = position.get("side")

    # exchange_opened_at (der ECHTE Eroeffnungszeitpunkt auf der Boerse, nur
    # bei uebernommenen Positionen bekannt) hat Vorrang vor opened_at (bei
    # einer Uebernahme der Zeitpunkt der UEBERNAHME, nicht der urspruenglichen
    # Eroeffnung).
    opened_raw = position.get("exchange_opened_at") or position.get("opened_at")
    if opened_raw:
        opened = clock.parse_utc(opened_raw)
        if opened is not None:
            max_hold = float(limits.get("max_hold_hours", MAX_HOLD_HOURS))
            if now - opened > timedelta(hours=max_hold):
                return f"Maximale Haltedauer von {max_hold:.0f} h überschritten."

    if btc_regime is not None:
        if side == "long" and btc_regime == "short":
            return "Tagesregime ins Gegenteil gedreht (jetzt: short)."
        if side == "short" and btc_regime == "long":
            return "Tagesregime ins Gegenteil gedreht (jetzt: long)."

    ind = prepare(df) if ind is None else ind
    r = readings(ind)
    if r is None:
        return None
    price, ma20, ma50 = r["price"], r["ma20"], r["ma50"]
    if not ma20 or not ma50:
        return None
    if side == "long" and price < ma20 and ma20 < ma50:
        return "Trend gedreht: Kurs unter MA20, MA20 unter MA50."
    if side == "short" and price > ma20 and ma20 > ma50:
        return "Trend gedreht: Kurs über MA20, MA20 über MA50."
    return None


def trailing_stop_px(position: dict, df, limits: dict,
                     current_price: float | None, ind=None) -> float | None:
    """Neuer, NACHGEZOGENER Stop - oder None, wenn er unveraendert bleibt.

    AKTIVIERUNG ERST AB +1R: vor TRAILING_ACTIVATION_R gilt ausschliesslich
    der beim Einstieg gesetzte Anfangsstop (core.bot.open_position speichert
    ihn unveraenderlich als initial_stop_px). V1 zog jeden Takt sofort nach,
    ohne dass der Trade je im Plus gewesen sein musste - nur 2 von 33 Trades
    erreichten ueberhaupt +1R (CLAUDE.md-Feedback). Unveraendert seit V2.

    MINDESTGEWINN-STOP STATT CHANDELIER (STRATEGY_VERSION "2.3-swing4h",
    12.09.2026): ab +1R wird der Stop EINMALIG und ENDGUELTIG auf einen
    kostenbereinigten Mindestgewinn gezogen (Einstand +/- der geschaetzten
    Rundreise-Kosten, _ROUND_TRIP_COST_PCT_ESTIMATE) - kein fortlaufendes
    Nachziehen am Hoch/Tief seit Eroeffnung mehr danach. Ersetzt die vorher
    verwendete Chandelier-Regel (Hoch/Tief seit Eroeffnung minus/plus
    CHANDELIER_ATR_MULT x ATR, seit STRATEGY_VERSION "2.0-swing4h").

    BELEG (analysis/bot_exit_variants.py, drei unabhaengige Walk-Forward-
    Laeufe, je 45 Tage x 4 verkettete Fenster, Risikostufe 10, 12.09.2026):
    der Mindestgewinn-Stop schlug die Chandelier-Regel in ALLEN DREI Laeufen
    deutlich ueber dem vorab festgelegten Kriterium (Ø R +0,17 bis +0,35,
    Profit-Faktor +40 % bis +89 %). Ursache, aus den Trade-Protokollen
    nachvollzogen: die Chandelier-Regel zog bei einem gewoehnlichen
    Ruecksetzer schon frueh enger nach und wurde dadurch oft ausgestoppt,
    bevor der eigentliche Trend zu Ende war (Ø 75-82h Haltedauer bei
    "stop"-Ausstiegen). Der einmal gesetzte Mindestgewinn-Stop uebersteht
    denselben Ruecksetzer und laesst die Position stattdessen bis zum
    echten Regimewechsel/MA-Kreuz laufen (Ø 180-220h bei "signal"-
    Ausstiegen) - auf Kosten der reinen Trade-Anzahl (ca. 15-20 % weniger
    im selben Zeitraum, da ein Slot laenger belegt bleibt), aber mit
    deutlich hoeherer Trefferquote und Ø R. Eine gepruefte dritte
    Alternative (Chandelier haeufiger nachziehen, anhand von 1h-
    Unterkerzen statt nur alle 4h - analysis.bot_backtest.
    _hourly_trailing_stop_px()) verbesserte das Ergebnis NICHT messbar
    (Ø R -0,04, PF -0,5 %) und wurde deshalb NICHT uebernommen; das
    Werkzeug bleibt fuer eine moegliche kuenftige Messung mit echter
    stuendlicher ATR erhalten (Schritt "3b").

    `df`/`ind`/`limits` werden von dieser Regel nicht mehr benoetigt (die
    Formel braucht nur entry_px/initial_stop_px/stop_px/current_price) -
    die Parameter bleiben aus Kompatibilitaetsgruenden erhalten, core.bot.
    update_trailing_stops() und der Referenz-Zweig in analysis.bot_backtest
    reichen weiterhin Kerzen/ein Indikatorfenster durch.

    Ratsche bleibt: Der Stop wandert ausschliesslich in Gewinnrichtung.
    """
    if not current_price or current_price <= 0:
        return None
    entry_px = position.get("entry_px")
    initial_stop = position.get("initial_stop_px")
    side = position.get("side")
    if entry_px is None or initial_stop is None or side not in ("long", "short"):
        return None
    risk_usd = abs(float(entry_px) - float(initial_stop))
    if risk_usd <= 0:
        return None
    direction = 1.0 if side == "long" else -1.0
    unrealized_r = (current_price - float(entry_px)) * direction / risk_usd
    if unrealized_r < TRAILING_ACTIVATION_R:
        return None

    locked = float(entry_px) * (1 + _ROUND_TRIP_COST_PCT_ESTIMATE / 100.0 * direction)
    current_stop = position.get("stop_px")
    if current_stop is not None and (locked - float(current_stop)) * direction <= 0:
        return None   # bereits mindestens so guenstig (typischerweise: schon gesetzt)
    return locked
