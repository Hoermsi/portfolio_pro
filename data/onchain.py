"""BTC-On-Chain-Zyklusmetriken (MVRV Z-Score, Puell Multiple, Mayer Multiple,
STH-MVRV) von bitcoin-data.com - frei, kein Key, aber hart auf 10 Anfragen/Stunde
limitiert (Antwort bei Überschreitung: {"error": {"code": "RATE_LIMIT_HOUR_EXCEEDED"}}).
Deshalb: die Vollhistorie je Metrik wird EINMALIG geladen und in
core.db.onchain_history persistiert (core.db.onchain_bootstrapped/-mark_...),
danach nur noch der günstige /last-Endpunkt - und auch der über ttl_cache
höchstens einmal pro Tag wirklich abgefragt (die Metriken sind Tagesschluss-
Werte, häufigeres Polling brächte nichts, würde aber das Limit belasten).

Jede Funktion ist fehlertolerant wie data/sentiment.py: liefert bei jedem
Problem None zurück statt zu werfen - ein Ausfall dieser Quelle darf die
Zyklus-Anzeige nicht crashen, nur den betroffenen Baustein aus der Gewichtung
nehmen (siehe analysis/cycle.py).
"""
from __future__ import annotations

import requests

from core import db
from core.cache import ttl_cache

_BASE = "https://bitcoin-data.com/v1"
_TIMEOUT = 20

# Bestätigte Endpunkte (verifiziert 21.08.2026 per curl). Andere naheliegende
# Namen wie pi-cycle-top-indicator, fear-greed-index, bitcoin-price liefern
# bei diesem Anbieter 404 - nicht verwenden, Pi-Cycle wird stattdessen aus der
# yfinance-Kurshistorie selbst berechnet (analysis/cycle.py).
METRICS = ("mvrv-zscore", "puell-multiple", "mayer-multiple", "sth-mvrv")


def _get(path: str):
    """Roher GET gegen die API. Eigener Modul-Helfer, damit Tests ihn
    monkeypatchen können (Muster data/crypto.py._get). None bei jedem Fehler
    inkl. Rate-Limit - Aufrufer müssen nie auf Exceptions prüfen."""
    try:
        r = requests.get(f"{_BASE}/{path}", timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"onchain._get({path}): {e}")
        return None
    if isinstance(data, dict) and data.get("error"):
        print(f"onchain._get({path}): {data['error']}")
        return None
    return data


def _value_field(row: dict) -> float | None:
    """Der eigentliche Metrikwert steckt je Endpunkt unter einem eigenen
    camelCase-Feldnamen (z.B. 'mvrvZscore', 'puellMultiple', 'sthMvrv').
    Statt das pro Metrik hart zu verdrahten (und bei jeder API-Umbenennung
    zu brechen), wird das einzige numerische Feld neben 'd'/'unixTs' genommen."""
    for k, v in row.items():
        if k in ("d", "unixTs"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def bootstrap(metric: str) -> bool:
    """Lädt die Vollhistorie einer Metrik EINMALIG pro Installation und
    persistiert sie in core.db.onchain_history. True bei Erfolg oder wenn
    bereits gebootstrapped. Kein Aufruf, sobald core.db.onchain_bootstrapped()
    True liefert - das ist die eigentliche Rate-Limit-Bremse."""
    if db.onchain_bootstrapped(metric):
        return True
    data = _get(metric)
    if not isinstance(data, list) or not data:
        return False
    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        v = _value_field(row)
        d = row.get("d")
        if v is None or not d:
            continue
        rows.append({"d": d, "value": v})
    if not rows:
        return False
    db.save_onchain(metric, rows)
    db.mark_onchain_bootstrapped(metric)
    return True


@ttl_cache(86400)
def latest(metric: str) -> float | None:
    """Aktuellster Wert einer Metrik. Stößt bei Bedarf einmalig den Bootstrap
    an; direkt danach ist der neueste Wert bereits die letzte Zeile der frisch
    geladenen Vollhistorie - ein zusätzlicher /last-Request am selben Tag wäre
    verschwendetes Kontingent. Erst ab dem nächsten Tag (neuer Cache-Eintrag)
    wird /last tatsächlich einmal befragt."""
    just_bootstrapped = not db.onchain_bootstrapped(metric)
    if not bootstrap(metric):
        return None
    if just_bootstrapped:
        rows = db.list_onchain(metric)
        return rows[-1]["value"] if rows else None
    row = _get(f"{metric}/last")
    if not isinstance(row, dict):
        rows = db.list_onchain(metric)
        return rows[-1]["value"] if rows else None
    v = _value_field(row)
    d = row.get("d")
    if v is not None and d:
        db.save_onchain(metric, [{"d": d, "value": v}])
    return v


def history(metric: str, days: int | None = None) -> list[dict]:
    """Lokale Historie nach Bootstrap - [{"d": "YYYY-MM-DD", "value": float}, ...],
    chronologisch aufsteigend. Liest nur die Persistenz (core.db.list_onchain),
    kein zusätzlicher Netzzugriff außer dem einmaligen Bootstrap."""
    bootstrap(metric)
    return db.list_onchain(metric, days=days)


def mvrv_zscore() -> float | None:
    return latest("mvrv-zscore")


def puell_multiple() -> float | None:
    return latest("puell-multiple")


def mayer_multiple() -> float | None:
    """On-Chain-Mayer-Multiple mit voller Historie - im Unterschied zu
    analysis.market_timing.mayer_multiple(), das aus yfinance BTC-EUR/MA200
    nur eine 1-Jahres-Näherung bildet. analysis/cycle.py nutzt diese Version
    fürs Perzentil-Scoring, wo die volle Historie zählt."""
    return latest("mayer-multiple")


def sth_mvrv() -> float | None:
    return latest("sth-mvrv")
