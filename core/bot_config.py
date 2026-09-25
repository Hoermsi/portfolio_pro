"""Trading-Bot: Risikoregler, Konfigurationslimits, Kill-Switch, Demo-/Dry-Run-Guards.

Eigenes core-Modul (statt views/trading_bot), damit core/bot.py,
core/bot_signals.py und core/bot_guards.py sie ohne Streamlit-Import lesen
koennen - nach dem Muster von core/profile.py: Validierung/Clamping passiert
beim LESEN, nicht nur beim Schreiben, damit ein kaputter oder von aussen
manipulierter Meta-Eintrag niemals einen Sicherheits-Guard stillschweigend
ausschaltet.

ZENTRALE STELLSCHRAUBE ist die Risikostufe 1-10 (``risk_level()``). Alle
Limits werden daraus abgeleitet (``derive_limits()``), statt einzeln gepflegt
zu werden - genau eine Zahl, die der Nutzer versteht, statt neun, die
zueinander passen muessen. Ein Experten-Override je Einzelwert bleibt moeglich
(``save_bot_limits()``), wird aber als DELTA gespeichert: alles, was nicht
ausdruecklich ueberschrieben wurde, folgt weiterhin dem Regler.

DER REGLER STEUERT AB JETZT AUSSCHLIESSLICH RISIKO - NIE STRATEGIE. Bis zu
diesem Umbau aenderte eine hoehere Risikostufe unter anderem auch die
Einstiegsschwelle, den Stop-Abstand, die Haltedauer und den Symbol-Cooldown:
ein Nutzer, der nur "mehr riskieren" wollte, bekam unbemerkt eine andere
STRATEGIE - bei Stufe 9 lief die Engine effektiv mit einer auf 29/100
abgesenkten Schwelle, engem 1,36-ATR-Stop und 2,4h-Cooldown, nicht nur mit
groesseren Positionen. Diese Werte sind jetzt feste Konstanten in
core/bot_signals.py (INITIAL_STOP_ATR_MULT, MAX_HOLD_HOURS, ...) -
bot_limits() fuehrt sie unveraendert ins Limits-Dict ein (siehe
strategy_constants()), aber weder der Regler noch save_bot_limits() koennen
sie aendern, weil sie kein Eintrag in _RISK_ANCHORS mehr sind. ENTRY_SCORE_MIN
(V1) ist mit der V2-Signal-Engine ERSATZLOS gestrichen - dort entscheiden
Pflichtbedingungen (Gates), OB gehandelt wird, der Score nur noch die
Rangfolge unter bereits qualifizierten Kandidaten (core/bot_signals.py-
Modul-Docstring).
"""
import hashlib
import json

from core import bot_signals, clock, db

