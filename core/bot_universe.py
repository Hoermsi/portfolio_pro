"""Dynamischer Kandidaten-Pool fuer den Trading-Bot.

Ersetzt die frueher feste 11-Symbol-Liste (core.bot.CANDIDATE_UNIVERSE) durch
eine taeglich aktualisierte, deterministisch gefilterte Auswahl: Mindest-
marktkapitalisierung + Mindest-"Alter" (etablierter Altcoin, keine frische
Notierung/Hype) + Hyperliquid-Liquiditaet. BEWUSST OHNE KI-Screen in dieser
Ausbaustufe - die Funktionen hier sind so geschnitten, dass eine spaetere
KI-Stufe (z.B. ein Nachrichten-Screen auf der Stufe-1-Shortlist) ergaenzt
werden koennte, ohne diesen Teil umzubauen.

CACHING: das Ergebnis wird EINMAL TAEGLICH ermittelt (bot_runner.py,
_maybe_refresh_universe(), Muster core.bot._maybe_refresh_costs) und in
core.db-Meta zwischengespeichert - core.bot.candidate_symbols() liest bei
JEDEM Takt nur den Cache, nie live CoinGecko/Hyperliquid. Ein taeglicher statt
15-minuetiger Wechsel reicht fuer eine 3-14-Tage-Swing-Strategie (core.
bot_signals.MAX_HOLD_HOURS) voellig aus und haelt sowohl das enge CoinGecko-
Rate-Limit als auch core.bot_signals.scan()s sequenzielle Netzaufrufe je
Kandidat im Rahmen.

FEHLT der Cache (allererster Lauf oder kaputter Meta-Wert), faellt
core.bot.candidate_symbols() auf die alte feste Liste zurueck. Ein
erfolgreich gespeicherter, aber leerer Pool bleibt dagegen bewusst leer: ein
temporärer Datenfehler darf weder einen guten Cache ueberschreiben noch eine
unbeabsichtigte Rueckkehr zum alten, anders validierten Universum ausloesen.
"""
import json

from core import bot_signals, clock, db

# --- FESTE SCHWELLEN (Code-Konstanten, kein Regler/Override - Muster
# core.bot_config.strategy_constants(): Strategie-Parameter, keine Risiko-
# Groesse) ---
MIN_MARKET_CAP_EUR = 500_000_000.0
MIN_LISTING_AGE_DAYS = 180
# Dieselbe Zahl wie core.bot_signals' Gate 7 (Liquiditaet) - EIN Wert, nicht
# zwei unabhaengig gepflegte, die sonst auseinanderdriften koennten. Ein
# Kandidat, der diese Schwelle hier nicht reisst, wuerde ohnehin jeden Takt
# an Gate 7 scheitern und nur einen Scan-Platz verschwenden.
MIN_HL_DAY_VOLUME_USD = bot_signals.MIN_24H_VOLUME_USD
# Sicherheitsobergrenze: core.bot_signals.scan() ruft pro Kandidat sequenziell
# candles_df() auf (echter, nicht gecachter Netzaufruf je Symbol) - 30 statt
# der bisherigen 11 sind bei 15-Minuten-Cadence unproblematisch, ohne dass
# scan() Nebenlaeufigkeit braeuchte. Wird der ermittelte Pool groesser, gewinnen
# die Symbole mit der hoechsten Marktkap. (pool_symbols() liefert rangsortiert).
MAX_ACTIVE_CANDIDATES = 30
DISCOVERY_REFRESH_INTERVAL_HOURS = 24.0
# Hart gesperrte Symbole - unabhaengig von Marktkap./Alter/Liquiditaet NIE
# als Kandidat vorgeschlagen. Bewusst eine Code-Konstante statt einer
# UI-Einstellung (Muster: core.bot_config.strategy_constants()) - eine
# Sperre, die sich per Klick wieder aufheben liesse, waere keine. Wird sowohl
# in apply_deterministic_filters() (fuer die UI-Transparenz: taucht als
# abgelehnt mit Grund auf) als auch defensiv in core.bot.candidate_symbols()
# geprueft (greift damit auch, falls der CANDIDATE_UNIVERSE-Rueckfall aktiv
# ist, nicht nur im dynamischen Pool).
BLOCKED_SYMBOLS = frozenset({"TRUMP"})
# Breite genug, um nach den drei Gates unten realistisch auf eine zweistellige
# Kandidatenzahl zu kommen, ohne CoinGecko mehrfach abzufragen (ein Request,
# siehe data.crypto.get_top_market_cap).
DISCOVERY_TOP_N = 250

