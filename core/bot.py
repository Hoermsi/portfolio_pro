"""Trading-Bot-Engine: Positions-Lebenszyklus, Guard-Durchsetzung, Equity-/
Kosten-Tracking, deterministischer Überwachungszyklus (Plan-Stufe 1 - kein
LLM). Läuft gegen jede Implementierung von data.hyperliquid.ExchangeProtocol
(PaperExchange in dieser Phase; LiveExchange folgt in Phase F nach dem
Scharfschuss-Test) - dieses Modul kennt nur das Protokoll, nie welche der
beiden Implementierungen tatsächlich dahinter steckt.

Vorbild core/shadow.py: init_from_real()/reset() als Lifecycle-Muster,
dieselbe Fairness-Snapshot-Idee beim Start. ANDERS als core/shadow.py hält
dieses Modul Risikoregeln NICHT nur im Prompt (das genügt für ein virtuelles
Depot, nicht für echtes Geld) - core.bot_guards läuft vor JEDER Order, egal
woher der Auftrag kommt (später: agents/trader.py, Phase D).
"""
import json
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from core import bot_config, bot_guards, bot_signals, clock, db


class BotError(Exception):
    pass


# Kurzer Retry direkt nach einem gefuellten Fill, bevor die frische Position
# im Kontostand gesucht wird - dasselbe Muster wie data.hyperliquid.
# LiveExchange._wait_for_verified_stop (_STOP_VERIFY_ATTEMPTS/_DELAY): eine
# leicht verzoegerte Lese-API koennte sonst eine tatsaechlich gefuellte Order
# als "keine Position gefunden" melden, obwohl die Boerse sie laengst haelt -
# der Bot wuerde eine echte offene Position dann nicht verwalten.
_POSITION_LOOKUP_ATTEMPTS = 5
_POSITION_LOOKUP_DELAY = 0.2


def _find_position(exchange, symbol: str):
    for attempt in range(_POSITION_LOOKUP_ATTEMPTS):
        fresh = next((p for p in exchange.account_state().positions if p.symbol == symbol), None)
        if fresh is not None:
            return fresh
        if attempt + 1 < _POSITION_LOOKUP_ATTEMPTS:
            time.sleep(_POSITION_LOOKUP_DELAY)
    return None


def _exchange_kind(exchange) -> str:
    """"paper" oder "live" - erkannt am selben Duck-Typing-Signal wie
    _apply_funding_if_paper() (nur PaperExchange hat apply_funding)."""
    return "paper" if getattr(exchange, "apply_funding", None) is not None else "live"


# --- LIFECYCLE ---

def is_active() -> bool:
    return db.get_meta("bot_start") is not None


def start_info() -> dict | None:
    raw = db.get_meta("bot_start")
    return json.loads(raw) if raw else None


def initialize(exchange) -> dict:
    """Einmalige Aktivierung: friert das Startkapital ein - Vorbild
    core.shadow.init_from_real() (derselbe Fairness-Anker für die spätere
    Performance-Berechnung: alles wird relativ zu DIESEM Wert gemessen).
    Wirft BotError, wenn schon aktiv - reset() muss davor bewusst aufgerufen
    werden, ein zweiter impliziter Start würde die Vergleichsbasis verzerren."""
    if is_active():
        raise BotError("Bot ist bereits initialisiert - reset() zuerst, um neu zu starten.")
    state = exchange.account_state()
    info = {"date": clock.now_utc().date().isoformat(), "equity_start_usd": state.equity_usd,
           "exchange_kind": _exchange_kind(exchange),
           "started_at": clock.iso_utc()}
    db.set_meta("bot_start", json.dumps(info))
    record_equity_snapshot(exchange)
    return info


def record_session_start(exchange) -> float:
    """Bei JEDEM Runner-Start neu geschrieben (bot_runner.py) - reine
    Anzeige-Baseline für die 'Startkapital'-Metrik. Bewusst getrennt von
    equity_start_usd(): der sicherheitskritische Anker für den
    Equity-Boden-Guard (core.bot_guards.check_equity_floor) und das
    90-Tage-Abbruchkriterium darf sich NIE durch einen bloßen Prozess-
    Neustart verschieben - siehe Docstring von exchange_kind_mismatch()."""
    state = exchange.account_state()
    db.set_meta("bot_session_start_usd", str(state.equity_usd))
    db.set_meta("bot_session_start_at", clock.iso_utc())
    return state.equity_usd


def session_start_usd() -> float | None:
    raw = db.get_meta("bot_session_start_usd")
    if raw is not None:
        return float(raw)
    info = start_info()
    return float(info["equity_start_usd"]) if info else None


def exchange_kind_mismatch(exchange) -> bool:
    """True, wenn bereits ein Start-Anker existiert, der zu einer ANDEREN
    Exchange-Art gehört als die gerade übergebene (typisch: Papierlauf-Anker,
    jetzt wird Live gestartet, oder umgekehrt) - z.B. weil ein Papierlauf-Test
    den Anker auf ein Startkapital gesetzt hat, das mit dem echten
    Kontostand nichts zu tun hat. Absichtlich KEINE automatische
    Neuinitialisierung hier - das würde die Vergleichsbasis heimlich
    verschieben. Der Aufrufer (bot_runner.py) muss abbrechen und einen
    bewussten core.bot.reset() + Neustart verlangen. Ein Anker von VOR
    diesem Fix (ohne 'exchange_kind'-Feld) blockiert nicht - er wird nur ab
    jetzt getrackt, nicht rückwirkend als Fehler gewertet."""
    info = start_info()
    if info is None or info.get("exchange_kind") is None:
        return False
    return info["exchange_kind"] != _exchange_kind(exchange)


def current_mode() -> str:
    """"paper" oder "live" nach der GEWÜNSCHTEN Konfiguration (Dry-Run/Demo-
    Guard) - ohne dafür eine Exchange-Instanz aufzubauen. Für die UI, die
    keine echte Börsenverbindung braucht, nur um den Anker-Konflikt
    anzuzeigen, der bot_runner.py sonst stumm zum Abbruch brachte."""
    return "paper" if (bot_config.dry_run_enabled() or not bot_config.live_trading_allowed()) else "live"


def anchor_mismatch() -> bool:
    """UI-Variante von exchange_kind_mismatch() - vergleicht den Start-Anker
    gegen den GEWÜNSCHTEN Modus, ohne eine Exchange-Instanz zu bauen."""
    info = start_info()
    if info is None or info.get("exchange_kind") is None:
        return False
    return info["exchange_kind"] != current_mode()


def realign_anchor_to_current_mode():
    """Setzt NUR das `exchange_kind`-Feld des Start-Ankers auf den aktuell
    gewünschten Modus, ohne die Equity-Historie zu löschen.

    Löst genau den Fall, der den Runner heute den ganzen Tag stumm beendet
    hat: ein Dry-Run-Wechsel, nachdem der Anker bereits als 'live' gesetzt
    war. Ein voller reset() wäre hier unnötig hart - die bisherigen
    Equity-Punkte sind weiterhin gültige Messungen, nur das Feld, an dem
    exchange_kind_mismatch() sich stört, war veraltet."""
    info = start_info()
    if info is None:
        raise BotError("Kein Start-Anker vorhanden - initialize() zuerst.")
    info["exchange_kind"] = current_mode()
    db.set_meta("bot_start", json.dumps(info))
    return info


def reset():
    """Löscht den kompletten Bot-Zustand (Positionen, Orders, Entscheidungen,
    Equity-Verlauf, Kill-Switch, Kosten-Zähler). Explizite Nutzeraktion
    (views/trading_bot.py, Phase E) - passiert nie automatisch, analog zu
    shadow.reset()."""
    db.clear_bot_state()


# --- ZUSTANDS-LESEFUNKTIONEN (dünne, typisierte Wrapper über db.py) ---

def open_positions() -> list[dict]:
    return db.list_open_bot_positions()


# Echte Handels-Intents fuer das Tageslimit - NICHT exchange_adopt/
# exchange_reconciled_close (Reconciliation, kein vom Bot ausgeloester Trade,
# siehe db.count_bot_orders_today's Docstring).
_REAL_TRADE_INTENTS = ("open_long", "open_short", "close")

# Toleranz fuer "voll gefuellt" bei close_position() - Hyperliquid rundet
# Groessen (data.hyperliquid._round_size), ein 1e-9-relatives Restchen ist
# Gleitkomma-Rauschen, keine echte Teilfuellung.
_CLOSE_FILL_EPSILON = 1e-6


def trades_today(now: datetime | None = None) -> int:
    """Zählt ALLE gefüllten ECHTEN Trade-Orders (Eröffnungen UND
    Schliessungen) - jede ist ein echter Trade mit echten Kosten, nicht nur
    Eröffnungen. Reconciliation-Zeilen (Adoption, Phantom-Schluss) zählen
    bewusst nicht mit - sie sind kein vom Bot ausgelöster Trade und dürfen
    das Tagesbudget MAX_TRADES_PER_DAY nicht durch bloße Buchhaltung
    aufbrauchen."""
    now = now or clock.now_utc()
    return db.count_bot_orders_today(status="filled", day=now.date().isoformat(),
                                     intents=_REAL_TRADE_INTENTS)


def last_closed_at(symbol: str) -> datetime | None:
    # clock.parse_utc() statt datetime.fromisoformat(): schuetzt gegen einen
    # tz-naiven Altwert (siehe bot_runner._maybe_refresh_flows fuer den
    # reproduzierten Absturz derselben Bugklasse) - kuenftige Schreibpfade
    # sind zwar seit Phase 1 durchgehend tz-aware, aber diese Funktion soll
    # auch einen aelteren/manuell veraenderten Wert nicht zum Fehler machen.
    return clock.parse_utc(db.last_closed_bot_position_at(symbol))


def equity_start_usd() -> float | None:
    info = start_info()
    return float(info["equity_start_usd"]) if info else None


def adjusted_start_usd() -> float | None:
    """equity_start_usd() bereinigt um seither erfasste Ein-/Auszahlungen -
    der sicherheitskritische Anker für Equity-Boden (check_equity_floor) und
    90-Tage-Kriterium. Ohne diese Korrektur löst eine Auszahlung den Boden
    ohne jeden Handelsverlust aus, eine Einzahlung schiebt ihn faktisch außer
    Reichweite. net_pnl_usd/pnl_usd bleiben davon unberührt (siehe
    performance_summary())."""
    start = equity_start_usd()
    if start is None:
        return None
    latest = db.latest_bot_equity()
    net_flows = float((latest or {}).get("net_flows_usd") or 0.0)
    return start + net_flows


def equity_start_of_day_usd(now: datetime | None = None) -> float | None:
    """Erster Equity-Punkt des heutigen Tages - fällt auf das
    Bot-Gesamt-Startkapital zurück, wenn heute noch kein Zyklus gelaufen ist
    (erster Tick des Tages: kein Tagesverlust messbar, also keine Sperre)."""
    now = now or clock.now_utc()
    row = db.first_bot_equity_of_day(now.date().isoformat())
    if row:
        return float(row["equity_usd"])
    return equity_start_usd()


def performance_summary(now: datetime | None = None) -> dict | None:
    """Verdichtet den tatsächlich aufgezeichneten Netto-Verlauf für die UI.

    Gebühren und Funding sind bereits in der Equity enthalten. Die getrennten
    Kostenfelder bleiben trotzdem sichtbar, damit ein kleiner Gewinn nicht als
    Erfolg erscheint, obwohl er allein von der Kostenreibung aufgezehrt wird.
    Das 90-Tage-Abbruchkriterium rechnet absichtlich in USD: Das Handelskonto
    lautet auf USDC, daher darf ein EUR/USD-Wechselkurs die Risikoregel nicht
    auslösen oder verdecken.
    """
    info = start_info()
    latest = db.latest_bot_equity()
    if not info or not latest:
        return None

    start_usd = float(info.get("equity_start_usd") or 0.0)
    current_usd = float(latest.get("equity_usd") or 0.0)
    fees_usd = float(latest.get("fees_cum_usd") or 0.0)
    funding_net_usd = float(latest.get("funding_cum_usd") or 0.0)
    net_flows_usd = float(latest.get("net_flows_usd") or 0.0)
    # Negatives Funding ist ein Ertrag und darf die tatsächlich gezahlten
    # Gebühren nicht künstlich kleinrechnen.
    costs_usd = fees_usd + max(funding_net_usd, 0.0)
    # Reines Kontostand-Delta: ENTHÄLT Ein-/Auszahlungen. Bleibt als
    # Anzeigewert erhalten, wird in der UI aber genau so beschriftet - früher
    # stand die daraus abgeleitete Prozentzahl unkommentiert neben dem
    # bereinigten PnL, sodass eine Einzahlung wie eine Rendite aussah.
    net_pnl_usd = current_usd - start_usd
    # Ein-/Auszahlungs-bereinigtes Lebenszeit-PnL: anders als net_pnl_usd
    # zieht dies eingezahltes Kapital ab bzw. rechnet ausgezahltes wieder
    # hinzu, damit ein Kapitalfluss nie als Handelsgewinn/-verlust erscheint.
    # Gebühren stecken bereits in current_usd (Equity sinkt beim Zahlen),
    # eine zusätzliche Abzugsrechnung ist daher nicht nötig.
    pnl_usd = current_usd - start_usd - net_flows_usd
    # Sicherheitskritischer Anker für das 90-Tage-Kriterium: um Ein-/Aus-
    # zahlungen bereinigt (wie adjusted_start_usd()), damit eine Auszahlung
    # den Boden nicht ohne jeden Handelsverlust auslöst und eine Einzahlung
    # ihn nicht künstlich verschiebt.
    adjusted_start_for_floor = start_usd + net_flows_usd
    # Bruttogewinn VOR Kosten - auf dem bereinigten Handels-PnL, nicht auf dem
    # Kontostand-Delta. Diese Zahl ist NICHT nur Anzeige: sie speist das
    # 90-Tage-Kriterium costs_over_double_gross_profit. Unbereinigt hat eine
    # Einzahlung den "Bruttogewinn" aufgeblasen und damit genau die
    # Abbruchregel entschärft, die eine unrentable Kostenstruktur aufdecken
    # soll. Mehrere Kapitalflüsse würden streng genommen eine zeitgewichtete
    # Rendite verlangen; bei bislang EINEM Fluss steht der Aufwand nicht im
    # Verhältnis - die Differenzrechnung ist hier exakt genug.
    gross_profit_usd = pnl_usd + costs_usd

    try:
        started = date.fromisoformat(str(info["date"]))
        days_running = max(0, ((now or clock.now_utc()).date() - started).days)
    except (KeyError, TypeError, ValueError):
        days_running = 0

    return {
        "start_usd": start_usd,
        "current_usd": current_usd,
        "current_eur": float(latest.get("equity_eur") or 0.0),
        "net_pnl_usd": net_pnl_usd,
        "net_return_pct": (net_pnl_usd / start_usd * 100) if start_usd else None,
        # Bereinigte Rendite - die Zahl, die tatsächlich "wie gut handelt der
        # Bot" beantwortet. net_return_pct daneben enthält Ein-/Auszahlungen.
        "return_pct": (pnl_usd / adjusted_start_for_floor * 100
                       if adjusted_start_for_floor else None),
        "fees_usd": fees_usd,
        "funding_net_usd": funding_net_usd,
        "net_flows_usd": net_flows_usd,
        "pnl_usd": pnl_usd,
        "costs_usd": costs_usd,
        "gross_profit_usd": gross_profit_usd,
        "days_running": days_running,
        "evaluation_due": days_running >= 90,
        "equity_below_80_pct": (current_usd < adjusted_start_for_floor * 0.8
                                if adjusted_start_for_floor else False),
        "costs_over_double_gross_profit": costs_usd > max(gross_profit_usd, 0.0) * 2,
    }


def abort_reasons(summary: dict | None = None) -> list[str]:
    """Gründe des vorab festgelegten 90-Tage-Abbruchs, sonst eine leere Liste.

    Diese Funktion wird sowohl vom Runner als auch von der UI verwendet. So
    kann die Anzeige niemals etwas anderes als die tatsächliche Notbremse
    behaupten.
    """
    summary = summary if summary is not None else performance_summary()
    if not summary or not summary["evaluation_due"]:
        return []
    reasons = []
    if summary["equity_below_80_pct"]:
        reasons.append("90-Tage-Abbruch: Netto-Equity unter 80 % des Startkapitals")
    if summary["costs_over_double_gross_profit"]:
        reasons.append("90-Tage-Abbruch: Kosten über dem Doppelten des Bruttogewinns")
    return reasons


CHART_START_META_KEY = "bot_chart_start"