# Risikostufe -> Limits. Je Schluessel die Werte an den Stuetzstellen
# (Stufe 1, Stufe 5, Stufe 10); dazwischen wird linear interpoliert. Die
# Stufen sind bewusst als durchgehende Achse gedacht: Stufe 1 handelt selten
# und klein, Stufe 10 haeufiger und groesser - aber IMMER mit derselben
# Signal-/Stop-/Haltedauer-Logik (core/bot_signals.py), nie mit einer
# anderen. max_leverage bleibt auch bei Stufe 10 bei 2.0 - das ist die vom
# Nutzer vorgegebene harte Obergrenze, keine Reglereinstellung. Er skaliert
# nur die Margin-Anforderung, NICHT die Nominale/den Erwartungswert eines
# Trades (core/bot_signals.position_size_usd() haengt nicht vom Hebel ab) -
# die eigentlichen Risiko-Stellschrauben sind risk_per_trade_pct,
# max_position_pct und max_total_exposure_pct.
_RISK_ANCHORS = {
    # Risikobudget je Trade (core.bot_signals.position_size_usd): wie viel
    # Prozent der Equity ein EINZELNER Trade beim Stop verlieren darf. Stufe
    # 5 = 0,4 % (Validierungsphase: 1x Hebel, kleines Risiko je Trade, siehe
    # Modul-Docstring core/bot.py und CLAUDE.md-Feedback).
    "risk_per_trade_pct": (0.15, 0.4, 1.0),
    # Harte Konzentrationsgrenze PRO Order - greift, wenn das Risikobudget
    # bei einem sehr engen Stop rechnerisch eine unplausibel grosse Position
    # ergeben wuerde (core.bot_signals.position_size_usd: min() der beiden).
    "max_position_pct": (12.0, 25.0, 45.0),
    # Gesamt-Exposure-Deckel (core.bot_guards.check_total_exposure): Summe
    # aller offenen Nominalen darf diesen Prozentsatz der Equity nicht
    # ueberschreiten - unabhaengig von max_position_pct/max_positions, die
    # nur PRO Order bzw. die Anzahl pruefen, nie die Summe.
    "max_total_exposure_pct": (20.0, 55.0, 120.0),
    # NEU (core.bot_guards.check_portfolio_heat, ab Phase 5 verdrahtet): Summe
    # aller Anfangs-Risiken (|Einstieg-Stop| * Groesse) ueber ALLE offenen
    # Positionen - anders als max_total_exposure_pct (Brutto-Nominale) ist
    # das der tatsaechliche Verlust, wenn JEDER Stop gleichzeitig reisst.
    "max_portfolio_heat_pct": (0.5, 1.5, 3.5),
    "max_leverage": (1.0, 1.0, 2.0),
    "max_positions": (2, 3, 5),
    # NEU (core.bot_guards.check_same_side_concentration, ab Phase 5
    # verdrahtet): mehrere gleichgerichtete Positionen sind eine gebuendelte
    # Faktorwette, keine unabhaengigen Wetten - siehe CLAUDE.md-Feedback
    # ("ETH, NEAR und SOL innerhalb von 32 Minuten alle short").
    "max_same_side_positions": (1, 2, 3),
    "daily_loss_limit_pct": (3.0, 6.0, 12.0),
    "equity_floor_pct": (85.0, 70.0, 50.0),
}

_INT_KEYS = {"max_positions", "max_same_side_positions"}

DEFAULT_RISK_LEVEL = 5
MIN_RISK_LEVEL = 1
MAX_RISK_LEVEL = 10

# (min, max) je Schluessel fuer die Read-Time-Clamps. Auch die aus dem Regler
# ABGELEITETEN Werte laufen hier durch - so kann weder ein manipulierter
# Meta-Eintrag noch ein Rechenfehler in den Ankerwerten eine harte Grenze
# (insbesondere max_leverage = 2.0) ueberschreiten.
_LIMIT_BOUNDS = {
    "risk_per_trade_pct": (0.05, 5.0),
    "max_position_pct": (1.0, 100.0),
    "max_total_exposure_pct": (5.0, 300.0),
    "max_portfolio_heat_pct": (0.1, 20.0),
    "max_leverage": (1.0, 2.0),
    "max_positions": (1, 20),
    "max_same_side_positions": (1, 20),
    "daily_loss_limit_pct": (1.0, 50.0),
    "equity_floor_pct": (10.0, 95.0),
}

# Beschriftungen fuer die UI-Vorschau - hier statt in views/trading_bot.py,
# damit ein neuer Anker-Schluessel nicht an zwei Stellen gepflegt werden muss.
LIMIT_LABELS = {
    "risk_per_trade_pct": "Risiko je Trade (% der Equity)",
    "max_position_pct": "Max. Position (% der Equity)",
    "max_total_exposure_pct": "Max. Gesamt-Exposure (% der Equity)",
    "max_portfolio_heat_pct": "Max. Portfolio-Heat (% der Equity)",
    "max_leverage": "Max. Hebel",
    "max_positions": "Max. Positionen",
    "max_same_side_positions": "Max. gleichgerichtete Positionen",
    "daily_loss_limit_pct": "Tagesverlust-Limit (%)",
    "equity_floor_pct": "Equity-Boden (% vom Start)",
}

# Feste Strategie-Parameter (core.bot_signals) - siehe Modul-Docstring oben:
# NIE vom Regler oder von save_bot_limits() aenderbar, nur zur Anzeige/zum
# Zusammenfuehren mit den Risiko-Limits in bot_limits(). Beschriftungen
# separat von LIMIT_LABELS, damit die UI (views/trading_bot.py) die beiden
# Gruppen bewusst getrennt darstellen kann statt sie zu vermischen.
STRATEGY_LABELS = {
    "atr_stop_mult": "Anfangsstop (× ATR)",
    "max_hold_hours": "Max. Haltedauer (h)",
    "symbol_cooldown_hours": "Symbol-Cooldown (h)",
    "max_trades_per_day": "Max. Trades/Tag",
    "funding_budget_hourly": "Funding-Budget (je Stunde)",
    "loss_streak_limit": "Verlustserie-Sperre ab",
}