_POOL_META_KEY = "bot_universe_pool"
_POOL_REFRESHED_AT_META_KEY = "bot_universe_pool_refreshed_at"


class UniverseDiscoveryError(RuntimeError):
    """Ermittlung unvollstaendig; vorhandener Cache muss erhalten bleiben."""


def apply_deterministic_filters(rows: list[dict], supported: set[str]) -> tuple[list[dict], list[dict]]:
    """Wendet die drei Pflichtbedingungen an - Symmetrie zu core.bot_signals.
    evaluate()s Gates: jede Ablehnung traegt einen lesbaren Grund, kein
    stilles Verwerfen. Gibt (qualifiziert, abgelehnt) zurueck, rangsortiert
    (hoechste Marktkap. zuerst) - `qualifiziert` ist die Grundlage fuer
    pool_symbols(), `abgelehnt` fuer die UI-Transparenz.
    """
    now = clock.now_utc()
    qualified, rejected = [], []
    for row in rows:
        symbol = row["symbol"]
        reasons = []
        if symbol in BLOCKED_SYMBOLS:
            reasons.append("Manuell gesperrt (core.bot_universe.BLOCKED_SYMBOLS).")
        if symbol not in supported:
            reasons.append("Auf Hyperliquid nicht als Perp gelistet.")
        cap = row.get("marktkap_eur")
        if not cap or cap < MIN_MARKET_CAP_EUR:
            reasons.append(f"Marktkapitalisierung {cap or 0:,.0f} € unter "
                           f"{MIN_MARKET_CAP_EUR:,.0f} €.")
        earliest = clock.parse_utc(row.get("aeltester_datenpunkt"))
        age_days = (now - earliest).total_seconds() / 86400 if earliest else None
        if age_days is None or age_days < MIN_LISTING_AGE_DAYS:
            age_txt = f"{age_days:.0f}" if age_days is not None else "unbekannt"
            reasons.append(f"Alter {age_txt} Tage unter {MIN_LISTING_AGE_DAYS} Tagen (nicht etabliert).")
        volume = row.get("hl_day_volume_usd")
        if volume is None or volume < MIN_HL_DAY_VOLUME_USD:
            vol_txt = "unbekannt" if volume is None else f"{volume:,.0f} $"
            reasons.append(f"Hyperliquid-24h-Volumen {vol_txt} unter "
                           f"{MIN_HL_DAY_VOLUME_USD:,.0f} $.")
        out_row = {**row, "age_days": age_days}
        if reasons:
            out_row["rejected_reason"] = " ".join(reasons)
            rejected.append(out_row)
        else:
            out_row["included_reason"] = (
                f"Marktkap. {cap:,.0f} € (Rang {row.get('rang', '?')}) · "
                f"{age_days:.0f} Tage · HL-Volumen {volume:,.0f} $")
            qualified.append(out_row)
    qualified.sort(key=lambda r: r.get("rang") or 10**9)
    rejected.sort(key=lambda r: r.get("rang") or 10**9)
    return qualified, rejected


def discover_candidates(top_n: int = DISCOVERY_TOP_N) -> dict:
    """Stufe 1 (einzige Stufe in dieser Ausbaustufe): CoinGecko Top-N nach
    Marktkap. laden, gegen Hyperliquid-Liquiditaet anreichern, Gates anwenden.
    Reine Ermittlung - schreibt NICHTS in die DB, das macht refresh_pool()."""
    from data import crypto, hyperliquid

    rows = crypto.get_top_market_cap(top_n)
    if not rows:
        raise UniverseDiscoveryError("CoinGecko lieferte keine Marktkapitalisierungsdaten.")
    try:
        supported = hyperliquid.supported_symbols()
    except Exception as exc:
        raise UniverseDiscoveryError("Hyperliquid-Symbolliste nicht verfügbar.") from exc
    if not supported:
        raise UniverseDiscoveryError("Hyperliquid lieferte keine handelbaren Perp-Symbole.")
    # Mehrfache Ticker (verschiedene Coins mit demselben Symbol) sind bei den
    # groessten Marktkap.-Rängen selten, aber nicht ausgeschlossen - der zuerst
    # (= hoechstrangige) Treffer gewinnt, CoinGecko liefert bereits nach
    # Marktkap. sortiert.
    seen = set()
    deduped = []
    for row in rows:
        if row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        deduped.append(row)
    for row in deduped:
        row["hl_day_volume_usd"] = (
            hyperliquid.day_notional_volume_usd(row["symbol"]) if row["symbol"] in supported else None)
    qualified, rejected = apply_deterministic_filters(deduped, supported)
    return {"qualified": qualified, "rejected": rejected}