def chart_start() -> datetime | None:
    """Beginn der Verlaufs-Charts (nur Anzeige). Frühere Messpunkte bleiben in
    der DB und in allen Kennzahlen, fließen aber nicht in die Charts ein."""
    raw = db.get_meta(CHART_START_META_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _equity_rows_since(rows: list[dict], since: datetime | None) -> list[dict]:
    """Nur Messpunkte ab ``since`` - reine Anzeige-Einschränkung, die DB bleibt vollständig.

    utc=True behandelt tz-naive Altzeilen als UTC (so wurden sie geschrieben).
    """
    if since is None or not rows:
        return rows
    cutoff = pd.Timestamp(since)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    stamps = pd.to_datetime([r.get("ts") for r in rows], errors="coerce", utc=True)
    return [r for r, ts in zip(rows, stamps) if pd.notna(ts) and ts >= cutoff]


def performance_index_df(btc_prices_eur: pd.Series | None = None, *,
                         equity_rows: list[dict] | None = None,
                         since: datetime | None = None) -> pd.DataFrame | None:
    """Netto-Index (Start = 100) mit optionalem BTC-Buy-&-Hold-Vergleich.

    ``btc_prices_eur`` bleibt ein Eingabewert statt eines Netzaufrufs. Das
    hält die Engine deterministisch und testbar; die UI kann bei Bedarf die
    vorhandene Kurs-Historie laden. Fehlt sie, bleibt der Bot-/EUR-Vergleich
    vollständig nutzbar.

    ``since`` lässt alle Reihen erst ab diesem Zeitpunkt beginnen und dort
    neu auf 100 starten (Startwert, Kapitalflüsse und BTC-Basis ab dort).
    """
    rows = equity_rows if equity_rows is not None else db.list_bot_equity()
    rows = _equity_rows_since(rows, since)
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    if frame.empty or "ts" not in frame:
        return None
    frame["Zeitpunkt"] = pd.to_datetime(frame["ts"], errors="coerce")
    frame = frame.dropna(subset=["Zeitpunkt"]).sort_values("Zeitpunkt")
    # Mehrere schnelle Runner-Aufrufe können wegen der Speicherung mit
    # Sekundenauflösung denselben Zeitstempel erhalten. Für den Chart darf
    # dieser Index nicht doppelt sein: Plotly würde sonst künstliche
    # Millisekunden-Abstände (z.B. 22:14:58.9995) auf die X-Achse setzen.
    # Der letzte Wert ist der jüngste Messstand dieses Zeitpunkts.
    frame = frame.drop_duplicates(subset=["Zeitpunkt"], keep="last")
    if frame.empty:
        return None

    first_eur = float(frame["equity_eur"].iloc[0])
    if first_eur <= 0:
        return None
    # Kapitalfluss-Bereinigung wie in performance_summary() (dort in USD) -
    # hier zusaetzlich auf EUR uebertragen, weil DIESER Chart in EUR
    # indiziert. Ohne sie erscheint eine Ein-/Auszahlung als Renditesprung.
    # net_flows_usd ist je Zeile die KUMULIERTE Summe seit Bot-Start; die
    # Differenz zur ersten Zeile ist der seither geflossene Betrag. Fuer die
    # USD->EUR-Umrechnung gibt es keine historischen Kurse je Fluss-Ereignis
    # - der aus equity_eur/equity_usd DERSELBEN Zeile abgeleitete Kurs ist
    # eine Naeherung, aber die einzige ohne neue Datenquelle verfuegbare.
    flows_usd = frame.get("net_flows_usd")
    if flows_usd is not None:
        flows_usd = flows_usd.astype(float).fillna(0.0)
        equity_usd = frame["equity_usd"].astype(float)
        fx_eur_per_usd = (frame["equity_eur"].astype(float) / equity_usd.replace(0, pd.NA)).fillna(0.0)
        flow_delta_usd = flows_usd - float(flows_usd.iloc[0])
        adjusted_eur = frame["equity_eur"].astype(float) - flow_delta_usd * fx_eur_per_usd
    else:
        adjusted_eur = frame["equity_eur"].astype(float)
    result = pd.DataFrame(index=pd.DatetimeIndex(frame["Zeitpunkt"], name="Zeitpunkt"))
    result["Bot netto"] = adjusted_eur.to_numpy() / first_eur * 100
    result["EUR halten"] = 100.0

    if btc_prices_eur is None or btc_prices_eur.empty:
        return result
    prices = pd.Series(btc_prices_eur).dropna().copy()
    if prices.empty:
        return result
    prices.index = pd.to_datetime(prices.index, errors="coerce")
    prices = prices[~prices.index.isna()]
    if prices.empty:
        return result
    # `result.index` (aus bot_equity.ts) ist seit core.clock (Phase 1) TZ-AWARE
    # UTC, sofern die Zeilen ueber clock.iso_utc() entstanden - btc_prices_eur
    # kommt aus einer unabhaengigen Quelle (data.crypto_history) und ist reine
    # Kalendertage, typischerweise TZ-NAIV. Ohne Angleichung AN DEN TATSAECHLICHEN
    # ZUSTAND VON result.index wirft reindex()/union() weiter unten "Cannot
    # compare tz-naive and tz-aware timestamps" - bislang unbemerkt, weil
    # bot_equity seit dem V2-Reset (Phase 0) bis zu diesem Bug leer war.
    if result.index.tz is None:
        if prices.index.tz is not None:
            prices.index = prices.index.tz_localize(None)
    elif prices.index.tz is None:
        prices.index = prices.index.tz_localize("UTC")
    else:
        prices.index = prices.index.tz_convert(result.index.tz)
    prices.index = prices.index.normalize()
    prices = prices[~prices.index.duplicated(keep="last")].sort_index()
    point_days = result.index.normalize()
    # Equity wird intraday erfasst und darf deshalb mehrere Messpunkte am
    # selben Kalendertag enthalten. Für den BTC-Lookup brauchen wir dagegen
    # einen eindeutigen Index: pandas verweigert ``reindex`` sonst mit
    # "cannot reindex on an axis with duplicate labels".
    lookup_days = point_days.drop_duplicates()
    # Der letzte bekannte Tageskurs wird verwendet. Damit wird nie ein
    # zukünftiger Schlusskurs in einen früheren Bot-Zeitpunkt zurückgerechnet.
    btc_by_day = (
        prices.reindex(prices.index.union(lookup_days))
        .sort_index()
        .ffill()
        .reindex(lookup_days)
    )
    # Danach wieder auf alle intraday Messpunkte expandieren; der Quellindex
    # bleibt eindeutig, auch wenn ``point_days`` bewusst Duplikate enthält.
    btc_at_points = btc_by_day.reindex(point_days)
    if btc_at_points.isna().any() or float(btc_at_points.iloc[0]) <= 0:
        return result
    result["BTC Buy & Hold"] = btc_at_points.to_numpy() / float(btc_at_points.iloc[0]) * 100
    return result


def performance_eur_df(btc_prices_eur: pd.Series | None = None, *,
                       since: datetime | None = None) -> pd.DataFrame | None:
    """Dieselben Reihen wie performance_index_df(), aber als Wert in EUR.

    Jede Index-Reihe wird mit dem Startwert skaliert (Index 100 = Start-Equity),
    bleibt also kapitalflussbereinigt und direkt mit BTC/EUR-halten vergleichbar.
    Zusätzlich zeigt "Kontostand" die tatsächliche Equity inkl. Ein-/Auszahlungen -
    ohne diese Reihe passt der Chart nach einer Einzahlung nicht mehr zur
    Netto-Equity-Kennzahl darüber.
    """
    # Der Runner schreibt parallel neue Messpunkte. Beide Ansichten muessen
    # denselben DB-Stand verwenden, sonst kann der rohe Kontostand gegen einen
    # anderen Zeitindex als die bereinigten Vergleichsreihen geraten.
    rows = _equity_rows_since(db.list_bot_equity(), since)
    perf = performance_index_df(btc_prices_eur, equity_rows=rows)
    if perf is None:
        return None
    # Gleicher Zeitpunkt-Aufbau wie in performance_index_df() (sortiert, pro
    # Zeitstempel der letzte Stand), damit Kontostand exakt auf dessen Index passt.
    frame = pd.DataFrame(rows)
    frame["Zeitpunkt"] = pd.to_datetime(frame["ts"], errors="coerce")
    frame = (frame.dropna(subset=["Zeitpunkt"]).sort_values("Zeitpunkt")
             .drop_duplicates(subset=["Zeitpunkt"], keep="last"))
    equity_eur = pd.Series(frame["equity_eur"].astype(float).to_numpy(),
                           index=pd.DatetimeIndex(frame["Zeitpunkt"], name="Zeitpunkt"))
    result = perf * (float(equity_eur.iloc[0]) / 100.0)
    result["Kontostand"] = equity_eur.reindex(result.index).to_numpy()
    return result


def _max_reasonable_notional(equity_usd: float, limits: dict) -> float:
    """Grenze für core.bot_guards.check_order_plausibility: doppelt so groß
    wie die größte laut Limits ERLAUBTE Position - alles darüber ist ein
    Rechenfehler, keine legitime Strategieentscheidung."""
    return equity_usd * (limits["max_position_pct"] / 100.0) * 2.0


def _max_reasonable_exit_notional(equity_usd: float, limits: dict) -> float:
    """Plausibilitätsgrenze für SCHLIESSUNGEN - bewusst NICHT an
    `max_position_pct` gekoppelt.

    Vorher teilte sich der Ausstieg diese Grenze mit dem Einstieg. Das ist
    genau falsch herum: Bricht die Equity ein, schrumpft die Grenze mit ihr,
    während die zu schliessende Position ihren Nominalwert behält - der
    Notausstieg wird also ausgerechnet in dem Moment blockiert, für den er
    gedacht ist. Nachweisbar am Equity-Boden: bei 25 % Positionslimit lag die
    Grenze nach einem -75-%-Einbruch bei 49,95 $, die Schliessung über 50,00 $
    wurde als "unplausibel hoch" abgelehnt und die Position blieb offen.

    Diese Grenze ist deshalb nur noch ein Größenordnungs-Filter gegen
    Einheiten-/Kommafehler: Sie fängt eine um Zehnerpotenzen falsche Zahl ab,
    kann aber keinen legitimen Ausstieg mehr verhindern.
    """
    return max(float(equity_usd or 0.0), 10.0) * float(limits["max_leverage"]) * 5.0


def _min_notional_usd() -> float:
    """Lazy-Import wie bei data.fx unten - hält dieses Modul frei von einem
    Import-Zeit-Zwang auf das Hyperliquid-SDK."""
    from data.hyperliquid import MIN_NOTIONAL_USD
    return MIN_NOTIONAL_USD


def _open_notional_usd(exchange, positions) -> float:
    """Summe der Nominalen aller bereits offenen Positionen zu aktuellen
    Kursen - für core.bot_guards.check_total_exposure. Fällt pro Position auf
    den Einstandskurs zurück, falls kein Live-Preis verfügbar ist (dasselbe
    Muster wie close_position()'s price_estimate) - ein einzelner fehlender
    Kurs darf den Guard nicht zum Scheitern bringen."""
    total = 0.0
    for p in positions:
        try:
            price = exchange.mid_price(p.symbol)
        except Exception:
            price = None
        total += (price or p.entry_px) * p.size
    return total


def _open_portfolio_heat_usd() -> float:
    """Summe der Anfangs-Risiken (|Einstand - urspruenglicher Stop| * Groesse)
    aller offenen Bot-Positionen - fuer core.bot_guards.check_portfolio_heat().
    Nutzt bewusst initial_stop_px (unveraenderlich), nicht das ggf. seither
    nachgezogene stop_px: die Kennzahl soll das urspruenglich eingegangene
    Risiko zeigen, nicht die aktuelle Momentaufnahme - sonst wuerde eine Serie
    guter Trailing-Stops die Grenze fuer NEUE Positionen kuenstlich weiten.
    Positionen ohne initial_stop_px (vor Einfuehrung des Felds uebernommen)
    tragen 0 bei - ein fehlender Wert darf den Guard nicht lautlos blockieren."""
    total = 0.0
    for p in db.list_open_bot_positions():
        stop = p.get("initial_stop_px")
        if stop is None:
            continue
        total += abs(float(p["entry_px"]) - float(stop)) * float(p["size"])
    return total


def _same_side_open_count(side: str) -> int:
    return sum(1 for p in db.list_open_bot_positions() if p["side"] == side)


def _loss_streak_state() -> tuple[int, datetime | None]:
    """Anzahl aufeinanderfolgender Verlierer unter den zuletzt geschlossenen
    BOT-Positionen (juengste zuerst) und der Schliesszeitpunkt des juengsten
    davon - fuer core.bot_guards.check_loss_streak()."""
    streak = 0
    last_loss_at = None
    for row in db.recent_closed_bot_positions(limit=10):
        pnl = row["realized_pnl_usd"]
        if pnl is None or pnl >= 0:
            break
        streak += 1
        if last_loss_at is None:
            last_loss_at = clock.parse_utc(row["closed_at"])
    return streak, last_loss_at


def _positions_over_leverage_limit(limits: dict) -> list[str]:
    """Symbole, deren bereits offene Position ueber dem AKTUELLEN
    Hebel-Limit liegt (Regler seither runtergesetzt, oder eine automatisch
    uebernommene Fremdposition kam schon zu hoch gehebelt herein).

    Eigene Funktion statt nur ein Codeblock in existing_position_limit_warnings():
    anders als eine Exposure-Verletzung (die neue Einstiege ohnehin implizit
    ueber check_total_exposure blockiert, jede zusaetzliche Order laesst die
    Summe nur weiter wachsen) blockiert eine reine Hebel-Verletzung KEINE
    neuen Einstiege von selbst - check_leverage prueft nur den Hebel DER
    NEUEN Order, nicht den bereits offener, unabhaengiger Positionen. Ohne
    einen expliziten Check koennte der Bot also munter neue, korrekt
    gehebelte Positionen eroeffnen, waehrend das Depot als Ganzes weiter
    ueber dem Limit liegt."""
    max_lev = float(limits.get("max_leverage", 999.0))
    return sorted({p["symbol"] for p in db.list_open_bot_positions()
                  if float(p["leverage"] or 1.0) > max_lev + 1e-9})


def existing_position_limit_warnings(exchange, limits: dict) -> list[str]:
    """Bestehende Positionen JEDEN Takt gegen die AKTUELLEN Limits prüfen -
    unabhängig davon, ob sie beim Eröffnen oder bei einer Übernahme zulässig
    waren. Ein nachträglich verschärftes Risikolimit (Regler runtergesetzt,
    Symbol-Universum geändert) oder eine automatisch übernommene Fremd-
    position mit hohem Hebel kann sonst dauerhaft unbemerkt über den
    aktuellen Regeln liegen.

    WARNT NUR - schliesst nichts automatisch. Eine bestehende Position gegen
    ein seither verschärftes Limit zwangsweise zu schliessen wäre selbst ein
    Risiko-Ereignis (Timing, Gebühren, Slippage) und widerspricht demselben
    Grundsatz wie evaluate_adoption/evaluate_exit_order: eine bestehende
    Position abzusichern oder stehenzulassen darf nicht an nachträglich
    verschärften Taktregeln scheitern. Ein bereits über dem Gesamt-Exposure-
    Limit liegendes Depot blockiert neue Einstiege ohnehin implizit über
    core.bot_guards.check_total_exposure (jede zusätzliche Order lässt die
    Summe nur weiter wachsen) - hier geht es um SICHTBARKEIT, nicht um eine
    zusätzliche Sperre. EINE AUSNAHME: eine reine Hebel-Verletzung blockiert
    NICHT von selbst (siehe _positions_over_leverage_limit) - run_signal_cycle()
    sperrt deshalb zusätzlich aktiv neue Einstiege, solange sie besteht."""
    positions = db.list_open_bot_positions()
    if not positions:
        return []
    warnings = []

    max_lev = float(limits.get("max_leverage", 999.0))
    over_lev = _positions_over_leverage_limit(limits)
    if over_lev:
        warnings.append(f"Hebel über dem aktuellen Limit ({max_lev:g}x): "
                        + ", ".join(over_lev))

    max_positions = int(limits.get("max_positions", 999))
    if len(positions) > max_positions:
        warnings.append(f"{len(positions)} offene Positionen über dem aktuellen "
                        f"Limit ({max_positions}).")

    try:
        equity = exchange.account_state().equity_usd
    except Exception:
        equity = None
    if equity and equity > 0:
        # Ueber die DB-Positionen rechnen (mit AKTUELLEM mid_price bewertet),
        # nicht ueber exchange.account_state().positions - dieselbe Quelle
        # wie die Leverage-/Anzahl-Pruefung oben. Das haelt alle drei Checks
        # konsistent auf "was der Bot selbst verwaltet", unabhaengig davon,
        # ob eine Boersen-Anbindung dieselbe Position ebenfalls kennt.
        notional = sum((exchange.mid_price(p["symbol"]) or p["entry_px"]) * p["size"]
                       for p in positions)
        max_exposure_pct = float(limits.get("max_total_exposure_pct", 999999.0))
        current_pct = notional / equity * 100
        if current_pct > max_exposure_pct + 1e-9:
            warnings.append(f"Gesamt-Exposure {current_pct:.0f}% über dem aktuellen "
                            f"Limit ({max_exposure_pct:.0f}%).")
    return warnings


# Lookback fuer den Standard-Kerzenlieferanten (Phase 4, V2-Signal-Engine):
# 4h-Kerzen statt der fruehren 1h-Kerzen. ~145 Kerzen bei 500h/4h - komfortabel
# ueber core.bot_signals.MIN_CANDLES (90) hinaus UND ueber MAX_HOLD_HOURS
# (336h/14 Tage), damit trailing_stop_px() das "hoechstes Hoch SEIT
# EROEFFNUNG"-Fenster (core.bot_signals.trailing_stop_px) fuer die maximal
# moegliche Haltedauer noch findet.
_CANDLE_LOOKBACK_HOURS = 500.0


def _default_candles_fn():
    """Produktions-Kerzenlieferant fuer alle drei Aufrufer unten (Ent-
    scheidungs-Scan, Ausstiege, Trailing, Adoption) - eine Stelle statt drei
    fast identischer Lazy-Imports, die sonst leicht auseinanderlaufen."""
    from data.hyperliquid import candles_df as _candles_df

    def _fn(symbol):
        return _candles_df(symbol, interval="4h", lookback_hours=_CANDLE_LOOKBACK_HOURS)
    return _fn


# Rückfall-Pool (liquide, etablierte Coins), falls core.bot_universe noch
# keinen dynamischen Pool geliefert hat (allererster Lauf, CoinGecko
# dauerhaft nicht erreichbar, kaputter Meta-Wert). Wird zur Laufzeit gegen
# data.hyperliquid.supported_symbols() gefiltert (nicht jeder Coin ist dort
# als Perp gelistet) und um bereits offene Positionen ergänzt, damit die auch
# bei einer Änderung des Pools weiter bewertet und geschlossen werden können.
CANDIDATE_UNIVERSE = ("BTC", "ETH", "SOL", "LINK", "ADA", "XRP", "LTC",
                      "AVAX", "DOT", "ATOM", "NEAR")


def candidate_symbols() -> list[str]:
    """Liegt hier statt in agents/trader.py, weil jetzt die Signal-Engine der
    Haupt-Nutzer ist - die KI ist nur noch Aufsicht und soll keine
    Voraussetzung für den Handelsbetrieb mehr sein.

    Nutzt den von core.bot_universe laufend ermittelten Pool (Marktkap. +
    Alter + Hyperliquid-Liquidität). Nur ohne jeglichen verwertbaren
    Snapshot (erster Lauf/defekter Cache) wird auf CANDIDATE_UNIVERSE
    zurückgefallen; ein erfolgreich ermittelter Leer-Pool bedeutet bewusst
    "keine neuen Kandidaten".

    core.bot_universe.BLOCKED_SYMBOLS wird HIER zusaetzlich zur Filterung im
    Pool selbst geprueft (defense in depth) - greift damit auch im
    CANDIDATE_UNIVERSE-Rueckfall und unabhaengig davon, wie der Pool zustande
    kam. Eine bereits offene Position in einem gesperrten Symbol bleibt
    trotzdem ueber die Positions-Schleife unten erreichbar, sonst liesse sie
    sich nicht mehr schliessen."""
    from core import bot_universe
    from data import hyperliquid

    pool = bot_universe.pool_symbols()
    seed = pool if pool else ([] if bot_universe.has_active_snapshot()
                               else list(CANDIDATE_UNIVERSE))
    seed = [s for s in seed if s not in bot_universe.BLOCKED_SYMBOLS]
    try:
        supported = hyperliquid.supported_symbols()
    except Exception:
        supported = set(seed)
    candidates = [s for s in seed if s in supported][:bot_universe.MAX_ACTIVE_CANDIDATES]
    for pos in db.list_open_bot_positions():
        if pos["symbol"] not in candidates:
            candidates.append(pos["symbol"])
    return candidates


def _accumulate_fee(fee_usd: float | None):
    if not fee_usd:
        return
    current = float(db.get_meta("bot_fees_cum_usd") or 0.0)
    db.set_meta("bot_fees_cum_usd", str(current + fee_usd))


def _apply_funding_if_paper(exchange, interval_hours: float) -> float:
    """PaperExchange muss Funding manuell nachbilden (siehe
    data.hyperliquid.PaperExchange.apply_funding), ein echtes Hyperliquid-
    Konto verrechnet es automatisch stündlich selbst - LiveExchange hat die
    Methode deshalb bewusst nicht, `getattr` erkennt das ohne isinstance-
    Verzweigung."""
    apply_fn = getattr(exchange, "apply_funding", None)
    if apply_fn is None:
        return 0.0
    return apply_fn(interval_hours)


def _cancel_stop_if_supported(exchange, symbol: str, oid: str | None) -> bool:
    """Storniert die zur Position gehörende Stop-Order NACH einem
    erfolgreichen Close - sonst bleibt sie als verwaiste reduce-only-Order
    im Buch stehen und könnte eine spätere, neue Position im selben Symbol
    zu einem Kurs schliessen, der mit ihr nichts zu tun hat (siehe
    phase_f_shot.py, das dasselbe Muster für den Scharfschuss-Test nutzt).
    Best effort: ein Fehler hier darf den bereits abgeschlossenen Close
    nicht rückgängig machen, nur loggen. Kein Teil von ExchangeProtocol -
    PaperExchange hat nichts zu stornieren (ihr Stop ist nur ein
    Platzhalter-String, kein echtes Orderbuch-Objekt)."""
    if not oid:
        return True
    cancel_fn = getattr(exchange, "cancel_order", None)
    if cancel_fn is None:
        return True
    try:
        return bool(cancel_fn(symbol, oid))
    except Exception:
        return False


def _cost_window_since_ms(info: dict) -> int:
    """Präziser Fensterbeginn (ms seit Epoch) für Börsenabfragen seit
    Bot-Start - WICHTIG bei Ein-/Auszahlungen: eine Einzahlung kurz VOR dem
    eigentlichen initialize()-Aufruf, aber am selben Kalendertag, steckt
    bereits in equity_start_usd und darf nicht nochmal als 'Fluss seit
    Start' gezählt werden (sonst wird sie doppelt gerechnet - einmal
    implizit im Startkapital, einmal explizit als Zufluss). Reihenfolge:
    1. info['started_at'] (ab dieser Änderung immer vorhanden),
    2. der allererste bot_equity-Punkt (von initialize() im selben
       Atemzug geschrieben - deckt bestehende Installationen von VOR
       dieser Änderung ab, ohne bot_start neu schreiben zu müssen),
    3. Mitternacht (UTC) des Start-Kalendertags als letzter Rückfall (nur
       für Gebühren/Funding unkritisch, siehe deren eigener Kommentar)."""
    raw = info.get("started_at")
    if raw:
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    first = db.first_bot_equity()
    if first and first.get("ts"):
        try:
            return int(datetime.fromisoformat(first["ts"]).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    start_date = date.fromisoformat(str(info["date"]))
    return int(datetime.combine(start_date, datetime.min.time(),
                                tzinfo=timezone.utc).timestamp() * 1000)


def refresh_live_costs(exchange) -> bool:
    """Ersetzt die Kosten-Zähler durch die ECHTEN Werte von der Börse - NUR
    für LiveExchange sinnvoll (erkannt am selben Duck-Typing-Signal wie
    _apply_funding_if_paper). Überschreibt statt zu addieren (im Gegensatz
    zu _accumulate_fee, die laufend nur SCHÄTZUNGEN akkumuliert) - so bleibt
    ein wiederholter Aufruf idempotent, statt Kosten doppelt zu zählen.
    Zeitfenster: siehe _cost_window_since_ms() - bei einem dedizierten
    Agent-Wallet-Konto ohne Fremdaktivität deckt das die gesamte
    Bot-Historie ab. Best effort: schlägt der Börsenaufruf fehl
    (Netzwerk, Rate-Limit), bleiben die zuletzt bekannten Werte unverändert
    stehen, kein Absturz - bot_runner.py ruft das ohnehin nur einmal
    täglich auf, ein Fehlschlag wird beim nächsten Tag erneut versucht."""
    fills_fn = getattr(exchange, "recent_fills", None)
    funding_fn = getattr(exchange, "funding_payments_usd", None)
    if fills_fn is None or funding_fn is None:
        return False
    info = start_info()
    if info is None:
        return False
    try:
        since_ms = _cost_window_since_ms(info)
        fills = fills_fn(since_ms)
        fees_total = sum(float(f.get("fee", 0) or 0) for f in fills)
        funding_total = funding_fn(since_ms)
    except Exception:
        return False
    db.set_meta("bot_fees_cum_usd", str(fees_total))
    db.set_meta("bot_funding_cum_usd", str(funding_total))
    return True


def refresh_live_flows(exchange) -> bool:
    """Wie refresh_live_costs(), aber für Ein-/Auszahlungen - eigene Funktion,
    weil Kapitalflüsse begrifflich keine 'Kosten' sind. Gleiches Zeitfenster
    (_cost_window_since_ms()), gleiche Überschreiben-Semantik (idempotent
    bei wiederholtem Aufruf)."""
    flow_fn = getattr(exchange, "deposit_withdrawal_usd", None)
    if flow_fn is None:
        return False
    info = start_info()
    if info is None:
        return False
    try:
        since_ms = _cost_window_since_ms(info)
        flows_total = flow_fn(since_ms)
    except Exception:
        return False
    db.set_meta("bot_net_flows_usd", str(flows_total))
    return True


# --- ORDER-AUSFÜHRUNG: einziger Weg, wie irgendein Aufrufer (deterministischer
# Zyklus hier, später agents/trader.py) tatsächlich eine Order auslöst -
# core.bot_guards läuft IMMER zuerst, unabhängig vom Aufrufer. ---

def _emergency_flatten(exchange, symbol: str):
    """Reduce-only-Marktschluss DIREKT am Boersen-Adapter - bewusst OHNE
    mid_price, account_state, DB oder Limits. Zweite externe Pruefung
    23.09.2026: jede dieser vorgeschalteten Abfragen konnte (API-/DB-Ausfall)
    den Schliessversuch komplett verhindern, obwohl gerade dann eine
    ungeschuetzte Position auf der Boerse steht. Erhalten bleibt nur der
    Demo-Modus-Check (reiner Pfadvergleich, kein DB-Zugriff). Der Einstieg hat
    die Guards gerade passiert - ein Schliessen wird nicht erneut an
    Plausibilitaetsgrenzen gemessen (Grundsatz wie evaluate_exit_order)."""
    from data.hyperliquid import OrderResult
    if not bot_config.live_trading_allowed():
        return OrderResult(status="error", error="Demo-Modus: Order blockiert.")
    try:
        return exchange.close_position(symbol)
    except Exception as exc:
        return OrderResult(status="error", error=f"Notausstieg mit Ausnahme abgebrochen: {exc!r}")


def _handle_naked_open(exchange, symbol: str, side: str, result, flat, *, leverage: float,
                       entry_px_estimate, decision_id, confirmed_stop_px, limits) -> dict:
    """Buchhaltung NACH einem gefuellten Einstieg ohne Boersen-Stop, dessen
    Notausstieg (`flat`) bereits versucht wurde. JEDER Schritt steht in einem
    eigenen try/except: ein DB-Fehler darf weder den (schon gelaufenen)
    Notausstieg noch die Rueckgabe verhindern. Was nicht gebucht werden kann,
    wird nicht erfunden - der naechste reconcile_with_exchange()-Takt raeumt
    auf (uebernimmt eine noch offene Position mit frischem Stop)."""
    errors: list[str] = []

    def step(label, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{label}: {exc!r}")
            return None

    def _open_order():
        return db.add_bot_order(
            symbol=symbol, intent=f"open_{side}", side=side, size=result.filled_size,
            order_type="market", requested_px=entry_px_estimate, status=result.status,
            exchange_oid=result.stop_oid, fill_px=result.fill_px, fee_usd=result.fee_usd,
            error=result.error, decision_id=decision_id)

    order_id = step("Eröffnungs-Order buchen", _open_order)

    def _position_row():
        position_id = db.upsert_open_bot_position(
            symbol=symbol, side=side, size=result.filled_size, entry_px=result.fill_px,
            leverage=result.leverage or leverage, stop_px=confirmed_stop_px,
            exchange_oid=None, exchange_stop_px=confirmed_stop_px, origin="bot",
            decision_id=decision_id, strategy_version=bot_signals.STRATEGY_VERSION,
            config_hash=bot_config.config_fingerprint(limits))
        if order_id is not None:
            db.set_bot_order_position(order_id, position_id)
        return position_id

    position_id = step("Position buchen", _position_row)

    if flat.status == "filled":
        def _book_close():
            pos = db.get_bot_position(symbol)
            if pos is None:
                return None
            return _book_confirmed_close(exchange, pos, flat, flat.fill_px or result.fill_px,
                                         decision_id, "stop_setup_failed")
        booked = step("Schluss buchen", _book_close)
        closed_size = float(flat.filled_size or 0.0)
        # TEILSCHLUSS ist KEIN Erfolg (dritte externe Pruefung 23.09.2026: von
        # 0,12 wurden 0,06 geschlossen, gemeldet wurde "sofort geschlossen",
        # der Rest blieb ungeschuetzt offen - ohne Retry und Kill-Switch). Er
        # wird unabhaengig davon erkannt, ob die Buchung gelang: an der
        # gemeldeten Fuellmenge selbst UND am Rueckgabewert der Buchung.
        partial = ((booked or {}).get("status") == "partial"
                   or closed_size < float(result.filled_size or 0.0) * (1 - _CLOSE_FILL_EPSILON))
        if not partial:
            step("Aufräumen", lambda: [_cancel_stop_if_supported(exchange, symbol, o)
                                       for o in _foreign_stop_oids(exchange, symbol)])
            step("Equity-Snapshot", lambda: record_equity_snapshot(exchange))
            note = (f" BUCHUNG FEHLGESCHLAGEN ({'; '.join(errors)}) - der nächste "
                    f"Bestandsabgleich räumt auf." if errors else "")
            return {"status": "error", "position_id": position_id,
                    "reason": f"{result.error} Position aus Sicherheitsgründen sofort "
                              f"geschlossen.{note}"}
        detail = (f"nur teilweise geschlossen ({closed_size:g} von "
                  f"{float(result.filled_size or 0.0):g}) - Rest bleibt ungeschützt offen")
    else:
        detail = flat.error or flat.status
        step("Schliess-Order buchen", lambda: db.add_bot_order(
            symbol=symbol, intent="close", side=side, size=None, order_type="market",
            requested_px=entry_px_estimate, status=flat.status, error=flat.error,
            decision_id=decision_id, position_id=position_id))

    # Notausstieg gescheitert ODER nur teilweise gelungen: (Rest-)Position steht
    # UNGESCHUETZT auf der Boerse. Der lokale Stop (stop_px) schuetzt nur,
    # solange der Runner laeuft und check_stop_triggers() greift; das Retry-Flag
    # laesst diesen Check den Schluss in JEDEM 15-Minuten-Takt erneut
    # versuchen, der Kill-Switch sperrt weitere Einstiege bis zur manuellen
    # Pruefung.
    step("Wiederholungsmarke", lambda: db.set_meta(f"bot_stop_retry:{symbol}", "1"))
    step("Kill-Switch", lambda: bot_config.trip_kill_switch(
        f"{symbol}: Stop-Order fehlgeschlagen UND Notausstieg fehlgeschlagen - "
        f"Position ungeschützt ({detail})."))
    step("Protokoll", lambda: db.add_bot_decision(
        cycle_type="deterministic",
        signals_json=json.dumps({"symbol": symbol, "position_id": position_id}),
        run_id=None, action="emergency_exit_failed", executed=False,
        reason=f"{symbol}: {result.error} Notausstieg fehlgeschlagen ({detail})."))
    step("Equity-Snapshot", lambda: record_equity_snapshot(exchange))
    db_note = f" (Buchung ebenfalls fehlgeschlagen: {'; '.join(errors)})" if errors else ""
    return {"status": "error", "position_id": position_id,
            "reason": f"{result.error} NOTAUSSTIEG FEHLGESCHLAGEN: {detail}{db_note}"}


def open_position(exchange, symbol: str, side: str, notional_usd: float, leverage: float,
                  stop_distance_pct: float, decision_id: int | None = None) -> dict:
    """`stop_distance_pct`, nicht ein fertiger `stop_px`: der Aufrufer kennt
    zum Scan-Zeitpunkt nur einen Schätzpreis
    (core.bot_signals.evaluate()/entry_signal.price). Zwischen diesem Scan
    und dem tatsächlichen Fill liegen mindestens eine weitere
    Kontostand-Abfrage und die Order-Ausführung selbst - ein daraus VORAB
    berechneter Stop hätte ein anderes Risiko abgesichert als das, auf dem
    core.bot_signals.position_size_usd() die Positionsgröße bemessen hat.
    exchange.open_long/open_short berechnen den Stop deshalb selbst, ERST aus
    dem bestätigten Fill (siehe data.hyperliquid.LiveExchange.
    _place_entry_and_stop) - hier unten wird nur noch der von dort
    zurückgemeldete, bestätigte Wert gespeichert."""
    symbol = symbol.strip().upper()
    side = side.strip().lower()
    if side not in ("long", "short"):
        return {"status": "error", "reason": f"Ungültige Seite '{side}' (nur 'long'/'short')."}
    now = clock.now_utc()

    reading = equity_reading(exchange)
    equity = reading["usd"]
    is_new_symbol = db.get_bot_position(symbol) is None
    limits = bot_config.bot_limits()
    entry_px_estimate = exchange.mid_price(symbol)
    open_notional = _open_notional_usd(exchange, reading["state"].positions)

    # Erste Eroeffnung auf einem ECHTEN Konto wird gedeckelt. Sie ersetzt den
    # frueher verpflichtenden Phase-F-Scharfschuss: derselbe Nachweis (Fill,
    # Stop-OID auf der Boerse, DB-Eintrag), aber als regulaerer Trade statt
    # als separates Ritual. Hier statt im Aufrufer, damit KEIN Weg daran
    # vorbeifuehrt - egal, wer die Order auslöst.
    is_live = _exchange_kind(exchange) == "live"
    first_live = is_live and bot_config.first_live_order_pending()
    if first_live:
        notional_usd = min(notional_usd, bot_config.FIRST_LIVE_ORDER_CAP_USD)

    # Nur eine SCHAETZUNG fuer die Vorab-Plausibilitaetspruefung
    # (check_stop_loss_present prueft bloss "liegt der Stop auf der
    # richtigen Seite des Einstands", keine Risikorechnung) - der Stop, der
    # tatsaechlich gilt und gespeichert wird, kommt erst unten aus dem
    # bestaetigten Fill.
    stop_px_estimate = None
    if stop_distance_pct is not None and entry_px_estimate:
        stop_px_estimate = (
            entry_px_estimate * (1 - stop_distance_pct / 100.0) if side == "long"
            else entry_px_estimate * (1 + stop_distance_pct / 100.0))
    # Dollar-Risiko der NEUEN Order bis zu ihrem Stop - aequivalent zu
    # |entry - stop| * Groesse, aber ohne durch entry_px_estimate teilen zu
    # muessen (size ist an dieser Stelle noch unbekannt, siehe oben).
    order_heat = (notional_usd * stop_distance_pct / 100.0) if stop_distance_pct else 0.0
    consecutive_losses, last_loss_at = _loss_streak_state()

    evaluation = bot_guards.evaluate_entry_order(
        kill_switch_active=bot_config.is_killed(),
        live_trading_allowed=bot_config.live_trading_allowed(),
        order_notional_usd=notional_usd,
        max_reasonable_notional_usd=_max_reasonable_notional(equity, limits),
        equity_usd=equity,
        equity_start_usd=adjusted_start_usd() or equity,
        equity_start_of_day_usd=equity_start_of_day_usd(now) or equity,
        leverage=leverage,
        funding_rate_hourly=exchange.funding_rate_hourly(symbol),
        stop_px=stop_px_estimate, side=side, entry_px=entry_px_estimate or 0.0,
        current_open_count=len(db.list_open_bot_positions()),
        is_new_symbol=is_new_symbol,
        last_closed_at=last_closed_at(symbol), now=now,
        trades_today=trades_today(now),
        limits=limits,
        min_notional_usd=_min_notional_usd(),
        equity_trusted=reading["trusted"],
        order_heat_usd=order_heat,
        open_heat_usd=_open_portfolio_heat_usd(),
        same_side_open_count=_same_side_open_count(side),
        consecutive_losses=consecutive_losses,
        last_loss_closed_at=last_loss_at,
        open_notional_usd=open_notional,
        runner_degraded=bot_config.runner_degraded(),
    )
    if not evaluation.allowed:
        db.add_bot_order(symbol=symbol, intent=f"open_{side}", side=side, size=None,
                         order_type="market", requested_px=entry_px_estimate, status="blocked",
                         error=evaluation.reason, decision_id=decision_id)
        return {"status": "blocked", "reason": evaluation.reason}

    result = (exchange.open_long(symbol, notional_usd, leverage, stop_distance_pct)
             if side == "long"
             else exchange.open_short(symbol, notional_usd, leverage, stop_distance_pct))

    # LiveExchange-Sonderfall: Eroeffnung gefuellt, aber die Stop-Order
    # fehlgeschlagen (siehe data/hyperliquid.py) - eine ungeschuetzte Position
    # mit echtem Geld. Der Notausstieg ist deshalb die ALLERERSTE Aktion nach
    # dem Fill, vor jeder DB-Buchung und Hilfsabfrage (zweite externe Pruefung
    # 23.09.2026: ein DB- oder Kursfehler davor verhinderte ihn komplett).
    # `mark_first_live_order_done()` wird in diesem Zweig bewusst NICHT
    # aufgerufen: der Nachweis der gedeckelten ersten Live-Order (Fill, Stop
    # OHNE Fehler, DB-Eintrag) ist gescheitert - auch wenn der Notausstieg die
    # Position rettet, bleibt der Deckel bis zu einer fehlerfreien Order aktiv.
    if result.status == "filled" and result.error:
        flat = _emergency_flatten(exchange, symbol)
        confirmed_stop = result.stop_px if result.stop_px is not None else stop_px_estimate
        return _handle_naked_open(exchange, symbol, side, result, flat, leverage=leverage,
                                  entry_px_estimate=entry_px_estimate, decision_id=decision_id,
                                  confirmed_stop_px=confirmed_stop, limits=limits)

    order_id = db.add_bot_order(
        symbol=symbol, intent=f"open_{side}", side=side, size=result.filled_size,
        order_type="market", requested_px=entry_px_estimate, status=result.status,
        exchange_oid=result.stop_oid, fill_px=result.fill_px, fee_usd=result.fee_usd,
        error=result.error, decision_id=decision_id,
    )
    if result.status != "filled":
        return {"status": "error", "reason": result.error}

    # Ground Truth ist die Boerse, nicht unsere eigene Rechnung: bei einem
    # Nachkauf (existing != None) liefert result.filled_size nur die NEUE
    # Teilmenge, nicht den kombinierten Gesamtbestand samt gemitteltem
    # Einstand. Deshalb nach dem Fill die tatsaechliche Position abfragen,
    # statt Menge/Einstand selbst hochzurechnen.
    # Eine Exception der Kontostand-Abfrage zaehlt wie "nicht gefunden" - dann
    # greift die Rekonstruktion aus der Order-Antwort unten. Vorher brach hier
    # bei einem API-Ausfall die ganze Funktion ab: kein DB-Eintrag und, bei
    # fehlgeschlagenem Stop, auch kein Notausstieg (externe Pruefung
    # 23.09.2026).
    try:
        fresh = _find_position(exchange, symbol)
    except Exception:
        fresh = None
    reconstructed_from_order = False
    if fresh is None:
        # Der Fill ist bestaetigt (result.status == "filled" oben), aber
        # account_state() zeigt die Position auch nach 5 Versuchen noch
        # nicht (Ansichts-Verzoegerung/API-Ruckler). Eine fruehere Fassung
        # gab hier einen Fehler zurueck OHNE die Position je in bot_positions
        # zu schreiben - sie existierte auf der Boerse, war aber bis zum
        # naechsten reconcile_with_exchange()-Takt (der sie dann als
        # "unbekannt" neu uebernehmen muss, mit frischem Stop und frischem
        # exchange_opened_at) komplett unverwaltet: kein Trailing, keine
        # Ausstiegspruefung, keine Sichtbarkeit in der UI. Der bestaetigte
        # Fill selbst (result) traegt bereits Groesse/Kurs/Hebel - daraus
        # laesst sich die Position rekonstruieren, statt sie zu verlieren.
        # Eine sichtbar markierte Naeherung ist besser als gar keine DB-Zeile.
        from data.hyperliquid import Position as _HLPosition
        fresh = _HLPosition(symbol=symbol, side=side, size=result.filled_size,
                            entry_px=result.fill_px, leverage=result.leverage or leverage,
                            unrealized_pnl_usd=0.0)
        reconstructed_from_order = True

    # Ground Truth gilt auch fuer den Hebel: `leverage` (der ANGEFORDERTE
    # Parameter) kann vom TATSAECHLICH gesetzten abweichen, weil Hyperliquid
    # nur Ganzzahlen erlaubt (LiveExchange._ensure_leverage rundet ab, z.B.
    # 1,9x -> 1x). fresh.leverage kommt aus derselben frischen
    # account_state()-Abfrage wie size/entry_px oben und ist damit der
    # bestaetigte Wert - ohne diese Korrektur haette die DB weiterhin den
    # angeforderten, nicht den echten Hebel gezeigt.
    # Auch der Stop ist Ground Truth aus `result`, nicht die urspruengliche
    # Schaetzung: exchange.open_long/open_short berechnen ihn aus dem
    # bestaetigten Fill (siehe Docstring oben). result.stop_px sollte nach
    # einem erfolgreichen Fill immer gesetzt sein; die Schaetzung bleibt nur
    # ein defensiver Rueckfall, falls eine ExchangeProtocol-Implementierung
    # (z.B. in Tests) das Feld nicht befuellt.
    confirmed_stop_px = result.stop_px if result.stop_px is not None else stop_px_estimate

    # Verwaiste Fremd-Trigger dieses Symbols aufraeumen, BEVOR die neue
    # Position die Boerse als "nur mein eigener Stop liegt dort" behandelt.
    # Ursache: LiveExchange.replace_stop_order() schluckt best effort einen
    # fehlgeschlagenen Storno des ALTEN Stops beim Nachziehen (siehe dessen
    # Docstring) - die DB verliert dessen OID dann komplett, und ohne diesen
    # Sweep koennte der verwaiste Stop spaeter eine voellig neue Position im
    # selben Symbol zu einem Kurs schliessen, der mit ihr nichts zu tun hat.
    # Dasselbe Muster wie bei adopt_unknown_positions()/_foreign_stop_oids().
    # Reines Aufraeumen - darf eine bereits offene Position nie gefaehrden.
    def _sweep_stale_stops():
        try:
            for stale_oid in _foreign_stop_oids(exchange, symbol):
                if stale_oid != result.stop_oid:
                    _cancel_stop_if_supported(exchange, symbol, stale_oid)
        except Exception:
            pass

    _sweep_stale_stops()

    if reconstructed_from_order:
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol, "filled_size": result.filled_size,
                                    "fill_px": result.fill_px}),
            run_id=None, action="position_reconstructed_from_order", executed=True,
            reason=(f"{symbol}: Position nach bestätigtem Fill nicht im Kontostand "
                   f"gefunden - aus der Order-Antwort rekonstruiert (Größe/Einstand "
                   f"möglicherweise ungenau, wird beim nächsten Reconcile korrigiert)."),
        )
    position_id = db.upsert_open_bot_position(
        symbol=symbol, side=fresh.side, size=fresh.size, entry_px=fresh.entry_px,
        leverage=fresh.leverage, stop_px=confirmed_stop_px, exchange_oid=result.stop_oid,
        exchange_stop_px=confirmed_stop_px,
        # Herkunft und ausloesende Entscheidung gehoeren an die POSITION, nicht
        # nur an die Order: nur so ist spaeter unterscheidbar, was der Bot
        # selbst entschieden hat und was er nur vorgefunden hat.
        origin="bot", decision_id=decision_id,
        # Unter welcher Scoring-Version UND welcher effektiven Konfiguration
        # (Risikostufe + Overrides + feste Strategie-Konstanten) diese
        # Position entstand - ohne beides ist im Nachhinein nicht mehr
        # rekonstruierbar, welches Regelwerk tatsaechlich galt (siehe
        # core.bot_config.config_fingerprint()-Docstring).
        strategy_version=bot_signals.STRATEGY_VERSION,
        config_hash=bot_config.config_fingerprint(limits),
    )
    db.set_bot_order_position(order_id, position_id)
    _accumulate_fee(result.fee_usd)

    def _snapshot_best_effort():
        # Protokollierung, nicht sicherheitsrelevant - ein Ausfall hier darf
        # weder den Notausstieg noch die Rueckgabe verhindern.
        try:
            record_equity_snapshot(exchange)
        except Exception:
            pass

    _snapshot_best_effort()

    if first_live:
        # Nachweis vollstaendig erbracht (Fill bestaetigt, Stop OHNE Fehler,
        # DB-Eintrag gespeichert) - ab jetzt gilt die regulaere
        # Positionsgroesse.
        bot_config.mark_first_live_order_done()
    return {"status": "filled", "position_id": position_id}