def strategy_constants() -> dict:
    """Feste Strategie-Parameter aus core.bot_signals, unveraendert in das
    von bot_limits() zurueckgegebene Dict gemischt - bestehende Aufrufer
    (core.bot, core.bot_guards, agents.trader, analysis.bot_backtest) lesen
    weiterhin ein einziges Limits-Dict, ohne zwischen "vom Regler" und "fest"
    unterscheiden zu muessen. Import hier (nicht auf Modulebene) waere nicht
    noetig - core.bot_signals importiert core.bot_config nicht zurueck -,
    steht aber bewusst neben den anderen Konstanten oben, nicht verstreut.

    KEIN "entry_score_min" mehr (V1-Konzept, ersatzlos gestrichen) - die
    V2-Signal-Engine (core.bot_signals) entscheidet ueber Pflichtbedingungen
    (Gates), nicht ueber eine Punktzahl-Schwelle.

    KEIN "llm_review_hours" mehr hier: der KI-Berichts-Takt ist seit dem
    An/Aus-Schalter (ai_report_enabled()) eine eigene, vom Nutzer direkt
    wählbare Einstellung (ai_report_interval_hours()) statt einer festen
    Konstante - anders als die uebrigen Werte hier beeinflusst er keine
    einzige Handelsentscheidung, nur wie oft die reine Berichterstattung
    (agents/trader.py) Geld kostet."""
    return {
        "atr_stop_mult": bot_signals.INITIAL_STOP_ATR_MULT,
        "max_hold_hours": bot_signals.MAX_HOLD_HOURS,
        "symbol_cooldown_hours": bot_signals.SYMBOL_COOLDOWN_HOURS,
        "max_trades_per_day": bot_signals.MAX_TRADES_PER_DAY,
        "funding_budget_hourly": bot_signals.FUNDING_BUDGET_HOURLY,
        "loss_streak_limit": bot_signals.LOSS_STREAK_LIMIT,
    }


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp_risk_level(value) -> int:
    try:
        return min(MAX_RISK_LEVEL, max(MIN_RISK_LEVEL, int(round(float(value)))))
    except (TypeError, ValueError):
        return DEFAULT_RISK_LEVEL


def derive_limits(risk: int) -> dict:
    """Vollstaendiges Limit-Set fuer eine Risikostufe (1-10).

    Zwei lineare Abschnitte (1->5 und 5->10) statt einer Geraden ueber alles:
    So bleibt die Mitte des Reglers eine bewusst gewaehlte, ausgewogene
    Einstellung und nicht bloss der Mittelwert der beiden Extreme.
    """
    level = clamp_risk_level(risk)
    out = {}
    for key, (low, mid, high) in _RISK_ANCHORS.items():
        if level <= DEFAULT_RISK_LEVEL:
            t = (level - MIN_RISK_LEVEL) / (DEFAULT_RISK_LEVEL - MIN_RISK_LEVEL)
            value = _lerp(low, mid, t)
        else:
            t = (level - DEFAULT_RISK_LEVEL) / (MAX_RISK_LEVEL - DEFAULT_RISK_LEVEL)
            value = _lerp(mid, high, t)
        out[key] = int(round(value)) if key in _INT_KEYS else float(value)
    return out


def risk_level() -> int:
    """Gespeicherte Risikostufe (1-10), Default 5."""
    raw = db.get_meta("bot_risk_level")
    if raw is None:
        return DEFAULT_RISK_LEVEL
    return clamp_risk_level(raw)


def save_risk_level(level: int) -> dict:
    """Setzt die Risikostufe und VERWIRFT alle Experten-Overrides.

    Sonst wuerde ein einmal per Hand gesetzter Einzelwert den Regler auf genau
    dieser Achse dauerhaft wirkungslos machen - der Nutzer zieht den Regler und
    nichts passiert. Der Regler ist der Hauptweg, also gewinnt er.
    """
    db.set_meta("bot_risk_level", str(clamp_risk_level(level)))
    db.delete_meta("bot_limits")
    return bot_limits()