def refresh_pool(force: bool = False) -> dict:
    """Fuehrt discover_candidates() aus und persistiert das Ergebnis, sofern
    seit dem letzten Erfolg DISCOVERY_REFRESH_INTERVAL_HOURS vergangen sind
    (oder `force=True`, fuer den manuellen UI-Knopf). Gate rueckt NUR bei
    Erfolg vor - ein Fehlschlag (CoinGecko down) laesst die naechste
    Auffrischung sofort erneut zu, statt einen vollen Tag zu warten (Muster
    core.bot._maybe_refresh_costs)."""
    if not force:
        last = clock.parse_utc(db.get_meta(_POOL_REFRESHED_AT_META_KEY))
        if last is not None:
            age_hours = (clock.now_utc() - last).total_seconds() / 3600
            if age_hours < DISCOVERY_REFRESH_INTERVAL_HOURS:
                return {"skipped": True, "reason": "Noch nicht faellig."}
    result = discover_candidates()
    db.set_meta(_POOL_META_KEY, json.dumps(result["qualified"]))
    db.set_meta(_POOL_REFRESHED_AT_META_KEY, clock.iso_utc())
    return {"skipped": False, "symbols": [r["symbol"] for r in result["qualified"]],
           "rejected_count": len(result["rejected"])}


def active_pool() -> list[dict]:
    """Letzter persistierter, qualifizierter Pool - kein Netzaufruf, reiner
    Cache-Read. Rangsortiert (hoechste Marktkap. zuerst, wie
    apply_deterministic_filters() es beim Schreiben bereits sortiert hat)."""
    raw = db.get_meta(_POOL_META_KEY)
    if not raw:
        return []
    try:
        pool = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(pool, list):
        return []
    # Eine nachtraegliche Sperre muss auch einen bereits gespeicherten Pool
    # sofort treffen, nicht erst nach dem naechsten 24h-Refresh.
    return [row for row in pool if isinstance(row, dict)
            and str(row.get("symbol") or "").upper() not in BLOCKED_SYMBOLS]


def has_active_snapshot() -> bool:
    """Unterscheidet keinen/defekten Cache von einem gueltigen Leer-Pool.

    Diese kleine Unterscheidung ist sicherheitsrelevant: ``[]`` nach einer
    erfolgreichen Ermittlung bedeutet *keine neue Position*, nicht
    "verwende unbemerkt das alte V1-Universum".
    """
    raw = db.get_meta(_POOL_META_KEY)
    if raw is None:
        return False
    try:
        return isinstance(json.loads(raw), list)
    except (json.JSONDecodeError, TypeError):
        return False


def pool_refreshed_at() -> str | None:
    return db.get_meta(_POOL_REFRESHED_AT_META_KEY)


def pool_symbols() -> list[str]:
    """Nur die Symbolliste aus active_pool(), fuer core.bot.candidate_symbols()."""
    return [str(r["symbol"]).upper() for r in active_pool() if r.get("symbol")]


def fingerprint_payload() -> dict:
    """Der aktuelle Pool ist Teil des gehandelten Regelwerks und wird daher
    im Konfigurations-Fingerabdruck mitgeführt. Ein Wechsel des Universums
    invalidiert so bewusst einen alten Walk-Forward-Nachweis."""
    return {
        "min_market_cap_eur": MIN_MARKET_CAP_EUR,
        "min_listing_age_days": MIN_LISTING_AGE_DAYS,
        "min_hl_day_volume_usd": MIN_HL_DAY_VOLUME_USD,
        "max_active_candidates": MAX_ACTIVE_CANDIDATES,
        "blocked_symbols": sorted(BLOCKED_SYMBOLS),
        "has_snapshot": has_active_snapshot(),
        "symbols": pool_symbols(),
    }