def close_position(exchange, symbol: str, reason: str = "", decision_id: int | None = None) -> dict:
    symbol = symbol.strip().upper()
    pos = db.get_bot_position(symbol)
    if pos is None:
        return {"status": "error", "reason": f"{symbol}: keine offene Position in der DB."}

    # Alle Hilfsabfragen VOR dem Boersen-Aufruf sind reine Plausibilitaets-
    # hilfen und duerfen den Schliessversuch nie verhindern (zweite externe
    # Pruefung 23.09.2026: ein Ausfall der Kursabfrage brach die Funktion ab,
    # bevor exchange.close_position() je aufgerufen wurde).
    try:
        limits = bot_config.bot_limits()
    except Exception:
        limits = {"max_leverage": 10.0}
    try:
        price_estimate = exchange.mid_price(symbol) or pos["entry_px"]
    except Exception:
        price_estimate = pos["entry_px"]
    notional_estimate = price_estimate * pos["size"]
    # Schliessen ist risikomindernd und darf nicht an einer Kontostand-Abfrage
    # scheitern (externe Pruefung 23.09.2026: fiel account_state() nach einem
    # Fill aus, kam auch der Notausstieg nie zustande). Die Equity dient hier
    # nur der Plausibilitaetsgrenze _max_reasonable_exit_notional - ohne
    # Messung genuegt der letzte bekannte Wert, sonst der Positionswert selbst.
    try:
        equity = exchange.account_state().equity_usd
    except Exception:
        latest = db.latest_bot_equity()
        equity = float((latest or {}).get("equity_usd") or 0.0) or notional_estimate

    evaluation = bot_guards.evaluate_exit_order(
        live_trading_allowed=bot_config.live_trading_allowed(),
        order_notional_usd=notional_estimate,
        max_reasonable_notional_usd=_max_reasonable_exit_notional(equity, limits),
    )
    if not evaluation.allowed:
        db.add_bot_order(symbol=symbol, intent="close", side=pos["side"], size=pos["size"],
                         order_type="market", requested_px=price_estimate, status="blocked",
                         error=evaluation.reason, decision_id=decision_id, position_id=pos["id"])
        return {"status": "blocked", "reason": evaluation.reason}

    result = exchange.close_position(symbol)
    return _book_confirmed_close(exchange, pos, result, price_estimate, decision_id, reason)