def ai_report_enabled() -> bool:
    """Ob der KI-Bericht (agents/trader.py, seit Phase 8 reine
    Berichterstattung ohne jede Entscheidungsbefugnis) ueberhaupt laufen
    darf. Default an, aber bewusst per Schalter abschaltbar: der Bericht
    kostet bei JEDEM Lauf Claude-API-Gebuehren, loest aber nachweislich
    keine einzige Order aus - core.bot_signals/core.bot_guards entscheiden
    unabhaengig davon. Wer das nicht will, muss dafuer nicht den globalen
    ANTHROPIC_API_KEY entfernen (der auch andere Teile der App bedient)."""
    return db.get_meta("bot_ai_report_enabled") != "0"


def set_ai_report_enabled(enabled: bool) -> None:
    db.set_meta("bot_ai_report_enabled", "1" if enabled else "0")


# Feste Auswahl statt freier Eingabe - ein Wert dazwischen (z.B. 18h) waere
# keine bewusste Nutzerentscheidung, nur ein Versehen am Regler.
AI_REPORT_INTERVAL_CHOICES_HOURS = (6, 12, 24, 48, 168)
DEFAULT_AI_REPORT_INTERVAL_HOURS = 6


def ai_report_interval_hours() -> int:
    """Wie oft der KI-Bericht (agents/trader.py) hoechstens laufen darf,
    wenn er aktiv ist (ai_report_enabled()). Reine Kosten-/Frequenz-
    Einstellung - anders als die Werte in strategy_constants() beeinflusst
    sie keine Handelsentscheidung, deshalb eigener Meta-Eintrag statt Teil
    der Risiko-Limits."""
    raw = db.get_meta("bot_ai_report_interval_hours")
    try:
        value = int(raw) if raw is not None else DEFAULT_AI_REPORT_INTERVAL_HOURS
    except (TypeError, ValueError):
        return DEFAULT_AI_REPORT_INTERVAL_HOURS
    return value if value in AI_REPORT_INTERVAL_CHOICES_HOURS else DEFAULT_AI_REPORT_INTERVAL_HOURS


def set_ai_report_interval_hours(hours) -> None:
    hours = int(hours)
    if hours not in AI_REPORT_INTERVAL_CHOICES_HOURS:
        raise ValueError(f"Ungültiges Berichtsintervall: {hours} h "
                         f"(erlaubt: {AI_REPORT_INTERVAL_CHOICES_HOURS}).")
    db.set_meta("bot_ai_report_interval_hours", str(hours))


def _stored_overrides() -> dict:
    """Experten-Overrides als Delta - nur ausdruecklich gesetzte Schluessel."""
    raw = db.get_meta("bot_limits")
    try:
        values = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {k: v for k, v in values.items() if k in _RISK_ANCHORS}


def limits_for_risk_level(level: int) -> dict:
    """Wie bot_limits(), aber fuer eine EXPLIZIT uebergebene Risikostufe statt
    der aktuell gespeicherten (risk_level()) - Grundlage fuer
    analysis.bot_backtest.run_backtest(risk_level=...) (Phase 6, CLAUDE.md-
    Feedback "Rücktest live-identisch machen"): vorher rechnete der Rücktest
    mit derive_limits() allein und liess damit gespeicherte Experten-Overrides
    (z.B. ein reduziertes max_trades_per_day) UNSICHTBAR aussen vor - er
    testete faktisch eine andere Konfiguration als die, die live tatsaechlich
    galt. Overrides und Strategie-Konstanten sind global (nicht je Stufe
    gespeichert), gelten hier also identisch wie in bot_limits()."""
    derived = derive_limits(level)
    out = dict(derived)
    out.update(_stored_overrides())
    for key, (lo, hi) in _LIMIT_BOUNDS.items():
        try:
            val = min(hi, max(lo, float(out[key])))
        except (TypeError, ValueError, KeyError):
            val = derived[key]
        out[key] = int(val) if key in _INT_KEYS else float(val)
    if out["max_same_side_positions"] > out["max_positions"]:
        out["max_same_side_positions"] = out["max_positions"]
    out.update(strategy_constants())
    return out


def bot_limits() -> dict:
    """Wirksame Bot-Limits: aus der Risikostufe abgeleitet, darueber die
    Experten-Overrides, darueber die festen Strategie-Konstanten. Jeder
    Risiko-Wert wird einzeln auf seine Bounds geklammert, damit ein teilweise
    kaputter oder veralteter Meta-Eintrag nicht ein einzelnes Limit
    unbegrenzt laesst. Die Strategie-Konstanten (strategy_constants())
    werden ZULETZT gemischt und sind von KEINEM Meta-Eintrag beeinflussbar -
    das ist der ganze Punkt (siehe Modul-Docstring)."""
    return limits_for_risk_level(risk_level())