def _book_confirmed_close(exchange, pos: dict, result, price_estimate: float,
                          decision_id: int | None, reason: str) -> dict:
    """Buchhaltung NACH dem Boersen-Aufruf einer Schliessung (Order-Zeile,
    Teil-/Vollschluss, PnL netto, Disarm, Stop-Storno, Snapshot). Gemeinsam
    genutzt von close_position() und dem Notausstieg in open_position() -
    dort laeuft der Boersen-Aufruf VOR jeder DB-Buchung (_emergency_flatten),
    die Buchung folgt erst danach."""
    symbol = pos["symbol"]
    db.add_bot_order(symbol=symbol, intent="close", side=pos["side"], size=result.filled_size,
                     order_type="market", requested_px=price_estimate, status=result.status,
                     exchange_oid=result.stop_oid, fill_px=result.fill_px, fee_usd=result.fee_usd,
                     error=result.error, decision_id=decision_id, position_id=pos["id"])
    if result.status != "filled":
        return {"status": "error", "reason": result.error}

    direction = 1 if pos["side"] == "long" else -1
    gross_pnl = (result.fill_px - pos["entry_px"]) * result.filled_size * direction

    # Teilschliessung: die Boerse hat weniger als die volle Positionsgroesse
    # gefuellt (selten, aber moeglich - z.B. Liquiditaet/Margin). Eine
    # fruehere Fassung buchte JEDE gefuellte Schliess-Order als Vollschluss:
    # db.close_bot_position() setzte status='closed', obwohl auf der Boerse
    # eine reale Restposition weiterlief - der naechste Takt uebernahm sie
    # dann ueber reconcile_with_exchange() als "unbekannt" neu, mit frischem
    # Stop und frischem exchange_opened_at (max_hold_hours faelschlich
    # zurueckgesetzt), und der bereits realisierte Teil-PnL war nirgends
    # festgehalten. `_CLOSE_FILL_EPSILON` toleriert Gleitkomma-/Rundungsrauschen
    # (Hyperliquid rundet Groessen, siehe _round_size), keine echte Teilfuellung.
    remaining = pos["size"] - result.filled_size
    if remaining > _CLOSE_FILL_EPSILON * max(pos["size"], 1e-12):
        db.reduce_bot_position_size(symbol, new_size=remaining, add_realized_pnl_usd=gross_pnl)
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol, "filled_size": result.filled_size,
                                    "remaining_size": remaining, "gross_pnl_usd": gross_pnl}),
            run_id=None, action="close_partial_fill", executed=True,
            reason=(f"{symbol}: Schliessung nur teilweise gefuellt "
                   f"({result.filled_size:g} von {pos['size']:g}) - Restposition "
                   f"({remaining:g}) bleibt offen, Stop unveraendert."),
        )
        return {"status": "partial", "position_id": pos["id"],
               "filled_size": result.filled_size, "remaining_size": remaining}

    # NETTO festhalten: die Schliessungs-Order ist oben bereits mit
    # position_id geschrieben, die Eroeffnung wurde in open_position()
    # verknuepft - die Summe deckt damit den kompletten Round-Trip ab.
    fees_usd = db.bot_position_fees_usd(pos["id"])
    realized_pnl = gross_pnl - fees_usd
    db.close_bot_position(symbol, close_px=result.fill_px, realized_pnl_usd=realized_pnl)
    # Symbol scharf/unscharf (Phase 3): erst wieder handelbar, wenn
    # _reentry_check() das Setup als nachweislich weg einstuft - siehe dessen
    # Docstring.
    db.disarm_bot_symbol(symbol, pos["side"])
    # Stop-Order der jetzt geschlossenen Position stornieren, BEVOR eine
    # neue im selben Symbol entstehen kann - verhindert einen verwaisten
    # Stop, der spaeter eine unverwandte neue Position ausloesen koennte.
    stop_cancelled = _cancel_stop_if_supported(exchange, symbol, pos["exchange_oid"])
    if pos["exchange_oid"] and not stop_cancelled:
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol}),
            run_id=None,
            action="stop_cancel_failed",
            executed=False,
            reason=f"{symbol}: Position geschlossen, Stop-OID konnte nicht sicher storniert werden.",
        )
    db.delete_meta(f"bot_stop_retry:{symbol}")
    _accumulate_fee(result.fee_usd)
    try:
        record_equity_snapshot(exchange)
    except Exception:
        pass   # Protokollierung - die Schliessung ist gebucht, nicht gefaehrden
    return {"status": "filled", "realized_pnl_usd": realized_pnl,
            "gross_pnl_usd": gross_pnl, "fees_usd": fees_usd, "reason": reason}


def close_all_positions(exchange, reason: str = "manual_close_all") -> list[dict]:
    """Schließt JEDE offene, in der DB bekannte Position über denselben
    geprüften Pfad wie ein einzelner Ausstieg (close_position()) - für den
    manuellen "Alle Positionen schließen"-Knopf (views/trading_bot.py, per
    Meta-Flag an bot_runner.py signalisiert statt direkt aus der UI: nur der
    Runner-Prozess hält die aktive Börsen-/Paper-Verbindung). Arbeitet die
    Liste unabhängig vom Ergebnis EINZELNER Schließungen komplett ab - ein
    Fehlschlag bei einem Symbol darf die anderen nicht blockieren."""
    positions = db.list_open_bot_positions()
    results = [{"symbol": p["symbol"], "result": close_position(exchange, p["symbol"], reason=reason)}
              for p in positions]
    if positions:
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbols": [p["symbol"] for p in positions],
                                    "results": [r["result"].get("status") for r in results]}),
            run_id=None, action="manual_close_all",
            executed=any(r["result"].get("status") == "filled" for r in results),
            reason=f"Manuell ausgelöst: {len(positions)} Position(en) angefragt.",
        )
    return results


# --- MANUELLES SCHLIESSEN EINER EINZELNEN POSITION (Button je Position,
# views/trading_bot.py, "Positionen bearbeiten") ---
#
# Dasselbe Meta-Flag-Muster wie bei "Alle Positionen schließen"/dem manuellen
# Stop-Nachziehen: die UI legt nur eine Anfrage ab, ausgefuehrt wird sie
# ausschliesslich vom Runner-Prozess. Anders als beim Stop-Nachziehen
# (set_manual_stop, EIN Versuch, kein stiller Retry) verhaelt sich das
# Schliessen wie close_all_positions()/_maybe_close_all(): risikomindernd,
# also wird ein Fehlschlag NICHT verworfen, sondern beim naechsten Takt
# automatisch erneut versucht - die Anfrage bleibt stehen, bis die Position
# nachweislich weg ist.
_CLOSE_REQUESTS_META_KEY = "bot_close_position_requests"
_CLOSE_RESULTS_META_KEY = "bot_close_position_results"


def close_position_requests() -> dict:
    return _read_json_meta(_CLOSE_REQUESTS_META_KEY)


def close_position_results() -> dict:
    return _read_json_meta(_CLOSE_RESULTS_META_KEY)


def request_close_position(symbol: str):
    """Von der UI aufgerufen - sendet selbst nichts, siehe Block-Kommentar."""
    symbol = symbol.strip().upper()
    requests = close_position_requests()
    requests[symbol] = {"requested_at": clock.iso_utc()}
    db.set_meta(_CLOSE_REQUESTS_META_KEY, json.dumps(requests))


def cancel_close_position_request(symbol: str):
    requests = close_position_requests()
    if requests.pop(symbol.strip().upper(), None) is not None:
        db.set_meta(_CLOSE_REQUESTS_META_KEY, json.dumps(requests))


def process_close_position_requests(exchange) -> list[dict]:
    """Vom Runner je Takt aufgerufen. Eine Anfrage wird erst geraeumt, wenn
    db.get_bot_position() DANACH tatsächlich keine offene Position mehr
    zeigt - nicht schon nach dem blossen Versuch (identisches Muster wie
    _maybe_close_all()'s "nur bei Erfolg voranschreiten"-Prinzip): ein
    Guard-Block oder Börsenfehler lässt den nächsten Takt automatisch erneut
    versuchen, statt die Anfrage spurlos zu verwerfen."""
    results = []
    for symbol, req in close_position_requests().items():
        result = close_position(exchange, symbol, reason="manual_close_single")
        still_open = db.get_bot_position(symbol) is not None
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol, "result": result.get("status")}),
            run_id=None, action="manual_close_single",
            executed=result.get("status") == "filled",
            reason=f"Manuell ausgelöst: {symbol} schließen angefragt.",
        )
        if not still_open:
            cancel_close_position_request(symbol)
        results_all = close_position_results()
        results_all[symbol] = {**result, "requested_at": req.get("requested_at"),
                               "finished_at": clock.iso_utc(), "still_open": still_open}
        db.set_meta(_CLOSE_RESULTS_META_KEY, json.dumps(results_all))
        results.append({"symbol": symbol, "still_open": still_open, **result})
    return results


# --- DETERMINISTISCHER ZYKLUS (Plan-Stufe 1: alle 15 Min, kein LLM) ---

def _foreign_stop_oids(exchange, symbol: str) -> list[str]:
    """Bereits auf der Börse liegende Trigger-Orders EINES Symbols.

    Bei einer Übernahme wird immer ein frischer Stop gesetzt. Lag dort schon
    einer (vom Nutzer von Hand platziert, oder Rest eines früheren Laufs),
    hätten danach ZWEI Stops zu unterschiedlichen Kursen auf derselben
    Position gelegen - der ältere würde beim Auslösen eine Position
    schliessen, die der Bot gerade erst zu verwalten begonnen hat. Best
    effort: kann die Liste nicht gelesen werden, wird nichts storniert (die
    Übernahme selbst hängt nicht davon ab)."""
    reader = getattr(exchange, "frontend_open_orders", None)
    if reader is None:
        return []
    try:
        orders = reader()
    except Exception:
        return []
    flatten = getattr(type(exchange), "_flatten_frontend_orders", None)
    try:
        candidates = list(flatten(orders)) if flatten else list(orders)
    except Exception:
        candidates = list(orders)
    oids = []
    for order in candidates:
        if not isinstance(order, dict) or not order.get("isTrigger"):
            continue
        if str(order.get("coin", "")).strip().upper() != symbol.strip().upper():
            continue
        oid = order.get("oid")
        if oid not in (None, ""):
            oids.append(str(oid))
    return oids


def _exchange_open_time(exchange, symbol: str) -> str | None:
    """Tatsächlicher Eröffnungszeitpunkt der AKTUELLEN Positions-Lebensphase
    einer Fremdposition aus den Fills.

    Ohne ihn bekäme eine seit Tagen offene Position beim Übernehmen eine
    frische Uhr, denn core.bot_signals.exit_signal misst `max_hold_hours`
    gegen `opened_at` - eine Position, die längst hätte auslaufen sollen,
    bliebe dadurch weitere volle 96 h liegen.

    Der FRÜHESTE Open-Fill NACH dem letzten vollständigen Close ist gesucht -
    nicht der neueste Open-Fill überhaupt. Eine frühere Fassung nahm den
    neuesten: bei einer Position mit mehreren Nachkäufen wäre das der
    letzte Nachkauf gewesen, nicht der Beginn des aktuellen Zyklus - die Uhr
    wäre dadurch JÜNGER erschienen, als die Position tatsächlich ist, exakt
    das Gegenteil dessen, was diese Funktion verhindern soll.

    Best effort: ohne Fill-Historie (PaperExchange) oder bei einem API-Fehler
    bleibt es bei None, und der Aufrufer fällt auf den Übernahmezeitpunkt
    zurück."""
    fills_fn = getattr(exchange, "recent_fills", None)
    if fills_fn is None:
        return None
    try:
        fills = fills_fn(0)
    except Exception:
        return None
    own = sorted(
        (f for f in fills if isinstance(f, dict)
         and str(f.get("coin", "")).strip().upper() == symbol.strip().upper()),
        key=lambda f: int(f.get("time", 0) or 0))
    # Ab NACH dem letzten vollstaendigen Close - alles davor gehoert zu einer
    # frueheren, laengst beendeten Positions-Lebensphase.
    last_close_idx = -1
    for i, f in enumerate(own):
        if str(f.get("dir", "")).strip().lower().startswith("close"):
            last_close_idx = i
    opens = [f for f in own[last_close_idx + 1:]
            if str(f.get("dir", "")).strip().lower().startswith("open")]
    if not opens:
        return None
    try:
        earliest_ms = min(int(f.get("time", 0) or 0) for f in opens)
    except (TypeError, ValueError):
        return None
    if earliest_ms <= 0:
        return None
    return datetime.fromtimestamp(earliest_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def backfill_exchange_opened_at(exchange) -> list[dict]:
    """Einmaliger, manuell auszulösender Nachtrag von exchange_opened_at für
    bereits VOR dieser Funktion übernommene Positionen (origin='adopted'),
    deren echter Eröffnungszeitpunkt beim Übernehmen noch nicht ermittelt
    wurde. Ohne den echten Zeitpunkt zählt max_hold_hours (core.bot_signals.
    exit_signal) ab dem Übernahme-Zeitpunkt statt der tatsächlichen
    Eröffnung - eine seit Tagen offene Position bekäme dadurch weiterhin
    eine frische Uhr.

    Nutzt dieselbe Logik wie eine NEUE Übernahme (_exchange_open_time), nur
    nachträglich und ausdrücklich pro Symbol ausgelöst statt automatisch bei
    jedem Takt - eine Börsen-Fill-Historie abzufragen kostet einen
    Netzwerk-Aufruf je betroffenes Symbol, das gehört nicht in den
    15-Minuten-Zyklus. set_bot_position_exchange_opened_at() überschreibt nie
    einen bereits bekannten Wert (WHERE exchange_opened_at IS NULL)."""
    results = []
    for pos in db.list_open_bot_positions():
        if pos.get("origin") != "adopted" or pos.get("exchange_opened_at"):
            continue
        opened_on_exchange = _exchange_open_time(exchange, pos["symbol"])
        if opened_on_exchange is None:
            results.append({"symbol": pos["symbol"], "updated": False,
                           "reason": "Kein Eröffnungs-Fill in der Börsen-Historie gefunden."})
            continue
        updated = db.set_bot_position_exchange_opened_at(pos["id"], opened_on_exchange)
        results.append({"symbol": pos["symbol"], "updated": updated,
                       "exchange_opened_at": opened_on_exchange})
    return results


def adopt_unknown_positions(exchange, live_positions: dict, unknown_symbols: list[str],
                            candles_fn=None) -> list[dict]:
    """Übernimmt Börsen-Positionen, die die DB nicht kennt, in die Verwaltung -
    NIE ohne einen frisch platzierten, verifizierten Stop und NIE ohne die
    Risiko-Prüfkette (core.bot_guards.evaluate_adoption).

    Früher lief dieser Weg an JEDEM Guard vorbei und schrieb direkt in die DB.
    Folge: fünf einzeln unauffällige Fremd-Longs summierten sich auf 139 %
    Brutto-Exposure bei einem Limit von 138 %, und ein von Hand mit hohem
    Hebel eröffneter Trade wurde mit genau diesem Hebel übernommen. Das
    bereits übernommene Nominal wächst deshalb INNERHALB der Schleife mit -
    sonst ist jede Position für sich harmlos und nur die Summe zu gross.

    Scheitert etwas für ein Symbol (Guard, kein ATR, Stop-Platzierung), wird
    GENAU DIESES Symbol nicht übernommen und der Aufrufer zieht daraufhin den
    Kill-Switch: eine Position, die der Bot nicht absichern oder nicht
    verantworten kann, gehört vor menschliche Augen, nicht in die stille
    Weiterverwaltung.

    Stop-Abstand wie bei einer neuen Position: ATR-basiert über
    core.bot_signals, ausgehend vom AKTUELLEN Kurs (nicht dem historischen
    Einstand) - die Absicherung soll den Verlust ab JETZT begrenzen, nicht
    rückwirkend den ursprünglichen Einstand schützen.
    """
    if candles_fn is None:
        candles_fn = _default_candles_fn()
    stop_fn = getattr(exchange, "replace_stop_order", None)
    limits = bot_config.bot_limits()

    reading = equity_reading(exchange)
    equity = reading["usd"]
    # Nur das BEREITS VERWALTETE Nominal als Ausgangspunkt - die noch nicht
    # übernommenen Fremdpositionen dürfen das Exposure-Budget nicht schon
    # vorab selbst ausfüllen und sich damit gegenseitig blockieren.
    managed = {p["symbol"] for p in db.list_open_bot_positions()}
    open_notional = _open_notional_usd(
        exchange, [p for p in reading["state"].positions if p.symbol in managed])
    open_count = len(managed)

    results = []
    for symbol in unknown_symbols:
        pos = live_positions[symbol]
        if stop_fn is None:
            results.append({"symbol": symbol, "adopted": False,
                           "reason": "Börse unterstützt keine Stop-Order-Platzierung."})
            continue
        try:
            r = bot_signals.readings(bot_signals.prepare(candles_fn(symbol)))
            price = exchange.mid_price(symbol) or pos.entry_px
            dist = bot_signals.stop_distance_pct(r["atr_pct"] if r else None, limits)
        except Exception as e:
            results.append({"symbol": symbol, "adopted": False,
                           "reason": f"Kursdaten nicht abrufbar: {e!r}"})
            continue
        if dist is None:
            results.append({"symbol": symbol, "adopted": False,
                           "reason": "Kein ATR verfügbar - Stop-Abstand nicht berechenbar."})
            continue
        stop_px = price * (1 - dist / 100.0) if pos.side == "long" else price * (1 + dist / 100.0)
        notional = price * pos.size

        try:
            funding = exchange.funding_rate_hourly(symbol)
        except Exception:
            funding = None
        evaluation = bot_guards.evaluate_adoption(
            live_trading_allowed=bot_config.live_trading_allowed(),
            order_notional_usd=notional,
            max_reasonable_notional_usd=_max_reasonable_notional(equity, limits),
            equity_usd=equity,
            leverage=pos.leverage,
            funding_rate_hourly=funding,
            stop_px=stop_px, side=pos.side, entry_px=price,
            current_open_count=open_count,
            limits=limits,
            equity_trusted=reading["trusted"],
            open_notional_usd=open_notional,
        )
        if not evaluation.allowed:
            db.add_bot_order(symbol=symbol, intent="exchange_adopt", side=pos.side,
                             size=pos.size, order_type=None, requested_px=price,
                             status="blocked", error=evaluation.reason)
            results.append({"symbol": symbol, "adopted": False,
                           "reason": f"Risikoprüfung abgelehnt: {evaluation.reason} "
                                     f"(kein Stop gesetzt - Position bleibt ungeschützt)"})
            continue

        # REIHENFOLGE IST SICHERHEITSRELEVANT - derselbe Grundsatz wie
        # data.hyperliquid.LiveExchange.replace_stop_order (siehe dessen
        # Docstring): ERST der neue Stop, verifiziert, DANN die Stornierung
        # der alten. Eine fruehere Fassung stornierte zuerst - schlug die
        # Platzierung des neuen Stops danach fehl, blieb die Position ohne
        # JEDEN Schutz stehen. Mit alter UND neuer Stop-Order (kurzzeitig
        # doppelt abgesichert) ist der schlimmste Fall dagegen nur ein
        # verwaister, harmloser reduce-only-Stop.
        stale_oids = _foreign_stop_oids(exchange, symbol)
        new_oid, error = stop_fn(symbol, pos.side, pos.size, stop_px, None)
        if error:
            # Alte Stop-Order(s) BEWUSST unangetastet lassen - die Position
            # bleibt dann so geschuetzt (oder ungeschuetzt), wie sie vor der
            # Uebernahme war, statt zusaetzlich zum gescheiterten neuen Stop
            # auch noch den alten zu verlieren.
            results.append({"symbol": symbol, "adopted": False,
                           "reason": f"Stop-Platzierung fehlgeschlagen: {error}"})
            continue
        cancelled = [oid for oid in stale_oids if _cancel_stop_if_supported(exchange, symbol, oid)]

        opened_on_exchange = _exchange_open_time(exchange, symbol)
        note = (f"Auf der Börse vorgefunden, Hebel {pos.leverage:g}x, "
                f"Einstand {pos.entry_px:,.4f} $.")
        if stale_oids:
            note += f" {len(cancelled)}/{len(stale_oids)} vorhandene Stop-Order(s) storniert."
        if opened_on_exchange is None:
            note += " Ursprünglicher Eröffnungszeitpunkt nicht ermittelbar."
        position_id = db.upsert_open_bot_position(
            symbol=symbol, side=pos.side, size=pos.size, entry_px=pos.entry_px,
            leverage=pos.leverage, stop_px=stop_px, exchange_oid=new_oid,
            exchange_stop_px=stop_px,
            origin="adopted", origin_note=note, exchange_opened_at=opened_on_exchange,
            # Nicht "unter welcher Strategie eroeffnet" (das war nicht der
            # Bot), sondern welche Konfiguration zum ZEITPUNKT DER UEBERNAHME
            # galt - fuer eine spaetere Auswertung genauso relevant wie bei
            # bot-eroeffneten Positionen.
            strategy_version=bot_signals.STRATEGY_VERSION,
            config_hash=bot_config.config_fingerprint(limits))
        # Eine Übernahme ist ein Bestandsvorgang und gehört in denselben
        # Audit-Trail wie der Phantom-Schluss, der längst eine Order-Zeile
        # schreibt - sonst bleibt sie in der Order-Historie unsichtbar.
        db.add_bot_order(
            symbol=symbol, intent="exchange_adopt", side=pos.side, size=pos.size,
            order_type=None, requested_px=price, status="filled",
            exchange_oid=new_oid, fill_px=pos.entry_px, fee_usd=None, error=None,
            position_id=position_id)

        open_notional += notional
        open_count += 1
        results.append({"symbol": symbol, "adopted": True, "stop_px": stop_px,
                       "cancelled_stops": len(cancelled),
                       "exchange_opened_at": opened_on_exchange})
    return results


def _actual_close_from_fills(exchange, symbol: str, since_ms: int,
                             remaining_size: float | None = None) -> dict | None:
    """Den ECHTEN Schluss-Fill (oder mehrere Teil-Fills) eines PHANTOM-
    geschlossenen Symbols aus der Fill-Historie rekonstruieren, statt den
    Verlust ueber den AKTUELLEN mid_price zu schaetzen. Ein Stop, der
    zwischen zwei 15-Minuten-Takten feuert, kann zu einem deutlich anderen
    Kurs gefuellt worden sein als dem, der beim naechsten Takt gerade
    herrscht - die Schaetzung war bislang die einzige Quelle und konnte PnL,
    Trefferquote und (ueber core.db.bot_signal_log) Lernlabels verfaelschen.

    `closedPnl` kommt direkt von Hyperliquid (die Börse führt selbst Buch
    über den realisierten Gewinn je Fill) und `fee` ist die tatsächlich
    gezahlte Gebühr - beides präziser als eine eigene Nachrechnung aus einem
    geschätzten Kurs.

    Best effort: ohne Fill-Historie (PaperExchange hat keine `recent_fills`),
    ohne Treffer im Zeitfenster oder bei einem API-Fehler wird None
    zurückgegeben - der Aufrufer fällt dann auf die mid_price-Schätzung
    zurück, die es davor schon gab."""
    fills_fn = getattr(exchange, "recent_fills", None)
    if fills_fn is None:
        return None
    try:
        fills = fills_fn(since_ms)
    except Exception:
        return None
    closing = [f for f in fills
              if isinstance(f, dict)
              and str(f.get("coin", "")).strip().upper() == symbol.strip().upper()
              and str(f.get("dir", "")).strip().lower().startswith(("close", "liquidat"))]
    if not closing:
        return None
    if remaining_size is not None and remaining_size > 0:
        # NUR die Fills, die zur NOCH OFFENEN Restgroesse gehoeren: neueste
        # zuerst, bis ihre Summe die Restgroesse erreicht. Aeltere Fills sind
        # die vom Bot selbst schon gebuchten Teilschluesse (deren Gewinn
        # steckt in partial_realized_pnl_usd, das close_bot_position()
        # addiert). Zweite externe Pruefung 23.09.2026: die Abgrenzung ueber
        # einen Zeitstempel (lokaler Buchungszeitpunkt) war grundsaetzlich
        # falsch - Fills haben Millisekunden, die Buchung Sekunden, und eine
        # verzoegerte Buchung liess einen echten Restschluss herausfallen.
        # Die Groesse ist von beidem unabhaengig. Gleiche Zeit: hoehere tid
        # (streng steigend) bzw. spaetere Listenposition gilt als neuer.
        def _newest_first(item):
            idx, f = item
            try:
                tid = int(f.get("tid", 0) or 0)
            except (TypeError, ValueError):
                tid = 0
            return (int(f.get("time", 0) or 0), tid, idx)

        picked, acc = [], 0.0
        for _, f in sorted(enumerate(closing), key=_newest_first, reverse=True):
            picked.append(f)
            acc += abs(float(f.get("sz", 0) or 0))
            if acc >= remaining_size * (1 - _CLOSE_FILL_EPSILON):
                break
        closing = picked
    try:
        total_size = sum(abs(float(f.get("sz", 0) or 0)) for f in closing)
        if total_size <= 0:
            return None
        notional = sum(abs(float(f.get("sz", 0) or 0)) * float(f.get("px", 0) or 0)
                       for f in closing)
        total_fee = sum(float(f.get("fee", 0) or 0) for f in closing)
        total_closed_pnl = sum(float(f.get("closedPnl", 0) or 0) for f in closing)
        # Echter Boersen-Fill-Zeitpunkt (letzter Schluss-Fill, falls mehrere
        # Teilausfuehrungen) - der Aufrufer speichert damit `closed_at`
        # bewusst NICHT mehr als den Abgleich-Erkennungszeitpunkt (der bei
        # einem 15-Minuten-Takt bis zu einer Kerze spaeter liegen kann und in
        # einem beobachteten Fall sogar zwei Schluesse in der Reihenfolge
        # vertauschte) - Haltedauer, Cooldown-Start und die Reihenfolge einer
        # Verlustserie beziehen sich damit auf den TATSAECHLICHEN Schluss.
        close_time_ms = max(int(f.get("time", 0) or 0) for f in closing)
    except (TypeError, ValueError):
        return None
    # BRUTTO (vor Gebuehren) zurueckgeben - der Aufrufer nettet ueber
    # db.bot_position_fees_usd() die komplette Position (Eroeffnung UND
    # Schliessung), nicht nur diese Schluss-Fills. Wuerde hier bereits netto
    # zurueckgegeben, fehlte beim Nachrechnen die beim Eroeffnen gezahlte
    # Gebuehr - genau die Bot-eroeffneten Positionen (deren Eroeffnungsgebuehr
    # bekannt ist) waeren dann zu optimistisch bewertet.
    return {"close_px": notional / total_size,
           "gross_pnl_usd": total_closed_pnl,
           "fee_usd": total_fee, "fill_count": len(closing),
           "closed_at": clock.iso_utc(datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc))
           if close_time_ms > 0 else None}


# Reine Sichtbarkeit, keine Aktion - initial_stop_px/R-Multiple bleiben in
# jedem Fall unangetastet (siehe _reconcile_matched_position()).
_ENTRY_PX_RECONCILE_EPSILON_PCT = 0.05


def _reconcile_matched_position(pos: dict, live) -> None:
    """Ein Symbol, das sowohl in der DB als auch auf der Börse offen ist -
    vorher endete reconcile_with_exchange() hier mit einem blossen
    `continue`: Größe, Richtung und Einstandskurs wurden nie verglichen. Eine
    externe Änderung (manueller Teilverkauf, Aufstockung, Richtungswechsel)
    blieb dadurch unbemerkt, während Heat-/Exposure-/PnL-/Trailing-
    Berechnungen im selben und jedem weiteren Zyklus mit einer längst
    falschen Größe weiterrechneten."""
    symbol = pos["symbol"]

    # Richtungswechsel ist ein Integritätsfehler, kein normaler Abgleich -
    # eine manuell (oder durch einen Börsenfehler) auf die Gegenseite
    # gedrehte Position darf der Bot nie stillschweigend weiterverwalten
    # (Stop, Heat-Rechnung, Trailing gingen alle vom falschen Vorzeichen aus).
    # Kill-Switch statt Auto-Korrektur, gleiches Muster wie bei "unbekannte
    # Börsen-Position" weiter unten in reconcile_with_exchange().
    if live.side != pos["side"]:
        reason = (f"{symbol}: DB führt {pos['side'].upper()}, Börse zeigt "
                 f"{live.side.upper()} - Richtungswechsel außerhalb des Bots.")
        if not bot_config.is_killed():
            bot_config.trip_kill_switch(reason)
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol, "db_side": pos["side"],
                                    "exchange_side": live.side}),
            run_id=None, action="reconciled_side_mismatch", executed=True, reason=reason,
        )
        return

    # Größen-Drift (manuelle Teilschließung/Aufstockung außerhalb des Bots) -
    # DB auf die Börsen-Wahrheit nachziehen, BEVOR Heat-/Exposure-/Trailing-
    # Berechnungen im selben Zyklus eine veraltete Größe lesen
    # (reconcile_with_exchange läuft laut eigenem Docstring als ERSTES im
    # Zyklus).
    if abs(live.size - pos["size"]) > _CLOSE_FILL_EPSILON * max(pos["size"], 1e-12):
        old_size = pos["size"]
        db.set_bot_position_size(symbol, live.size)
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": symbol, "old_size": old_size, "new_size": live.size}),
            run_id=None, action="reconciled_size_adjusted", executed=True,
            reason=(f"{symbol}: Größe an Börse angeglichen {old_size:g} -> {live.size:g} "
                   f"(Änderung außerhalb des Bots)."),
        )

    # Einstandskurs-Drift NUR protokollieren, initial_stop_px/stop_px bewusst
    # unangetastet (CLAUDE.md dokumentiert initial_stop_px explizit als
    # unveränderlich) - eine automatische Neuberechnung des Risikos anhand
    # eines nicht vom Bot gewählten Einstands wäre selbst eine neue,
    # ungeprüfte Entscheidung.
    if pos["entry_px"] and live.entry_px:
        drift_pct = abs(live.entry_px - pos["entry_px"]) / pos["entry_px"] * 100
        if drift_pct > _ENTRY_PX_RECONCILE_EPSILON_PCT:
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": symbol, "db_entry_px": pos["entry_px"],
                                        "exchange_entry_px": live.entry_px}),
                run_id=None, action="reconciled_entry_px_drift", executed=False,
                reason=(f"{symbol}: Einstandskurs an der Börse ({live.entry_px:,.4f} $) weicht "
                       f"von der DB ({pos['entry_px']:,.4f} $) ab - vermutlich nachgekauft/"
                       f"teilgeschlossen außerhalb des Bots. initial_stop_px/R-Multiple bleiben "
                       f"unverändert, nur zur Kenntnis protokolliert."),
            )


def reconcile_with_exchange(exchange, candles_fn=None) -> dict:
    """Gleicht die DB-Positionen mit der tatsächlichen Börse ab. Muss als
    ERSTES im Zyklus laufen, vor check_stop_triggers - sonst versucht der Bot
    immer wieder, eine Position zu schliessen, die die Börse längst nicht
    mehr kennt (Stop feuerte offline, Liquidation, manueller Eingriff im
    HL-Frontend).

    Läuft AUCH gegen die PaperExchange. Früher stieg diese Funktion für
    Paper sofort wieder aus ("per Konstruktion konsistent") - das stimmte
    nicht: PaperExchange hielt ihre Positionen nur im Prozessspeicher, die DB
    überlebte einen Neustart. Der eigentliche Fix dafür ist mittlerweile
    bot_runner._seed_paper_state(): eine neu gebaute PaperExchange wird mit
    den offenen DB-Positionen UND dem zuletzt bekannten Cash-Stand
    initialisiert, ein normaler Neustart erzeugt also gar keine Geister mehr.
    Der Phantom-Zweig unten bleibt trotzdem als Netz für den Rest-Fall
    stehen, in dem DB und Papier-Börse aus einem ANDEREN Grund auseinander-
    laufen (z.B. ein manueller Eingriff über eine Konsole zwischen zwei
    Prozessläufen) - kein erfundener Fill, sondern ehrlich als
    Bestandsabgleich gekennzeichnet.

    Phantom in der DB (Börse zeigt die Position nicht mehr): wird DB-seitig
    mit `intent='exchange_reconciled_close'` geschlossen - kein erfundener
    Fill, sondern ehrlich als Bestandsabgleich gekennzeichnet, mit
    bestmöglicher PnL-Schätzung auf Basis des aktuellen Kurses (der echte
    historische Fill-Kurs ist retrospektiv nicht mehr sicher rekonstruierbar
    ohne recent_fills() minutiös nach dem exakten Zeitpunkt zu durchsuchen).

    Unbekannte Position auf der Börse (DB kennt sie nicht): wird übernommen,
    sofern sie die Risiko-Prüfkette besteht und ein Stop gesetzt werden kann
    (siehe adopt_unknown_positions()). Jedes Symbol wird dabei EINZELN
    geprüft, mit mitwachsendem Exposure- und Positionszähler. Was nicht
    durchkommt, zieht den Kill-Switch: eine Position, die der Bot nicht
    absichern oder nicht verantworten kann, gehört vor menschliche Augen
    statt in die stille Weiterverwaltung."""
    live_positions = {p.symbol: p for p in exchange.account_state().positions}
    db_positions = db.list_open_bot_positions()

    phantom_closed = []
    phantom_closed_details = []
    for pos in db_positions:
        live = live_positions.get(pos["symbol"])
        if live is not None:
            _reconcile_matched_position(pos, live)
            continue
        opened_raw = pos.get("exchange_opened_at") or pos["opened_at"]
        try:
            since_ms = int(datetime.fromisoformat(str(opened_raw)).timestamp() * 1000)
        except (TypeError, ValueError):
            since_ms = 0
        # pos["size"] ist die REST-Groesse (die DB reduziert sie bei jedem Bot-
        # Teilschluss) - _actual_close_from_fills waehlt daraus nur die Fills
        # des noch nicht gebuchten Rests, siehe dortigen Kommentar.
        actual = _actual_close_from_fills(exchange, pos["symbol"], since_ms,
                                          remaining_size=pos["size"])
        if actual is not None:
            price = actual["close_px"]
            gross_pnl = actual["gross_pnl_usd"]
            close_fee_usd = actual["fee_usd"]
            closed_at = actual.get("closed_at")
            reason = (f"{pos['symbol']}: auf der Börse nicht mehr offen, DB nachgezogen "
                     f"(echter Fill-Kurs aus {actual['fill_count']} Börsen-Fill(s)).")
        else:
            price = exchange.mid_price(pos["symbol"]) or pos["entry_px"]
            direction = 1 if pos["side"] == "long" else -1
            gross_pnl = (price - pos["entry_px"]) * pos["size"] * direction
            close_fee_usd = None
            closed_at = None
            reason = (f"{pos['symbol']}: auf der Börse nicht mehr offen, DB nachgezogen "
                     f"(geschätzter PnL anhand des aktuellen Kurses, kein echter Fill-Kurs "
                     f"gefunden - PRÄZISION EINGESCHRÄNKT).")
        db.add_bot_order(symbol=pos["symbol"], intent="exchange_reconciled_close",
                         side=pos["side"], size=pos["size"], order_type=None,
                         requested_px=None, status="filled", fill_px=price,
                         fee_usd=close_fee_usd, error=None, position_id=pos["id"])
        # NETTO wie im regulaeren close_position(): Eroeffnungs- UND
        # Schliessungsgebuehr zusammen (bot_position_fees_usd summiert alle
        # bot_orders-Zeilen der Position, die Schluss-Zeile ist jetzt drin).
        # Vorher wurde beim echten Fill nur die Schliessungsgebuehr abgezogen
        # und bei der Schaetzung gar keine - beides war zu optimistisch.
        total_fees = db.bot_position_fees_usd(pos["id"])
        realized_pnl = gross_pnl - total_fees
        db.close_bot_position(pos["symbol"], close_px=price, realized_pnl_usd=realized_pnl,
                              closed_at=closed_at)
        # Symbol scharf/unscharf (Phase 3) - derselbe Grundsatz wie in
        # close_position(): ein Phantom-Schluss ist genauso ein Schluss.
        db.disarm_bot_symbol(pos["symbol"], pos["side"])
        # Stop stornieren wie in close_position() (core/bot.py:1011) - ein
        # Phantom-Schluss (Liquidation, manueller Schluss auf der Boerse)
        # loescht die Position, laesst aber einen zuvor gesetzten
        # reduce-only-Stop-Trigger unangetastet auf der Boerse stehen. Vorher
        # blieb der nur bis zur naechsten Eroeffnung im selben Symbol liegen
        # (dort raeumt _foreign_stop_oids ihn als Nebeneffekt mit auf) - bis
        # dahin ein verwaister Trigger, der ein voellig unbeteiligtes,
        # spaeter im selben Symbol eroeffnetes Geschaeft ausloesen koennte.
        stop_cancelled = _cancel_stop_if_supported(exchange, pos["symbol"], pos["exchange_oid"])
        if pos["exchange_oid"] and not stop_cancelled:
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": pos["symbol"]}),
                run_id=None, action="stop_cancel_failed", executed=False,
                reason=f"{pos['symbol']}: Stop-Order nach Phantom-Schluss konnte nicht "
                       f"storniert werden (OID {pos['exchange_oid']}).",
            )
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": pos["symbol"], "realized_pnl_usd": realized_pnl,
                                    "from_real_fills": actual is not None}),
            run_id=None, action="reconciled_close", executed=True,
            reason=reason,
        )
        phantom_closed.append(pos["symbol"])
        phantom_closed_details.append({"symbol": pos["symbol"], "side": pos["side"],
                                       "price": price, "realized_pnl_usd": realized_pnl,
                                       "from_real_fills": actual is not None})

    db_symbols_after = {p["symbol"] for p in db_positions if p["symbol"] not in phantom_closed}
    unknown_symbols = [sym for sym in live_positions if sym not in db_symbols_after]

    # KEIN Mengen-Vorgate mehr. Früher stand hier
    # `len(unknown_symbols) <= max_positions` - das verglich die Unbekannten
    # ALLEIN mit dem Limit, nicht Bestand + Unbekannte, sodass bei drei
    # bekannten Positionen und max_positions=3 zwei weitere anstandslos
    # durchliefen. Und es entschied nach dem Alles-oder-nichts-Prinzip, was
    # auch den Positionen den Schutz-Stop entzogen hätte, die problemlos ins
    # Limit gepasst hätten. Beides erledigt jetzt die Prüfkette je Symbol in
    # adopt_unknown_positions() (check_position_count mit mitwachsendem
    # Zähler); was dort nicht durchkommt, landet in still_unknown und zieht
    # weiter unten den Kill-Switch.
    adopted, still_unknown = [], []
    if unknown_symbols:
        for r in adopt_unknown_positions(exchange, live_positions, unknown_symbols, candles_fn=candles_fn):
            if r["adopted"]:
                adopted.append(r["symbol"])
                db.add_bot_decision(
                    cycle_type="deterministic",
                    signals_json=json.dumps({"symbol": r["symbol"], "stop_px": r["stop_px"]}),
                    run_id=None, action="position_adopted", executed=True,
                    reason=f"{r['symbol']}: automatisch übernommen, Stop bei {r['stop_px']:,.4f} $ gesetzt.",
                )
            else:
                still_unknown.append(r["symbol"])
                db.add_bot_decision(
                    cycle_type="deterministic",
                    signals_json=json.dumps({"symbol": r["symbol"]}),
                    run_id=None, action="position_adopt_failed", executed=False,
                    reason=f"{r['symbol']}: Übernahme fehlgeschlagen - {r['reason']}",
                )

    if still_unknown:
        reason = ("Börse zeigt Position(en), die die DB nicht übernehmen konnte: " +
                  ", ".join(still_unknown) + " - menschliche Prüfung nötig.")
        if not bot_config.is_killed():
            bot_config.trip_kill_switch(reason)
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"unknown_positions": still_unknown}),
            run_id=None, action="unknown_exchange_position", executed=True, reason=reason,
        )

    return {"phantom_closed": phantom_closed, "phantom_closed_details": phantom_closed_details,
            "unknown_positions": still_unknown, "adopted": adopted}