def save_bot_limits(**overrides) -> dict:
    """Experten-Override einzelner Limits. Nur bekannte Schluessel werden
    uebernommen; alles andere folgt weiterhin dem Risikoregler. Gibt die
    wirksamen, geklammerten Limits zurueck (nicht die rohen Overrides), damit
    der Aufrufer sofort sieht, was tatsaechlich gilt."""
    current = _stored_overrides()
    current.update({k: v for k, v in overrides.items() if k in _RISK_ANCHORS})
    db.set_meta("bot_limits", json.dumps(current))
    return bot_limits()


def overridden_keys() -> list[str]:
    """Welche Limits gerade vom Regler abweichen - fuer die UI, damit ein
    vergessener Experten-Override sichtbar bleibt statt still zu wirken."""
    return sorted(_stored_overrides())


def clear_overrides() -> dict:
    db.delete_meta("bot_limits")
    return bot_limits()


def config_fingerprint(limits: dict | None = None) -> str:
    """Kurzer Fingerabdruck der aktuell WIRKSAMEN Konfiguration (Risikostufe,
    Experten-Overrides und feste Strategie-Konstanten zusammen), persistiert
    neben core.bot_signals.STRATEGY_VERSION in bot_signal_log/bot_decisions/
    bot_positions bei jedem Schreibzugriff.

    WARUM: STRATEGY_VERSION allein trennt nur unterschiedlichen SCORING-CODE
    (score_side/evaluate/Gates) - eine 1430 Zeilen umfassende Historie trug
    frueher durchgehend strategy_version="1.0", obwohl sich darunter mehrfach
    die Konfiguration geaendert hatte (Risikoregler-Overrides kamen und
    gingen). Ohne einen zweiten, konfigurationssensitiven Wert liess sich im
    Nachhinein nicht rekonstruieren, unter welchem tatsaechlichen Regelwerk
    eine bestimmte Zeile entstand - jede Auswertung mischte unbemerkt mehrere
    Konfigurationszustaende. sha1 statt eines Zaehlers, weil es KEINEN
    zentralen Ort braucht, der eine Versionsnummer hochzaehlt - zwei gleiche
    Konfigurationen ergeben deterministisch denselben Fingerabdruck, egal
    wann/wo sie galten."""
    # Der Kandidaten-Pool aendert die tatsächlich gehandelte Strategie
    # materiell. Lazy import vermeidet den sonst unnoetigen Importpfad beim
    # einfachen Lesen von Limits.
    from core import bot_universe
    payload = {"strategy_version": bot_signals.STRATEGY_VERSION,
              "limits": (limits if limits is not None else bot_limits()),
              "universe": bot_universe.fingerprint_payload()}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def feasibility(equity_usd: float, limits: dict | None = None,
                min_notional_usd: float | None = None) -> dict:
    """Kann bei DIESEM Kontostand mit DIESEN Limits ueberhaupt eine Order
    entstehen?

    Hyperliquid lehnt Orders unter ``MIN_NOTIONAL_USD`` (10 $) ab. Bei einem
    bewusst kleinen Konto ist das keine Randbedingung, sondern der haeufigste
    stille Blocker: eine niedrige Risikostufe erzeugt eine Zielgroesse
    unterhalb der Mindestordergroesse, und der Bot handelt schlicht nie - ohne
    dass irgendwo ein Fehler auftaucht. Die UI zeigt das deshalb als Warnung
    an, statt den Regler heimlich hochzusetzen.

    Seit core.bot_signals.position_size_usd() die Groesse zusaetzlich am
    Risikobudget (risk_per_trade_pct / Stop-Abstand) bemisst, kann die
    tatsaechliche Zielgroesse UNTER max_position_pct liegen - hier wird
    deshalb zusaetzlich der ungünstigste plausible Stop-Abstand geprueft (die
    ATR-Obergrenze aus core.bot_signals._ATR_PCT_MAX mal atr_stop_mult,
    geklammert an core.bot_signals._STOP_PCT_CEILING), damit die Warnung
    nicht optimistisch "ok" meldet, obwohl der Bot bei einem weiten Stop
    real gar nicht handeln kann.
    """
    from data.hyperliquid import MIN_NOTIONAL_USD

    limits = limits if limits is not None else bot_limits()
    floor = float(min_notional_usd if min_notional_usd is not None else MIN_NOTIONAL_USD)
    equity = max(0.0, float(equity_usd or 0.0))
    concentration_cap = equity * limits["max_position_pct"] / 100.0
    worst_case_stop_pct = min(bot_signals._STOP_PCT_CEILING,
                              bot_signals._ATR_PCT_MAX * float(limits.get("atr_stop_mult", 2.0)))
    risk_pct = float(limits.get("risk_per_trade_pct") or limits["max_position_pct"])
    risk_based_worst_case = (equity * risk_pct / worst_case_stop_pct
                             if worst_case_stop_pct else concentration_cap)
    max_notional = min(concentration_cap, risk_based_worst_case)
    min_pct_needed = (floor / equity * 100.0) if equity > 0 else None

    if equity <= 0:
        return {"ok": False, "max_notional_usd": 0.0, "min_notional_usd": floor,
                "min_pct_needed": None,
                "hint": "Kein Kontostand bekannt - Machbarkeit nicht bewertbar."}
    if max_notional >= floor:
        return {"ok": True, "max_notional_usd": max_notional, "min_notional_usd": floor,
                "min_pct_needed": min_pct_needed, "hint": ""}
    return {
        "ok": False,
        "max_notional_usd": max_notional,
        "min_notional_usd": floor,
        "min_pct_needed": min_pct_needed,
        "hint": (f"Bei {equity:,.2f} $ Equity erlaubt die aktuelle Einstellung hoechstens "
                 f"{max_notional:,.2f} $ je Position - die Boerse verlangt aber mindestens "
                 f"{floor:,.0f} $ ({min_pct_needed:.1f} % der Equity). Der Bot kann so keine "
                 f"einzige Order platzieren: hoehere Risikostufe waehlen oder einzahlen."),
    }