def check_stop_triggers(exchange) -> list[dict]:
    """Bewusst für Paper UND Live: bei Live liegt der Stop zwar schon als
    Trigger-Order auf der Börse (data/hyperliquid.py), aber falls der Kurs
    über den Stop hinweg gesprungen ist (Gap) oder die Trigger-Order aus
    irgendeinem Grund nicht auslöste, schliesst dieser zweite, Code-seitige
    Check die Position trotzdem - Verteidigung in der Tiefe statt blindem
    Vertrauen in eine einzelne Absicherung.

    Jedes Event trägt ein `closed`-Flag - ein fehlgeschlagener Close (Guard-
    Block, Börsenfehler) darf NIE als erfolgreiche Aktion durchgehen, sonst
    hängt sich core.db.add_bot_decision an ein `executed=True` auf, das nicht
    stimmt, und bot_runner._maybe_run_llm_review löst bei jedem 15-Minuten-
    Takt erneut einen bezahlten LLM-Call aus, ohne dass sich etwas ändert."""
    events = []
    for pos in db.list_open_bot_positions():
        price = exchange.mid_price(pos["symbol"])
        if price is None or pos["stop_px"] is None:
            continue
        hit = ((pos["side"] == "long" and price <= pos["stop_px"]) or
              (pos["side"] == "short" and price >= pos["stop_px"]))
        retry_pending = db.get_meta(f"bot_stop_retry:{pos['symbol']}") == "1"
        if not hit and not retry_pending:
            continue
        result = close_position(exchange, pos["symbol"], reason="stop_triggered")
        if result.get("status") == "filled":
            db.delete_meta(f"bot_stop_retry:{pos['symbol']}")
        else:
            db.set_meta(f"bot_stop_retry:{pos['symbol']}", "1")
        events.append({"symbol": pos["symbol"], "stop_px": pos["stop_px"], "price": price,
                       "result": result, "closed": result.get("status") == "filled"})
    return events


# --- NACHGEZOGENE STOPS ---

# Erst ab dieser Bewegung wird die Boersen-Stop-Order tatsaechlich ersetzt.
# Jede Ersetzung ist ein API-Call und ein kurzes Fenster mit zwei Stops -
# das lohnt sich nicht fuer eine Verschiebung um Zehntelprozente.
_STOP_REPLACE_MIN_MOVE_PCT = 0.5


def update_trailing_stops(exchange, candles_fn=None) -> list[dict]:
    """Stops offener Positionen in Gewinnrichtung nachziehen.

    ``stop_px`` wird als sofort wirksamer lokaler Schutz nachgezogen.
    ``exchange_stop_px`` bleibt dagegen der letzte *bestaetigte* Trigger auf
    der Boerse. Der Abstand fuer die 0,5%-Schwelle wird immer gegen diesen
    bestaetigten Wert gemessen, nicht gegen den bereits angeforderten DB-Stop:
    mehrere kleine Bewegungen addieren sich dadurch korrekt und ein
    fehlgeschlagener Austausch wird beim naechsten Takt erneut versucht.
    """
    limits = bot_config.bot_limits()
    if candles_fn is None:
        candles_fn = _default_candles_fn()

    updates = []
    for pos in db.list_open_bot_positions():
        symbol = pos["symbol"]
        price = exchange.mid_price(symbol)
        if not price:
            continue
        try:
            df = candles_fn(symbol)
        except Exception as exc:
            # Wie im Ausstiegs-Zweig (run_signal_cycle): ein Datenausfall fuer
            # EIN Symbol darf das Nachziehen der anderen nicht blockieren,
            # war bisher aber komplett unsichtbar - dieses Symbol behielt
            # seinen alten Stop, ohne dass irgendwo festgehalten wurde, dass
            # ueberhaupt ein Versuch unternommen und er mangels Daten
            # uebersprungen wurde.
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": symbol, "error": repr(exc)}),
                run_id=None, action="symbol_data_unavailable", executed=False,
                reason=f"{symbol}: Kerzen für das Stop-Nachziehen nicht abrufbar: {exc!r}",
            )
            continue
        proposed_stop = bot_signals.trailing_stop_px(pos, df, limits, price)
        desired_stop = proposed_stop if proposed_stop is not None else pos["stop_px"]
        if desired_stop is None:
            continue

        old_stop = pos["stop_px"]
        # Altpositionen, die vor der Migration existierten, haben beim ersten
        # Takt mindestens den konservativ hergeleiteten Exchange-Stop aus der
        # DB-Migration. Der zweite Fallback macht auch kaputte/manuelle
        # Altzeilen fail-safe: lieber einmal zu oft ersetzen als nie.
        exchange_stop = (pos.get("exchange_stop_px") or pos.get("initial_stop_px")
                         or old_stop)
        moved_pct = (abs(desired_stop - exchange_stop) / abs(exchange_stop) * 100
                     if exchange_stop else 100.0)
        if proposed_stop is None and moved_pct < _STOP_REPLACE_MIN_MOVE_PCT:
            continue

        new_oid, error = None, None
        replace_fn = getattr(exchange, "replace_stop_order", None)
        if replace_fn is not None and moved_pct >= _STOP_REPLACE_MIN_MOVE_PCT:
            new_oid, error = replace_fn(symbol, pos["side"], pos["size"], desired_stop,
                                        pos["exchange_oid"])
            if error or not new_oid:
                error = error or "Börse hat keine neue Stop-ID bestätigt."
                # Der lokale Stop bleibt enger und wird in jedem Takt
                # überwacht. exchange_stop_px bleibt absichtlich unverändert,
                # damit der nächste Takt DENSELBEN Exchange-Austausch erneut
                # versucht statt ihn als bereits erledigt zu betrachten.
                db.add_bot_decision(
                    cycle_type="deterministic",
                    signals_json=json.dumps({"symbol": symbol, "stop_px": desired_stop,
                                              "exchange_stop_px": exchange_stop}),
                    run_id=None, action="trailing_stop_exchange_failed", executed=False,
                    reason=error,
                )
                db.update_bot_position_stop(symbol, desired_stop)
            else:
                db.update_bot_position_stop(symbol, desired_stop, new_oid,
                                            exchange_stop_px=desired_stop)
        elif replace_fn is None:
            # PaperExchange hat keinen getrennten Boersen-Trigger. Die beiden
            # Werte duerfen dort gemeinsam nachziehen, damit Simulation und
            # DB nicht kuenstlich auseinanderlaufen.
            db.update_bot_position_stop(symbol, desired_stop,
                                        exchange_stop_px=desired_stop)
        else:
            # Noch unter der Ersetzungs-Schwelle: lokaler Schutz wird enger,
            # die Boersen-Order bleibt bewusst auf dem letzten bestaetigten
            # Wert. Die Differenz sammelt sich bis zum naechsten Austausch.
            db.update_bot_position_stop(symbol, desired_stop)
        updates.append({"symbol": symbol, "old_stop": old_stop, "new_stop": desired_stop,
                        "exchange_stop_px": exchange_stop,
                        "exchange_updated": bool(new_oid), "error": error})
    return updates


def retry_unconfirmed_exchange_stops(exchange) -> list[dict]:
    """Wiederholt einen bereits ANGEFORDERTEN, aber auf der Boerse nicht
    bestaetigten Stop-Austausch im 15-Minuten-Sicherheitstakt.

    update_trailing_stops() laeuft nur im 4h-Entscheidungsfenster; scheiterte
    dort replace_stop_order(), blieb bis zum naechsten Fenster der aeltere
    Boersen-Stop stehen, obwohl die Kommentare "naechster Takt" versprechen
    (externe Pruefung 23.09.2026). Hier wird KEIN neuer Strategie-Stop
    berechnet - nur der schon gewollte lokale stop_px (enger als der zuletzt
    bestaetigte exchange_stop_px, mindestens _STOP_REPLACE_MIN_MOVE_PCT)
    erneut an die Boerse gebracht. Kleinere, bewusst lokal gesammelte Schritte
    bleiben unberuehrt."""
    replace_fn = getattr(exchange, "replace_stop_order", None)
    if replace_fn is None:
        return []
    updates = []
    for pos in db.list_open_bot_positions():
        stop, confirmed = pos.get("stop_px"), pos.get("exchange_stop_px")
        if stop is None or not confirmed:
            continue
        direction = 1.0 if pos["side"] == "long" else -1.0
        if (float(stop) - float(confirmed)) * direction <= 0:
            continue   # lokal nicht enger als die Boerse - nichts nachzuholen
        if abs(float(stop) - float(confirmed)) / abs(float(confirmed)) * 100 < _STOP_REPLACE_MIN_MOVE_PCT:
            continue
        symbol = pos["symbol"]
        try:
            new_oid, error = replace_fn(symbol, pos["side"], pos["size"], stop,
                                        pos["exchange_oid"])
        except Exception as exc:
            new_oid, error = None, repr(exc)
        if error or not new_oid:
            error = error or "Börse hat keine neue Stop-ID bestätigt."
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": symbol, "stop_px": stop,
                                          "exchange_stop_px": confirmed, "retry": True}),
                run_id=None, action="trailing_stop_exchange_failed", executed=False,
                reason=error)
        else:
            db.update_bot_position_stop(symbol, stop, new_oid, exchange_stop_px=stop)
        updates.append({"symbol": symbol, "old_stop": stop, "new_stop": stop,
                        "exchange_stop_px": confirmed, "exchange_updated": bool(new_oid) and not error,
                        "error": error})
    return updates


# --- MANUELLES STOP-NACHZIEHEN (Button je Position, views/trading_bot.py) ---
#
# Dasselbe Meta-Flag-Muster wie "Alle Positionen schließen": die UI legt nur
# eine Anfrage ab, ausgefuehrt wird sie ausschliesslich vom Runner-Prozess
# (nur er haelt die Boersen-/Paper-Verbindung). Alle offenen Anfragen stehen
# in EINEM JSON-Meta-Wert {SYMBOL: {...}}, damit der Runner sie ohne
# Prefix-Suche in der Meta-Tabelle aufzaehlen kann - auch solche fuer
# inzwischen geschlossene Positionen, die sonst ewig liegen blieben.
_MANUAL_STOP_REQUESTS_META_KEY = "bot_manual_stop_requests"
_MANUAL_STOP_RESULTS_META_KEY = "bot_manual_stop_results"
# Mindestabstand zum aktuellen Kurs: ein Sell-Stop ueber (bzw. Buy-Stop unter)
# dem Markt loest auf Hyperliquid sofort aus - ein "Nachziehen" wuerde dann
# faktisch zum Marktschluss. Wer schliessen will, soll schliessen.
MANUAL_STOP_MIN_GAP_PCT = 0.2


def validate_manual_stop(pos: dict, new_stop: float, price: float | None) -> str | None:
    """Fehlermeldung oder None. Reine Funktion - die UI zeigt damit sofort,
    ob eine Eingabe zulaessig ist, der Runner prueft beim Ausfuehren noch
    einmal gegen den DANN aktuellen Kurs (der sich seit dem Klick bewegt
    haben kann).

    NUR NACHZIEHEN, nie lockern: ein weiter entfernter Stop erhoeht das
    Risiko ueber das hinaus, was die Guards beim Einstieg freigegeben haben
    (Positionsgroesse und Portfolio-Heat wurden auf dem Anfangsstop bemessen)."""
    if new_stop is None or new_stop <= 0:
        return "Stop-Kurs muss größer als 0 sein."
    side = pos["side"]
    current = pos.get("stop_px")
    if current is not None:
        tighter = new_stop > current if side == "long" else new_stop < current
        if not tighter:
            richtung = "über" if side == "long" else "unter"
            return (f"Der Stop darf nur nachgezogen werden - bei einem {side.capitalize()} "
                    f"muss er {richtung} dem aktuellen Stop ({current:,.6g}) liegen.")
    if not price or price <= 0:
        return "Aktueller Kurs nicht verfügbar - Stop-Abstand nicht prüfbar."
    gap_pct = ((price - new_stop) if side == "long" else (new_stop - price)) / price * 100
    if gap_pct < MANUAL_STOP_MIN_GAP_PCT:
        richtung = "unter" if side == "long" else "über"
        return (f"Der Stop muss mindestens {MANUAL_STOP_MIN_GAP_PCT:.1f} % {richtung} dem "
                f"aktuellen Kurs ({price:,.6g}) liegen, sonst löst er sofort aus.")
    return None


def _read_json_meta(key: str) -> dict:
    raw = db.get_meta(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def manual_stop_requests() -> dict:
    return _read_json_meta(_MANUAL_STOP_REQUESTS_META_KEY)


def manual_stop_results() -> dict:
    return _read_json_meta(_MANUAL_STOP_RESULTS_META_KEY)


def request_manual_stop(symbol: str, stop_px: float):
    """Von der UI aufgerufen - sendet selbst nichts, siehe Block-Kommentar."""
    symbol = symbol.strip().upper()
    requests = manual_stop_requests()
    requests[symbol] = {"stop_px": float(stop_px), "requested_at": clock.iso_utc()}
    db.set_meta(_MANUAL_STOP_REQUESTS_META_KEY, json.dumps(requests))


def cancel_manual_stop_request(symbol: str):
    requests = manual_stop_requests()
    if requests.pop(symbol.strip().upper(), None) is not None:
        db.set_meta(_MANUAL_STOP_REQUESTS_META_KEY, json.dumps(requests))


def _finish_manual_stop_request(symbol: str, requested_at: str | None, result: dict):
    # Nur DIESE Anfrage entfernen: hat die UI waehrenddessen eine neue fuer
    # dasselbe Symbol abgelegt (anderes requested_at), bleibt die stehen.
    requests = manual_stop_requests()
    if requests.get(symbol, {}).get("requested_at") == requested_at:
        requests.pop(symbol, None)
        db.set_meta(_MANUAL_STOP_REQUESTS_META_KEY, json.dumps(requests))
    results = manual_stop_results()
    results[symbol] = {**result, "requested_at": requested_at, "finished_at": clock.iso_utc()}
    db.set_meta(_MANUAL_STOP_RESULTS_META_KEY, json.dumps(results))


def set_manual_stop(exchange, symbol: str, stop_px: float) -> dict:
    """Stop einer offenen Position manuell nachziehen - auf der Boerse UND in
    der DB. Gibt {"status": "ok"|"error", "reason", ...} zurueck.

    Live: derselbe Weg wie update_trailing_stops() (replace_stop_order: erst
    neuer Stop verifiziert, dann alter storniert). Schlaegt das fehl, bleibt
    die DB BEWUSST unveraendert - anders als beim automatischen Nachziehen
    gibt es keinen naechsten Takt, der es erneut versucht, und die UI darf
    keinen Stop anzeigen, den die Boerse nicht hat. PaperExchange hat keine
    getrennte Stop-Order (check_stop_triggers prueft den DB-Stop), dort
    wandern stop_px und exchange_stop_px gemeinsam.

    Kein Guard-Check (Kill-Switch, Tageslimit ...): einen Stop enger zu
    ziehen ist risikomindernd - derselbe Grundsatz wie evaluate_exit_order.
    Die Automatik ueberschreibt einen manuell engeren Stop nicht: sowohl
    trailing_stop_px() als auch update_trailing_stops() ziehen nur in
    Gewinnrichtung nach."""
    symbol = symbol.strip().upper()
    pos = db.get_bot_position(symbol)
    if not pos or pos.get("status") != "open":
        return {"status": "error", "reason": f"{symbol}: keine offene Bot-Position."}
    price = exchange.mid_price(symbol)
    error = validate_manual_stop(pos, stop_px, price)
    if error:
        return {"status": "error", "reason": f"{symbol}: {error}"}

    old_stop = pos["stop_px"]
    replace_fn = getattr(exchange, "replace_stop_order", None)
    exchange_updated = False
    if replace_fn is not None:
        new_oid, err = replace_fn(symbol, pos["side"], pos["size"], stop_px, pos["exchange_oid"])
        if err or not new_oid:
            reason = err or "Börse hat keine neue Stop-ID bestätigt."
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": symbol, "requested_stop_px": stop_px,
                                         "stop_px": old_stop, "price": price}),
                run_id=None, action="manual_stop_failed", executed=False, reason=reason)
            return {"status": "error", "reason": f"{symbol}: {reason} Stop unverändert."}
        db.update_bot_position_stop(symbol, stop_px, new_oid, exchange_stop_px=stop_px)
        exchange_updated = True
    else:
        db.update_bot_position_stop(symbol, stop_px, exchange_stop_px=stop_px)

    db.add_bot_decision(
        cycle_type="deterministic",
        signals_json=json.dumps({"symbol": symbol, "old_stop_px": old_stop,
                                 "new_stop_px": stop_px, "price": price,
                                 "exchange_updated": exchange_updated}),
        run_id=None, action="manual_stop_update", executed=True,
        reason=f"{symbol}: Stop manuell von "
               f"{'–' if old_stop is None else f'{old_stop:,.6g}'} auf {stop_px:,.6g} "
               f"nachgezogen (Kurs {price:,.6g}).")
    return {"status": "ok", "symbol": symbol, "old_stop": old_stop, "new_stop": stop_px,
            "price": price, "exchange_updated": exchange_updated,
            "reason": f"{symbol}: Stop auf {stop_px:,.6g} gesetzt"
                      f"{' (Börsenorder ersetzt)' if exchange_updated else ' (Paper)'}."}


def process_manual_stop_requests(exchange) -> list[dict]:
    """Vom Runner je Takt (und direkt nach dem Aufwachen) aufgerufen. Jede
    Anfrage wird genau einmal versucht und danach mit ihrem Ergebnis
    abgelegt - ein Fehlschlag wird NICHT still wiederholt: der Nutzer sieht
    ihn in der UI und entscheidet selbst, ob er es erneut versucht."""
    results = []
    for symbol, req in manual_stop_requests().items():
        try:
            result = set_manual_stop(exchange, symbol, float(req.get("stop_px")))
        except Exception as exc:
            result = {"status": "error", "reason": f"{symbol}: {exc!r}"}
        _finish_manual_stop_request(symbol, req.get("requested_at"), result)
        results.append({"symbol": symbol, **result})
    return results


# --- ENTSCHEIDUNGSTAKT (Phase 3): nur bei neu geschlossener 4h-Kerze ---
#
# Vorher enthielt core.bot_signals nur den "laufende Kerze verwerfen"-Schutz
# (prepare()) - der schuetzt vor UNTERSCHIEDLICHEN Signalen innerhalb
# derselben Stunde, nicht vor MEHRFACHEN Versuchen auf demselben Kerzenstand.
# Bei einem 15-Minuten-Runner und stuendlichen Kerzen (V1) konnte derselbe
# Scan-Stand bis zu VIER Mal zum Einstieg fuehren, sobald sich zwischen zwei
# Takten ein Slot oeffnete oder der Live-Kurs leicht schwankte (CLAUDE.md:
# "29 von 31 Einstiegen aus abgesenkter Schwelle", "17 der 31 Einstiege
# stammten aus nur sieben Stundenkerzen"). Der 4h-Anker hier macht das
# strukturell unmoeglich, statt sich auf eine Wiederholungs-Heuristik zu
# verlassen.

def _due_for_decision(decision_candles_fn=None) -> str | None:
    """Wurde seit der letzten Entscheidung ein neues 4h-Fenster (BTC als
    Anker-Symbol, dieselbe Referenz wie das Tagesregime in core.bot_signals)
    abgeschlossen? Gibt dessen Zeitstempel zurueck, wenn ja - sonst None.
    Nichts anderes als core.bot_signals.prepare()'s "letzte Zeile ist die
    laufende Kerze"-Trick, nur auf 4h statt 1h und gegen einen persistenten
    Meta-Marker statt gegen den DataFrame-Index selbst geprueft.

    Default ist derselbe volle Kerzenlieferant wie fuer Scan/Ausstiege/
    Trailing (_default_candles_fn) - seit Phase 4 (V2-Signal-Engine) braucht
    ohnehin JEDER Aufrufer 4h-Daten mit vollem Lookback (core.bot_signals.
    MIN_CANDLES), ein separater, kleinerer Abruf nur fuer die Grenzpruefung
    war seit dem nur noch redundant (data.hyperliquid.candles_df ist TTL-
    gecacht, ein zweiter Aufruf mit denselben Parametern kostet nichts)."""
    if decision_candles_fn is None:
        decision_candles_fn = _default_candles_fn()

    try:
        df = decision_candles_fn("BTC")
    except Exception as exc:
        # NICHT dasselbe wie "noch kein neues Fenster" (der Normalfall in
        # ~14 von 15 Takten): ein echter Abruffehler legt den gesamten
        # Entscheidungszweig (Trailing-Stops UND Signal-Zyklus) still, ohne
        # dass das je sichtbar wurde - eine anhaltende Stoerung sah identisch
        # aus wie "nichts zu tun". Bewusst nur hier geloggt, nicht auch bei
        # `len(df) < 2`, weil das bei einem frisch gestarteten Symbol/Konto
        # ein legitimer, vorübergehender Zustand ist.
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"error": repr(exc)}),
            run_id=None, action="decision_window_unavailable", executed=False,
            reason=f"BTC-4h-Kerzen für die Entscheidungs-Fensterprüfung nicht abrufbar: {exc!r}",
        )
        return None
    if df is None or len(df) < 2:
        return None
    closed_ts = str(df.index[-2])
    if db.get_meta("bot_last_decision_candle") == closed_ts:
        return None
    return closed_ts


def _current_btc_regime(daily_fn=None) -> str:
    """Tagesregime fuer die AUSSTIEGS-Seite von run_signal_cycle() (Gate 1 der
    Hysterese, core.bot_signals.exit_signal). bot_signals.scan() berechnet
    das Regime fuer die EINSTIEGS-Seite bereits selbst (einmal je Scan) - ein
    zweiter Aufruf hier dupliziert die Berechnung, ist aber dank
    data.hyperliquid.candles_df's TTL-Cache (300s) faktisch kostenlos
    innerhalb desselben 4h-Entscheidungstakts. Eigene, kleine Funktion statt
    das Regime zwischen den beiden Aufrufern durchzureichen - haelt beide
    Seiten unabhaengig lauffaehig (z.B. in Tests, die nur Ausstiege pruefen)."""
    if daily_fn is None:
        from data.hyperliquid import candles_df as _candles_df

        def daily_fn(symbol):
            return _candles_df(symbol, interval="1d",
                               lookback_hours=bot_signals.REGIME_LOOKBACK_HOURS)
    try:
        btc_daily = daily_fn("BTC")
    except Exception:
        btc_daily = None
    return bot_signals.regime(btc_daily)


_REENTRY_MIN_WAIT_HOURS = 4.0


def _reentry_check(symbol: str, signal, now: datetime) -> str | None:
    """None = Symbol darf neu eroeffnet werden. Sonst der Sperrgrund.

    Der reine Zeit-Cooldown (core.bot_guards.check_symbol_cooldown,
    limits["symbol_cooldown_hours"]) verhindert nur, dass DIESELBE frisch
    geschlossene Kerze sofort erneut ausloest - nicht, dass ein Symbol
    Stunden spaeter auf demselben, laengst bekannten Setup wieder einsteigt
    (CLAUDE.md-Feedback: 17 Wiedereinstiege in dasselbe Symbol innerhalb von
    24h, davon 14 erneut in dieselbe Richtung, zusammen -6,59 $). Ein Symbol
    wird deshalb bei jedem Schluss SCHARF GESTELLT (db.disarm_bot_symbol,
    core.db.bot_symbol_state) und erst wieder scharf, wenn BEIDE Bedingungen
    erfuellt sind:
    1. Mindestens `_REENTRY_MIN_WAIT_HOURS` (eine volle 4h-Kerze) seit dem
       Schluss vergangen - reiner Zeitablauf allein reicht NICHT.
    2. Das Setup ist NACHWEISLICH weg: die aktuelle Bewertung fuer dieses
       Symbol ist entweder nicht mehr in dieselbe Richtung handelbar
       (signal.side != last_side) oder gar nicht mehr handelbar
       (not signal.tradeable) - nicht schon, weil Zeit vergangen ist.
    Ein kaputter/fehlender disarmed_at-Zeitstempel sperrt NIE dauerhaft
    (Read-Time-Robustheit wie ueberall in core.bot_config) - das Symbol wird
    stattdessen sofort scharf gestellt.

    Ein blosser DATENFEHLER ist KEIN Nachweis, dass das Setup weg ist:
    `evaluate()` liefert bei zu wenigen/nicht abrufbaren Kursdaten ein
    Signal mit side=None UND leerem `readings` (core.bot_signals.evaluate,
    Zweig `if r is None`) - ohne die readings-Pruefung unten waere
    `signal.side == last_side` sofort False und stellte das Symbol scharf,
    obwohl ueber das tatsaechliche Setup nichts bekannt ist (reproduziert:
    ein einzelner Kursdatenausfall gibt ein Symbol frei)."""
    state = db.get_bot_symbol_state(symbol)
    if state is None or state.get("armed"):
        return None
    disarmed_at = clock.parse_utc(state.get("disarmed_at"))
    if disarmed_at is None:
        db.arm_bot_symbol(symbol)
        return None
    reason = reentry_block_reason(disarmed_at, state.get("last_side"), signal, now)
    if reason is None:
        db.arm_bot_symbol(symbol)
    return reason


def reentry_block_reason(disarmed_at: datetime, last_side: str | None, signal,
                         now: datetime) -> str | None:
    """REINE Entscheidung der Wiedereinstiegssperre (kein DB-Zugriff) - von
    _reentry_check() (live) UND analysis.bot_backtest gemeinsam genutzt, damit
    der Rücktest dieselbe Regel rechnet statt einer Nachbildung (externe
    Pruefung 23.09.2026: der Ruecktest simulierte die Sperre gar nicht).
    None = Symbol darf neu eroeffnet werden (der Aufrufer stellt es scharf),
    sonst der Sperrgrund."""
    if now - disarmed_at < timedelta(hours=_REENTRY_MIN_WAIT_HOURS):
        remaining = _REENTRY_MIN_WAIT_HOURS - (now - disarmed_at).total_seconds() / 3600
        return (f"Signal-Reset: mindestens {_REENTRY_MIN_WAIT_HOURS:.0f}h seit dem letzten "
                f"Schluss abwarten (noch {max(0.0, remaining):.1f}h).")
    if not signal.readings:
        return ("Signal-Reset: aktuelle Bewertung ohne auswertbare Kursdaten - "
                "Setup gilt nicht als aufgelöst.")
    if signal.side == last_side and signal.tradeable:
        return f"Signal-Reset: Setup für {last_side} weiterhin aktiv, noch nicht neu bewertet."
    return None


# --- SIGNAL-ZYKLUS: die Instanz, die tatsaechlich handelt ---

def run_signal_cycle(exchange, candles_fn=None, equity_trusted: bool = True) -> dict:
    """Deterministische Ein- und Ausstiege aus core.bot_signals.

    AUSSTIEGE VOR EINSTIEGEN - aus zwei Gruenden: ein freigewordener Platz
    steht sofort wieder zur Verfuegung, und eine Position, die raus soll, wird
    nie vom Tageslimit blockiert, das ein Einstieg im selben Takt verbraucht
    haette.

    Es wird HOECHSTENS EIN Einstieg pro Takt eroeffnet. Der Bot soll ein
    Portfolio aufbauen, nicht in einem einzigen Moment alle Plaetze mit
    Signalen derselben Marktlage fuellen - vier gleichzeitige Longs auf einen
    gemeinsamen Krypto-Aufschwung sind eine einzige Wette, keine vier.

    Jeder Lauf protokolliert seinen kompletten Scan (auch wenn nichts
    passiert). Genau das fehlte bisher: "der Bot macht nichts" war ohne
    Protokoll nicht von "der Bot ist abgestuerzt" zu unterscheiden.

    `equity_trusted=False` (aus core.bot.equity_reading() via
    run_deterministic_cycle) sperrt NUR neue Einstiege - Ausstiege oben
    bleiben unberuehrt, ein Notausstieg muss immer moeglich sein.
    """
    limits = bot_config.bot_limits()
    if candles_fn is None:
        candles_fn = _default_candles_fn()
    btc_regime = _current_btc_regime()

    result = {"exits": [], "entry": None, "signals": [], "skipped": None}

    # 1. Ausstiege
    for pos in db.list_open_bot_positions():
        try:
            df = candles_fn(pos["symbol"])
        except Exception as exc:
            # Ein Datenausfall fuer EIN Symbol darf die anderen nicht
            # blockieren (daher weiterhin `continue`, kein `break`/`raise`) -
            # aber vorher unsichtbar: die Ausstiegspruefung dieses Symbols
            # fand diesen Takt schlicht nicht statt, ohne jede Spur im Audit-
            # Trail. Eine anhaltende Stoerung genau dieses Symbols sah damit
            # identisch aus wie "kein Ausstiegsgrund".
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"symbol": pos["symbol"], "error": repr(exc)}),
                run_id=None, action="symbol_data_unavailable", executed=False,
                reason=f"{pos['symbol']}: Kerzen für die Ausstiegsprüfung nicht abrufbar: {exc!r}",
            )
            continue
        reason = bot_signals.exit_signal(pos, df, limits, btc_regime=btc_regime)
        if not reason:
            continue
        outcome = close_position(exchange, pos["symbol"], reason=reason)
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"symbol": pos["symbol"], "side": pos["side"]}),
            run_id=None, action="signal_exit",
            executed=outcome.get("status") == "filled",
            reason=f"{pos['symbol']}: {reason} | Ergebnis: {outcome.get('status')}"
                   f"{' (' + str(outcome.get('reason')) + ')' if outcome.get('reason') else ''}",
        )
        result["exits"].append({"symbol": pos["symbol"], "reason": reason, "result": outcome})

    # 2. Einstiege
    open_positions_now = db.list_open_bot_positions()
    open_symbols = {p["symbol"] for p in open_positions_now}
    signals = bot_signals.scan(exchange, candidate_symbols(), limits, candles_fn=candles_fn)
    result["signals"] = [s.as_row() for s in signals]

    now = clock.now_utc()
    # Wiedereinstiegssperre fuer ALLE gescannten, noch nicht offenen Symbole
    # UNABHAENGIG von der Kandidatenauswahl unten aktualisieren - _reentry_
    # check() ist der einzige Ort, an dem ein disarmed Symbol per `not
    # signal.tradeable` wieder scharf gestellt wird (dessen Docstring,
    # Bedingung 2). Vorher lief dieser Aufruf NUR innerhalb der
    # Auswahlschleife weiter unten - die komplett uebersprungen wird, sobald
    # z.B. alle Positionsplaetze belegt sind (skip_reason greift dann VOR
    # der Schleife), und die beim ersten gefundenen Kandidaten abbricht. Ein
    # nachrangiges Symbol, dessen Setup sich zwischenzeitlich aufloest,
    # waehrend alle Plaetze belegt sind oder ein hoeher gerankter Kandidat
    # zuerst gewinnt, verpasste dadurch seine eigene Freischaltung fuer
    # diesen Takt (reproduziert von einer externen Pruefung, 13.09.2026).
    # Das Ergebnis wird hier einmal berechnet und unten nur noch gelesen -
    # kein zweiter _reentry_check()-Aufruf pro Symbol je Takt.
    disarmed: dict[str, str] = {}
    for signal in signals:
        if signal.symbol in open_symbols:
            continue
        reentry_block = _reentry_check(signal.symbol, signal, now)
        if reentry_block:
            disarmed[signal.symbol] = reentry_block

    entry_signal, skip_reason, notional = None, None, None
    if not equity_trusted:
        skip_reason = "Equity nicht vertrauenswürdig - keine neuen Einstiege."
    elif bot_config.is_killed():
        skip_reason = "Kill-Switch aktiv - keine neuen Einstiege."
    elif len(open_positions_now) >= limits["max_positions"]:
        skip_reason = f"Alle {limits['max_positions']} Positionsplätze belegt."
    elif (over_lev := _positions_over_leverage_limit(limits)):
        # Anders als eine Exposure-Verletzung blockiert eine reine Hebel-
        # Verletzung KEINE neuen Einstiege von selbst (check_leverage prueft
        # nur den Hebel DER NEUEN Order) - ohne diese explizite Sperre wuerde
        # der Bot munter weiterhandeln, waehrend das Depot als Ganzes ueber
        # dem aktuellen Limit liegt. Bestehende Positionen bleiben unberuehrt
        # (kein Zwangsschluss, siehe existing_position_limit_warnings).
        skip_reason = ("Bestehende Position(en) über dem aktuellen Hebel-Limit "
                       f"({', '.join(over_lev)}) - keine neuen Einstiege, bis das "
                       "Depot wieder regelkonform ist.")
    else:
        for signal in signals:
            if signal.symbol in open_symbols:
                continue
            if signal.symbol in disarmed:
                skip_reason = f"{signal.symbol}: {disarmed[signal.symbol]}"
                continue
            # `tradeable` ist seit der V2-Signal-Engine der ALLEINIGE
            # Gate-Entscheid (core.bot_signals.evaluate/Signal.tradeable) -
            # keine zusaetzliche Punktzahl-Schwelle mehr (siehe dessen
            # Modul-Docstring: eine Rangfolge muss nicht kalibriert sein,
            # ein Gate schon). Ein soeben (wieder) scharf gestelltes, aber
            # weiterhin nicht handelbares Signal darf trotzdem nicht zum
            # Einstieg werden.
            if not signal.tradeable:
                continue
            entry_signal = signal
            break
        if entry_signal is None and skip_reason is None:
            skip_reason = "Kein handelbarer Kandidat (Pflichtbedingungen nicht erfüllt oder Regime neutral)."

    # Symbol/Ergebnis eines an der Mindestordergroesse gescheiterten
    # Kandidaten - siehe die Erklaerung im `if notional is None`-Zweig
    # unten. Vorab initialisiert, damit die Variablen auch im Erfolgsfall
    # (kein Sizing-Fehlschlag) definiert sind.
    sizing_failed_symbol, sizing_failed_outcome = None, None
    if entry_signal is not None:
        # ERST NACH der Kandidatenwahl bemessen (nicht vorab pauschal): die
        # Groesse haengt jetzt vom Stop-Abstand DIESES Kandidaten ab
        # (core.bot_signals.position_size_usd's Risikobudget-Rechnung) - ein
        # fester Equity-Prozentsatz vorher riskierte je nach ATR ein
        # Vielfaches pro Trade bei identischer Positionsgroesse.
        equity = exchange.account_state().equity_usd
        cap = (bot_config.FIRST_LIVE_ORDER_CAP_USD
               if _exchange_kind(exchange) == "live" and bot_config.first_live_order_pending()
               else None)
        notional, size_error = bot_signals.position_size_usd(
            equity, limits, _min_notional_usd(), cap,
            stop_distance_pct=entry_signal.stop_distance_pct)
        if notional is None:
            # Der GEWAEHLTE Kandidat hat alle sieben Gates bestanden (sonst
            # waere er nie entry_signal geworden) und scheitert erst hier an
            # der Mindestordergroesse der Boerse - vorher verschwand dieser
            # Fall spurlos im generischen "skipped"-Eimer von
            # _log_signal_scan (dort nur `attempted_symbol`/`entry_outcome`
            # unterscheidbar, die beide erst UNTERHALB dieses Blocks gesetzt
            # wurden). Ein Kandidat, der jede inhaltliche Pruefung besteht
            # und nur an der Kontogroesse scheitert, ist kein "kein
            # Kandidat" - dieselbe Luecke wie ein von evaluate_entry_order()
            # blockierter Kandidat.
            sizing_failed_symbol = entry_signal.symbol
            sizing_failed_outcome = {"status": "blocked", "reason": size_error}
            skip_reason = size_error
            entry_signal = None

    result["skipped"] = skip_reason
    if entry_signal is not None:
        decision_id = db.add_bot_decision(
            cycle_type="deterministic",
            # KOMPLETTER Scan, nicht nur der Gewinner. Vorher stand hier
            # allein entry_signal.as_row(): der Audit-Trail fehlte damit
            # ausgerechnet an den Takten, an denen tatsaechlich entschieden
            # wurde - "warum dieser Kandidat und nicht jener" war
            # nachtraeglich nicht mehr beantwortbar.
            signals_json=json.dumps({"signals": result["signals"],
                                     "entry": entry_signal.as_row()}),
            run_id=None, action=f"signal_entry_{entry_signal.side}", executed=False,
            reason=f"{entry_signal.symbol}: alle Pflichtbedingungen erfüllt (Score {entry_signal.score:.0f}) · "
                   + " · ".join(entry_signal.reasons[:3]),
        )
        # stop_distance_pct statt eines vorab aus entry_signal.price (dem
        # SCAN-Zeitpunkt-Kurs) berechneten stop_px - open_position()
        # berechnet den tatsaechlichen Stop erst aus dem bestaetigten Fill.
        outcome = open_position(exchange, entry_signal.symbol, entry_signal.side, notional,
                                _entry_leverage(entry_signal, limits),
                                entry_signal.stop_distance_pct, decision_id=decision_id)
        db.update_bot_decision(
            decision_id, executed=outcome.get("status") == "filled",
            reason=f"{entry_signal.symbol}: Score {entry_signal.score:.0f} | "
                   f"Ergebnis: {outcome.get('status')}"
                   f"{' (' + str(outcome.get('reason')) + ')' if outcome.get('reason') else ''}",
        )
        # Nur bei einem tatsaechlichen Fill gilt das Signal als "opened" -
        # eine vom Guard blockierte Order ist eine verpasste Gelegenheit,
        # keine Bot-Entscheidung, und darf spaeter nicht so gezaehlt werden.
        opened = entry_signal.symbol if outcome.get("status") == "filled" else None
        _log_signal_scan(signals, limits, opened, decision_id,
                         attempted_symbol=entry_signal.symbol, entry_outcome=outcome,
                         disarmed=disarmed)
        result["entry"] = {"symbol": entry_signal.symbol, "side": entry_signal.side,
                           "score": entry_signal.score, "notional_usd": notional,
                           "result": outcome}
    else:
        # Auch ein Takt ohne Handlung wird protokolliert - mit dem kompletten
        # Scan im signals_json. Das ist die Datenquelle fuer "Warum handelt
        # der Bot (nicht)?" in views/trading_bot.py.
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"signals": result["signals"], "entry": None}),
            run_id=None, action="signal_scan", executed=False,
            reason=skip_reason or "Kein Einstieg in diesem Takt.",
        )
        _log_signal_scan(signals, limits, None, None,
                         attempted_symbol=sizing_failed_symbol,
                         entry_outcome=sizing_failed_outcome, disarmed=disarmed)
    return result