# --- KILL-SWITCH: Sperrklinke, faellt nie automatisch zurueck ---
#
# Nach dem Muster von core/profile.advance_cycle_tier(): einmal ausgeloest -
# manuell ueber die UI oder automatisch durch einen Guard (z.B. Equity-Boden
# core/bot_guards.check_equity_floor() unterschritten) - bleibt der Kill-Switch
# aktiv, bis der Nutzer ihn BEWUSST zuruecksetzt. Ein Bug, der die Ursache des
# Stopps "repariert" (z.B. ein falscher Kurs, der sich korrigiert), darf den
# Bot nicht von selbst wieder anlaufen lassen.

def is_killed() -> bool:
    return db.get_meta("bot_kill_switch") == "1"


def trip_kill_switch(reason: str):
    """Loest den Kill-Switch aus. Idempotent - ein bereits aktiver Switch
    wird durch einen zweiten Grund nicht ueberschrieben, der ERSTE Grund
    bleibt sichtbar (der ist es, der tatsaechlich zum Stopp gefuehrt hat)."""
    if is_killed():
        return
    db.set_meta("bot_kill_switch", "1")
    db.set_meta("bot_kill_switch_reason", reason)
    db.set_meta("bot_kill_switch_at", _now())


def kill_switch_info() -> dict:
    return {
        "active": is_killed(),
        "reason": db.get_meta("bot_kill_switch_reason"),
        "at": db.get_meta("bot_kill_switch_at"),
    }


def reset_kill_switch():
    """Einzige Moeglichkeit, den Kill-Switch zu loesen - muss der Nutzer
    bewusst in der UI ausloesen, passiert nie automatisch."""
    db.delete_meta("bot_kill_switch")
    db.delete_meta("bot_kill_switch_reason")
    db.delete_meta("bot_kill_switch_at")


# --- RUNNER-GESUNDHEIT: aufeinanderfolgende Zyklus-Fehlschlaege ---
#
# Ein frischer Heartbeat sagt nur "der Prozess lebt", nicht "der letzte
# Zyklus hat tatsaechlich etwas getan". Ein Dauerfehler (z.B. eine seit einem
# Hyperliquid-API-Update kaputte Kerzen-Abfrage) liess den Runner bislang
# beliebig lange GRUEN aussehen (Heartbeat + Konsolenlog), waehrend real seit
# Stunden weder Reconciliation noch Trailing-Stops noch neue Einstiege liefen
# - ohne jede Warnung ausser einer Zeile im Log. Anders als der Kill-Switch
# (Sperrklinke, braucht bewusstes Zuruecksetzen) heilt dieser Zustand SELBST,
# sobald wieder ein Zyklus sauber durchlaeuft - ein voruebergehender
# Datenausfall soll sich nicht in eine manuelle Nutzeraktion verwandeln.

MAX_CONSECUTIVE_CYCLE_FAILURES = 3


def record_cycle_success():
    db.set_meta("bot_runner_last_success_at", _now())
    db.set_meta("bot_runner_consecutive_failures", "0")


def record_cycle_failure() -> int:
    """Zaehlt hoch und gibt den neuen Stand zurueck - der Aufrufer
    (bot_runner.py) loggt eine Warnung, sobald die Schwelle erreicht wird."""
    count = int(db.get_meta("bot_runner_consecutive_failures") or 0) + 1
    db.set_meta("bot_runner_consecutive_failures", str(count))
    return count


def consecutive_cycle_failures() -> int:
    return int(db.get_meta("bot_runner_consecutive_failures") or 0)


def runner_degraded() -> bool:
    """True ab MAX_CONSECUTIVE_CYCLE_FAILURES Fehlschlaegen in Folge - sperrt
    ueber core.bot_guards.check_runner_health() NUR neue Einstiege, bis
    wieder ein Zyklus sauber durchlaeuft. Bestehende Positionen/Stops laufen
    unveraendert weiter."""
    return consecutive_cycle_failures() >= MAX_CONSECUTIVE_CYCLE_FAILURES


def last_successful_cycle_at() -> str | None:
    return db.get_meta("bot_runner_last_success_at")


# --- DEMO-MODUS-GUARD ---

def live_trading_allowed() -> bool:
    """False im Demo-Modus (core.config.DB_PATH == DEMO_DB_PATH) - der Bot
    darf dort nie eine echte Order senden, unabhaengig von allen anderen
    Einstellungen. Lazy-Import wie in core/config.py, um einen Zirkelimport
    zu vermeiden."""
    from core import config
    return config.DB_PATH != config.DEMO_DB_PATH


# --- DRY-RUN: zweite, unabhaengige Bremse zusaetzlich zum Demo-Modus-Guard ---

def dry_run_enabled() -> bool:
    """Hart auf True vorbelegt - erst wenn der Nutzer den Live-Handel in der
    UI bewusst freigibt (schreibt 'bot_dry_run' = '0'), sendet der Runner
    echte Orders statt sie nur zu protokollieren."""
    return db.get_meta("bot_dry_run") != "0"


def set_dry_run(enabled: bool):
    db.set_meta("bot_dry_run", "1" if enabled else "0")


# --- WALK-FORWARD-LAUFPROTOKOLL ---
#
# Der fruehere Live-Nachweis-Zwang (Walk-Forward-Zertifikat ODER dokumentierte
# manuelle Ausnahme, Pflicht fuer jede neue Live-Order) ist entfernt: der
# Live-Schalter verlangt seither nur noch die Verbindungs-Vorpruefung
# (preflight_is_fresh()) plus Tippbestaetigung "LIVE" in
# views/trading_bot.py::_live_gate_dialog() - Nutzerentscheidung, der
# Walk-Forward-Test bleibt als freiwilliges Analyse-Werkzeug erhalten.
# log_walkforward_run() protokolliert dafuer weiterhin JEDEN Lauf (bestanden
# und durchgefallen) in db.bot_walkforward_runs, rein zur Nachvollziehbarkeit
# der Strategie-Guete im Zeitverlauf - ohne irgendeine Order zu blockieren.