def _log_signal_scan(signals, limits: dict,
                     entry_symbol: str | None, decision_id: int | None,
                     attempted_symbol: str | None = None,
                     entry_outcome: dict | None = None, disarmed: dict | None = None) -> int:
    """Eine Zeile je KANDIDAT je Takt in bot_signal_log.

    Warum zusaetzlich zu bot_decisions: dort liegt der Scan als ein einziger
    JSON-Blob je Takt - gut zum Anzeigen, unbrauchbar zum Auswerten. Fuer die
    Frage "waeren die uebersprungenen Signale profitabel gewesen?" braucht es
    eine Zeile je Kandidat, die spaeter um das tatsaechliche Ergebnis
    ergaenzt werden kann (bot_runner.backfill_signal_outcomes).

    `attempted_symbol`/`entry_outcome`: der GEWAEHLTE Kandidat kann trotz
    ausreichendem Score an core.bot_guards.evaluate_entry_order() scheitern
    (Leverage/Exposure/Kill-Switch/...) oder nach einem Fill an einem
    spaeteren Schritt (Notausstieg wegen fehlgeschlagener Stop-Order). Ohne
    diese beiden Parameter fiel so eine Zeile in den generischen "skipped"-
    Eimer - nicht von "kein Kandidat ueber der Schwelle" zu unterscheiden,
    obwohl der genaue Ablehnungsgrund in outcome['reason'] bereits vorlag.

    REINE DATENSAMMLUNG. Nichts davon wirkt auf die Live-Strategie zurueck -
    genau das ist der Punkt: ein Bot, der sich nach jedem Lauf selbst
    umbaut, lernt Rauschen.
    """
    now = clock.iso_utc()
    risk = bot_config.risk_level()
    config_hash = bot_config.config_fingerprint(limits)
    disarmed = disarmed or {}
    rows = []
    for s in signals:
        position_id = None
        if s.symbol == entry_symbol:
            action, block_reason = "opened", None
            position_id = (entry_outcome or {}).get("position_id")
        elif s.symbol == attempted_symbol and entry_outcome is not None:
            # Guard-Ablehnung ("blocked") vs. ein spaeterer Ausfuehrungsfehler
            # nach bereits erfolgtem Fill ("error", z.B. Notausstieg) - beide
            # sind eine ECHTE Blockade, keine generische Nicht-Wahl.
            action = "blocked" if entry_outcome.get("status") == "blocked" else "error"
            block_reason = entry_outcome.get("reason")
            position_id = entry_outcome.get("position_id")
        elif s.symbol in disarmed:
            # Signal-Reset (Phase 3, core.bot._reentry_check): eigene Kategorie,
            # damit "haette gehandelt, aber gerade erst geschlossen" von einem
            # generischen Guard-'blocked' unterscheidbar bleibt.
            action, block_reason = "disarmed", disarmed[s.symbol]
        elif s.blocked_by:
            action, block_reason = "blocked", s.blocked_by
        else:
            action, block_reason = "skipped", None
        r = s.readings or {}
        rows.append({
            "ts": now, "candle_ts": r.get("candle_ts"), "symbol": s.symbol,
            "side": s.side, "score": s.score, "long_score": s.long_score,
            "short_score": s.short_score,
            "components_json": json.dumps(s.components),
            "readings_json": json.dumps(r, default=str),
            "price": s.price, "atr_pct": s.atr_pct, "stop_pct": s.stop_distance_pct,
            "funding_hourly": s.funding_hourly,
            # "threshold" (V1: Einstiegsschwelle) bleibt NULL - die V2-Signal-
            # Engine hat keine Punktzahl-Schwelle mehr (core.bot_signals-
            # Modul-Docstring). Spalte bleibt aus Kompatibilitaetsgruenden
            # bestehen (bot_signal_log ist reine Datensammlung), wird aber
            # nicht mehr befuellt.
            "risk_level": risk, "strategy_version": bot_signals.STRATEGY_VERSION,
            "config_hash": config_hash,
            "action": action, "block_reason": block_reason,
            "decision_id": decision_id if s.symbol == entry_symbol else None,
            "position_id": position_id,
        })
    return db.add_bot_signal_logs(rows)


def _entry_leverage(signal, limits: dict) -> float:
    """Hebel nur dort einsetzen, wo er nichts kostet: Der Guard laesst >1x nur
    innerhalb des Funding-Budgets zu (core.bot_guards.check_leverage). Statt
    blind das Maximum zu beantragen und blockiert zu werden, wird hier vorab
    geprueft, ob die Richtung ueberhaupt Funding zahlt."""
    max_lev = float(limits["max_leverage"])
    if max_lev <= 1.0 or signal.funding_hourly is None:
        return 1.0
    cost_rate = signal.funding_hourly if signal.side == "long" else -signal.funding_hourly
    budget = float(limits.get("funding_budget_hourly", 0.0))
    return max_lev if cost_rate <= budget else 1.0


# Live einmal beobachteter Fehlwert: 5.92 statt 44.50 $ bei genau einer
# offenen Position (Unified-Konto, dessen Spot-Saldo kurzzeitig nicht lesbar
# war) - positiv, aber weit unter der Wahrheit (Faktor ~0.13). Ein reiner
# "> 0"-Test haette das nicht erkannt. Die Schwelle liegt bewusst SEHR tief
# (0.2, nicht z.B. 0.5): ein hart gehebeltes Symbol kann in einem einzigen
# 15-Minuten-Takt real 40-60% seines Werts verlieren, bevor sein eigener Stop
# greift - das ist ein Fall fuer check_equity_floor (siehe
# _EQUITY_FLOOR_CONFIRMATIONS unten), nicht fuer diese Plausibilitaetsbremse.
# Die Bremse soll AUSSCHLIESSLICH den oben beschriebenen Daten-/API-Fehlwert
# abfangen, keine echten (wenn auch schweren) Handelsverluste.
# _EQUITY_FLOOR_CONFIRMATIONS verhindert zusaetzlich, dass eine EINZELNE
# (auch vertrauenswuerdige) Boden-Messung sofort alles zwangsschliesst -
# genau das hat den realen Vorfall ausgeloest.
_EQUITY_IMPLAUSIBLE_DROP_FACTOR = 0.2
_EQUITY_FLOOR_CONFIRMATIONS = 2


def equity_reading(exchange) -> dict:
    """Einzige Stelle, an der eine Equity-Messung bewertet wird - liefert
    {'usd', 'trusted', 'reason', 'state'}. Zwei unabhaengige Pruefungen:
    1. Die Boerse selbst markiert die Angabe als unsicher
       (state.equity_trusted, siehe data.hyperliquid.LiveExchange.account_state()).
    2. Ein Sturz auf unter _EQUITY_IMPLAUSIBLE_DROP_FACTOR des letzten
       aufgezeichneten Werts INNERHALB EINES TAKTS gilt als unplausibel - NICHT
       als Reaktion auf echte Handelsverluste (dafuer ist check_equity_floor
       da), sondern als letzte Bremse gegen einen offensichtlichen Datenfehler
       wie den oben beschriebenen."""
    state = exchange.account_state()
    trusted = getattr(state, "equity_trusted", True)
    reason = "" if trusted else "Börse meldet die Equity als nicht verifiziert (Kontomodus unklar)."
    if trusted:
        latest = db.latest_bot_equity()
        last_usd = float((latest or {}).get("equity_usd") or 0.0)
        if last_usd > 0 and state.equity_usd < last_usd * _EQUITY_IMPLAUSIBLE_DROP_FACTOR:
            trusted = False
            reason = (f"Equity {state.equity_usd:,.2f} $ liegt über "
                     f"{int((1 - _EQUITY_IMPLAUSIBLE_DROP_FACTOR) * 100)}% unter der letzten "
                     f"Messung ({last_usd:,.2f} $) - unplausibler Sprung.")
    return {"usd": state.equity_usd, "trusted": trusted, "reason": reason, "state": state}


def record_equity_snapshot(exchange) -> dict:
    """Schreibt NUR bei vertrauenswürdiger Messung (equity_reading()) einen
    Punkt in die Historie - ein Fehlwert würde sonst Verlauf, Vergleichschart
    UND die nächste Plausibilitätsprüfung (Sturz gegen den letzten Wert)
    vergiften. Rückgabe trägt immer 'trusted'/'reason', auch wenn nichts
    geschrieben wurde, damit der Aufrufer (run_deterministic_cycle) reagieren
    kann, ohne selbst erneut zu messen."""
    from data import fx

    reading = equity_reading(exchange)
    state = reading["state"]
    if not reading["trusted"]:
        return {"equity_usd": reading["usd"], "equity_eur": None,
                "fees_cum_usd": None, "funding_cum_usd": None, "net_flows_usd": None,
                "trusted": False, "reason": reading["reason"]}

    rate = fx.get_fx_to_eur("USD") or 1.0
    equity_eur = state.equity_usd * rate
    fees_cum = float(db.get_meta("bot_fees_cum_usd") or 0.0)
    funding_cum = float(db.get_meta("bot_funding_cum_usd") or 0.0)
    net_flows_cum = float(db.get_meta("bot_net_flows_usd") or 0.0)
    db.add_bot_equity_point(equity_usd=state.equity_usd, equity_eur=equity_eur,
                            cash_usd=state.withdrawable_usd, fees_cum_usd=fees_cum,
                            funding_cum_usd=funding_cum, net_flows_usd=net_flows_cum)
    return {"equity_usd": state.equity_usd, "equity_eur": equity_eur,
           "fees_cum_usd": fees_cum, "funding_cum_usd": funding_cum,
           "net_flows_usd": net_flows_cum, "trusted": True, "reason": ""}


def run_deterministic_cycle(exchange, interval_hours: float = 0.25,
                            candles_fn=None, decision_candles_fn=None) -> dict:
    """Der eigentliche Bot-Takt, vollständig ohne LLM und damit kostenlos.

    ZWEI EBENEN (Phase 3), nicht mehr eine: der SICHERHEITS-Teil laeuft
    JEDEN Takt (alle 15 Minuten, bot_runner.py) - Bestandsabgleich,
    ausgeloeste Stops, Funding (nur Paper), Equity messen, Equity-Boden/
    Abbruchkriterium. Der ENTSCHEIDUNGS-Teil (Stops nachziehen, Ein-/
    Ausstiege aus der Signal-Engine) laeuft dagegen nur, wenn seit der
    letzten Entscheidung ein neues 4h-Fenster abgeschlossen wurde
    (_due_for_decision) - vorher liefen beide im selben 15-Minuten-Takt,
    wodurch derselbe stuendliche Kerzenstand bis zu vier Mal zum Einstieg
    fuehren konnte. Wird der Kill-Switch im Sicherheits-Teil ausgeloest,
    sieht run_signal_cycle() ihn (sofern der Entscheidungs-Teil ueberhaupt
    faellig ist) bereits und eroeffnet nichts mehr.

    Die KI-Aufsicht (agents/trader.py) haengt daneben, nicht hier hinein.
    """
    reconciliation = reconcile_with_exchange(exchange, candles_fn=candles_fn)

    all_stop_attempts = check_stop_triggers(exchange)
    stop_events = [e for e in all_stop_attempts if e["closed"]]
    failed_stop_closes = [e for e in all_stop_attempts if not e["closed"]]

    funding_paid = _apply_funding_if_paper(exchange, interval_hours)
    if funding_paid:
        current = float(db.get_meta("bot_funding_cum_usd") or 0.0)
        db.set_meta("bot_funding_cum_usd", str(current + funding_paid))

    snapshot = record_equity_snapshot(exchange)
    equity = snapshot["equity_usd"]
    limits = bot_config.bot_limits()
    abort_reasons_now = []

    if not snapshot.get("trusted", True):
        # Equity nicht vertrauenswuerdig (core.bot.equity_reading) - kein
        # neuer Verlaufspunkt (schon in record_equity_snapshot uebersprungen),
        # kein Boden-/90-Tage-Check, keine neuen Einstiege in diesem Takt
        # (siehe equity_trusted unten). Genau die Kette "falscher Kontowert
        # -> sofortige Zwangsschliessung" hat live einmal beide offenen
        # Positionen unnoetig geschlossen - bestehende Positionen und ihre
        # Stops laufen unberuehrt weiter, es wird nur nichts NEUES riskiert.
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"equity_usd": equity}),
            run_id=None, action="equity_untrusted", executed=False,
            reason=snapshot["reason"],
        )
        # Eigener Meta-Schluessel statt "juengste Entscheidung pruefen" - der
        # Signal-Zyklus schreibt gleich darauf noch einen eigenen
        # signal_scan-Eintrag, der sonst die Untrusted-Warnung in der UI
        # (views/trading_bot.py) wieder verdeckt haette.
        db.set_meta("bot_equity_untrusted_at", clock.iso_utc())
    else:
        db.delete_meta("bot_equity_untrusted_at")
        start = adjusted_start_usd() or equity

        # Equity-Boden: "alles schliessen, harter Stopp, manueller Neustart
        # noetig" (Plan) - aber erst nach _EQUITY_FLOOR_CONFIRMATIONS
        # aufeinanderfolgenden BESTAETIGTEN (trusted) Unterschreitungen, nicht
        # schon bei der ersten Messung. Der Kill-Switch blockiert nur NEUE
        # Einstiege (core.bot_guards.evaluate_exit_order laesst Schliessungen
        # bewusst durch) - das Zwangsschliessen hier funktioniert deshalb auch
        # dann noch, wenn der Switch schon (z.B. manuell) aktiv war.
        floor_check = bot_guards.check_equity_floor(equity, start, limits["equity_floor_pct"])
        if not floor_check.allowed:
            hits = int(db.get_meta("bot_equity_floor_hits") or 0) + 1
            db.set_meta("bot_equity_floor_hits", str(hits))
            if hits >= _EQUITY_FLOOR_CONFIRMATIONS:
                newly_tripped = not bot_config.is_killed()
                if newly_tripped:
                    bot_config.trip_kill_switch(floor_check.reason)
                closed = [{"symbol": p["symbol"], "result": close_position(exchange, p["symbol"],
                                                                            reason="equity_floor")}
                         for p in db.list_open_bot_positions()]
                if newly_tripped or closed:
                    db.add_bot_decision(
                        cycle_type="deterministic",
                        signals_json=json.dumps({"equity_usd": equity, "equity_start_usd": start,
                                                "closed": [c["symbol"] for c in closed],
                                                "confirmations": hits}),
                        run_id=None, action="equity_floor", executed=True, reason=floor_check.reason,
                    )
        else:
            db.delete_meta("bot_equity_floor_hits")

        # Das vorab festgelegte 90-Tage-Kriterium ist keine bloße UI-Warnung:
        # Sobald es fällig und verletzt ist, sperrt derselbe sperrklinkenartige
        # Kill-Switch neue Einstiege. Offene Positionen bleiben nur über ihren
        # Stop bzw. einen bewussten Exit steuerbar; ein Hintergrundzyklus darf
        # nicht eigenmächtig eine ungeprüfte Marktorder erzeugen.
        abort_reasons_now = abort_reasons()
        if abort_reasons_now and not bot_config.is_killed():
            reason = " · ".join(abort_reasons_now)
            bot_config.trip_kill_switch(reason)
            db.add_bot_decision(
                cycle_type="deterministic",
                signals_json=json.dumps({"equity_usd": equity, "costs_usd":
                                         (performance_summary() or {}).get("costs_usd")}),
                run_id=None, action="90_day_abort", executed=True, reason=reason,
            )

    # Bestehende Positionen gegen die AKTUELLEN Limits pruefen - unabhaengig
    # von Kill-Switch/Equity-Boden oben (die reagieren auf Verluste, nicht auf
    # ein seither veraendertes Limit). Nur eine Warnung, kein Zwangsschluss.
    limit_warnings = existing_position_limit_warnings(exchange, limits)
    if limit_warnings:
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"warnings": limit_warnings}),
            run_id=None, action="existing_position_limits_exceeded", executed=False,
            reason=" · ".join(limit_warnings),
        )

    if stop_events:
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"stop_events": [e["symbol"] for e in stop_events]}),
            run_id=None, action="stop_triggered", executed=True,
            reason=f"{len(stop_events)} Position(en) am Stop geschlossen.",
        )
    if failed_stop_closes:
        # Bewusst executed=False - naechster Zyklus versucht es erneut, bis
        # die Boerse wieder erreichbar ist oder eine Reconciliation greift.
        db.add_bot_decision(
            cycle_type="deterministic",
            signals_json=json.dumps({"failed": [e["symbol"] for e in failed_stop_closes]}),
            run_id=None, action="stop_close_failed", executed=False,
            reason=f"{len(failed_stop_closes)} ausgelöste(r) Stop(s) konnte(n) nicht geschlossen "
                   f"werden - nächster Zyklus versucht erneut.",
        )

    # ENTSCHEIDUNGS-TEIL (Phase 3): Stops nachziehen und die Signal-Engine
    # laufen nur, wenn seit der letzten Entscheidung ein neues 4h-Fenster
    # (BTC-Anker) abgeschlossen wurde - siehe _due_for_decision()-Docstring.
    # Ein Fehler hier (Kursdaten nicht abrufbar, ein unerwarteter Boersen-
    # fehler) darf den Sicherheits-Teil oben nicht entwerten - der ist zu
    # diesem Zeitpunkt bereits vollstaendig gelaufen.
    decision_candle = _due_for_decision(decision_candles_fn)
    trailing_updates: list = []
    signal_result: dict = {"exits": [], "entry": None, "signals": [],
                           "skipped": "Kein neues 4h-Entscheidungsfenster seit dem letzten Takt."}
    if not decision_candle:
        # Kein 4h-Fenster faellig: einen frueher fehlgeschlagenen Boersen-
        # Austausch trotzdem JETZT nachholen (im Entscheidungsfenster selbst
        # uebernimmt das update_trailing_stops - kein Doppelversuch).
        try:
            trailing_updates = retry_unconfirmed_exchange_stops(exchange)
        except Exception as exc:
            db.add_bot_decision(
                cycle_type="deterministic", signals_json=None, run_id=None,
                action="trailing_stop_exchange_failed", executed=False,
                reason=f"Stop-Wiederholung fehlgeschlagen: {exc!r}")
    if decision_candle:
        trailing_updates = update_trailing_stops(exchange, candles_fn=candles_fn)
        try:
            signal_result = run_signal_cycle(exchange, candles_fn=candles_fn,
                                             equity_trusted=snapshot.get("trusted", True))
        except Exception as e:
            signal_result = {"error": repr(e), "exits": [], "entry": None, "signals": []}
            db.add_bot_decision(
                cycle_type="deterministic", signals_json=None, run_id=None,
                action="signal_cycle_failed", executed=False,
                reason=f"Signal-Zyklus fehlgeschlagen: {e!r}",
            )
        # Marker NUR bei tatsaechlich durchgelaufenem Zyklus setzen (kein
        # "error"-Schluessel in signal_result) - dasselbe Prinzip wie
        # bot_runner._maybe_refresh_costs()'s Tages-Gate: ein VORUEBERGEHENDER
        # Ausfall (Kursdaten, Boersenfehler) darf nicht die einzige
        # Entscheidungsgelegenheit dieses 4h-Fensters verbrauchen - der
        # naechste 15-Minuten-Takt versucht es dann einfach erneut, bis das
        # naechste Fenster schliesst.
        if "error" not in signal_result:
            db.set_meta("bot_last_decision_candle", decision_candle)

    return {"stop_events": stop_events, "failed_stop_closes": failed_stop_closes,
           "reconciliation": reconciliation, "equity": snapshot,
           "trailing_updates": trailing_updates, "signals": signal_result,
           "decision_candle": decision_candle,
           "kill_switch_active": bot_config.is_killed(), "abort_reasons": abort_reasons_now,
           "existing_position_limit_warnings": limit_warnings}