def log_walkforward_run(result: dict, window_days: int, num_windows: int,
                        risk_level: int) -> None:
    """Protokolliert JEDEN Walk-Forward-Lauf in db.bot_walkforward_runs,
    bestanden UND durchgefallen. Ohne das war nur der zuletzt bestandene Lauf
    sichtbar; wie viele Fenstergroessen/Risikostufen vorher durchgefallen
    waren, bevor eine Kombination bestand, liess sich nirgends
    nachvollziehen - im Kern eine unsichtbare Parametersuche auf dem
    Freigabe-Kriterium selbst. Diese Funktion verbietet das Ausprobieren
    nicht, macht es nur sichtbar.

    `validation_hash` = config_fingerprint() der fuer GENAU diese Risikostufe
    wirksamen Limits (limits_for_risk_level(risk_level), nicht die gerade
    aktive bot_limits() - der Lauf kann eine andere Stufe getestet haben als
    die live gesetzte) - enthaelt bereits bot_signals.STRATEGY_VERSION UND
    das Kandidaten-Universum (siehe dessen Docstring). Vorher fest `None`:
    ein gespeicherter Lauf liess sich nachtraeglich keinem Code-/
    Konfigurationsstand zuordnen (gefunden von einer externen Pruefung,
    13.09.2026) - kein Anspruch auf einen vollstaendigen Datei-Hash (kein
    Git-Repo vorhanden), aber immerhin Strategie-Version + Limits + Universum
    nachtraeglich unterscheidbar."""
    gate = result.get("gate") if isinstance(result, dict) else None
    if not isinstance(gate, dict):
        return
    validation_hash = config_fingerprint(limits_for_risk_level(risk_level))
    db.add_bot_walkforward_run(
        window_days=window_days, num_windows=num_windows, risk_level=risk_level,
        gate_ok=bool(gate.get("ok")), gate_reasons=gate.get("reasons"),
        pooled=gate.get("pooled"), validation_hash=validation_hash,
    )


# --- ERSTE LIVE-ORDER: gedeckelter Nachweis statt separatem Scharfschuss ---
#
# Der frueher verpflichtende Phase-F-Scharfschuss (phase_f_shot.py) ist kein
# Zwang mehr. An seine Stelle tritt eine Deckelung der ERSTEN Eroeffnung nach
# einem Wechsel in den Live-Modus: sie liefert denselben Nachweis (Fill,
# Stop-OID auf der Boerse, DB-Eintrag), aber als echter Trade statt als
# separates Ritual. Der Marker wird beim Moduswechsel zurueckgesetzt, damit
# jeder neue Live-Start wieder klein anfaengt.

FIRST_LIVE_ORDER_CAP_USD = 12.0


def first_live_order_pending() -> bool:
    return db.get_meta("bot_first_live_order_done") != "1"


def mark_first_live_order_done():
    db.set_meta("bot_first_live_order_done", "1")


def reset_first_live_order():
    db.delete_meta("bot_first_live_order_done")


# --- LIVE-SCHALTER-VORAUSSETZUNG: Preflight muss kuerzlich erfolgreich gewesen sein ---

def last_preflight_ok_at() -> str | None:
    return db.get_meta("bot_last_preflight_ok_at")


def preflight_is_fresh(max_age_minutes: float = 60.0) -> bool:
    """True nur, wenn core.bot_live.read_only_preflight() innerhalb der
    letzten `max_age_minutes` erfolgreich war - Voraussetzung für den
    Live-Schalter in der UI, damit ein Wechsel in den Live-Modus nie "ins
    Blaue" passiert, ohne dass die Verbindung nachweislich funktioniert."""
    last_ok = clock.parse_utc(last_preflight_ok_at())
    if last_ok is None:
        return False
    age_minutes = (clock.now_utc() - last_ok).total_seconds() / 60
    return 0 <= age_minutes <= max_age_minutes


# --- PHASE-F-NACHWEIS: optionaler Echtgeld-Scharfschuss ---
#
# Nicht mehr Voraussetzung fuer den Live-Schalter (siehe
# FIRST_LIVE_ORDER_CAP_USD oben), aber weiterhin durchfuehrbar und in der UI
# sichtbar - wer den separaten Test will, bekommt ihn.

def phase_f_verified_at() -> str | None:
    return db.get_meta("bot_phase_f_verified_at")


def phase_f_is_fresh(max_age_hours: float = 24.0) -> bool:
    """True, wenn ``phase_f_shot.py`` innerhalb der letzten Stunden
    vollstaendig erfolgreich war (Entry, Stop-OID, Cleanup auf dem echten
    Konto). Rein informativ."""
    verified = clock.parse_utc(phase_f_verified_at())
    if verified is None:
        return False
    age_hours = (clock.now_utc() - verified).total_seconds() / 3600
    return 0 <= age_hours <= max_age_hours


def mark_phase_f_verified():
    db.set_meta("bot_phase_f_verified_at", _now())


def _now() -> str:
    return clock.iso_utc()
