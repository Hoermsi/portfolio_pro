"""Watchlist-Kennzahlen und Kursalarm-Auswertung.

Liefert für ein Symbol den aktuellen EUR-Kurs, die Tagesveränderung und den RSI
und wertet daraus die pro Watchlist-Eintrag konfigurierten Alarme aus
(Zielkurs über/unter, Tages-Sprung in %, RSI überkauft/überverkauft). Wird von
der Watchlist-UI (Anzeige) und vom Dashboard (ausgelöste Alarme) gemeinsam
genutzt.

Kennzahlen werden für alle Einträge PARALLEL geholt (ThreadPoolExecutor wie bei
den Analyse-Spezialisten) - die zugrunde liegenden Datenmodule cachen
thread-sicher (core/cache.ttl_cache mit Lock).

Alarme sind quittierbar: ein "Gesehen" merkt sich (watch_id, Art, Schwelle) im
Meta-Key `alert_acks` und unterdrückt den Alarm, bis der Nutzer die Schwelle
oder Art ändert - dann feuert er wieder.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from analysis import technical
from core import db
from data import crypto as crypto_data
from data import fx as fx_data
from data import stocks as stock_data

_RSI_HIGH = 70.0
_RSI_LOW = 30.0
_ACK_META_KEY = "alert_acks"


def asset_metrics(symbol: str, asset_type: str) -> dict:
    """{price_eur, day_pct, rsi} für ein Symbol. Einzelne Felder sind None, wenn
    nicht abrufbar."""
    symbol = symbol.strip().upper()
    result: dict = {"price_eur": None, "day_pct": None, "rsi": None}
    try:
        if asset_type == "crypto":
            df = crypto_data.get_history(symbol, days=90)
            price = crypto_data.get_price_eur(symbol)
        else:
            df = stock_data.get_history(symbol, "3mo")
            quote = stock_data.get_quote(symbol)
            fx = fx_data.get_fx_to_eur(quote["currency"]) if quote else None
            price = quote["price"] * fx if quote and fx else None
    except Exception:
        return result

    result["price_eur"] = float(price) if price else None

    if df is not None and "Close" in df:
        close = df["Close"].dropna()
        if len(close) >= 2:
            prev, last = float(close.iloc[-2]), float(close.iloc[-1])
            if prev > 0:
                result["day_pct"] = (last / prev - 1.0) * 100.0
        if len(close) >= 15:
            try:
                result["rsi"] = float(technical.summarize(df)["rsi"])
            except Exception:
                pass
    return result


def metrics_for(entries: list[dict]) -> dict[int, dict]:
    """Kennzahlen für mehrere Watchlist-Einträge parallel holen: {id: metrics}."""
    if not entries:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {e["id"]: pool.submit(asset_metrics, e["symbol"], e["asset_type"])
                   for e in entries}
        return {wid: fut.result() for wid, fut in futures.items()}


def _triggers_for(entry: dict, metrics: dict) -> list[dict]:
    """Ausgelöste Alarme eines Eintrags: [{kind, text, threshold}]."""
    triggers: list[dict] = []
    price = metrics.get("price_eur")
    day_pct = metrics.get("day_pct")
    rsi = metrics.get("rsi")

    if price is not None:
        above = entry.get("target_above")
        below = entry.get("target_below")
        if above is not None and price >= above:
            triggers.append({"kind": "above", "threshold": above,
                             "text": f"über Zielkurs {above:,.2f} € (aktuell {price:,.2f} €)"})
        if below is not None and price <= below:
            triggers.append({"kind": "below", "threshold": below,
                             "text": f"unter Zielkurs {below:,.2f} € (aktuell {price:,.2f} €)"})

    threshold = entry.get("day_move_pct")
    if threshold is not None and day_pct is not None and abs(day_pct) >= threshold:
        triggers.append({"kind": "move", "threshold": threshold,
                         "text": f"Tagesbewegung {day_pct:+.1f} % (Schwelle ±{threshold:.1f} %)"})

    if entry.get("rsi_alert") and rsi is not None:
        if rsi >= _RSI_HIGH:
            triggers.append({"kind": "rsi", "threshold": "on",
                             "text": f"RSI {rsi:.0f} – überkauft"})
        elif rsi <= _RSI_LOW:
            triggers.append({"kind": "rsi", "threshold": "on",
                             "text": f"RSI {rsi:.0f} – überverkauft"})
    return triggers


def _ack_key(watch_id: int, trigger: dict) -> str:
    return f"{watch_id}:{trigger['kind']}:{trigger['threshold']}"


def _load_acks() -> dict:
    raw = db.get_meta(_ACK_META_KEY)
    try:
        acks = json.loads(raw) if raw else {}
        return acks if isinstance(acks, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def acknowledge(watch_id: int, triggers: list[dict]):
    """Alarme quittieren: unterdrückt sie, bis sich Art/Schwelle ändert."""
    acks = _load_acks()
    now = datetime.now().isoformat(timespec="seconds")
    for t in triggers:
        acks[_ack_key(watch_id, t)] = now
    db.set_meta(_ACK_META_KEY, json.dumps(acks))


def evaluate_watchlist() -> list[dict]:
    """Alle Watchlist-Einträge auswerten; nur nicht-quittierte Alarme zurückgeben:
    {id, symbol, name, asset_type, price_eur, triggers:[{kind,text,threshold}]}."""
    entries = [e for e in db.list_watchlist()
               if (e.get("target_above") is not None or e.get("target_below") is not None
                   or e.get("day_move_pct") is not None or e.get("rsi_alert"))]
    if not entries:
        return []
    acks = _load_acks()
    all_metrics = metrics_for(entries)
    out: list[dict] = []
    for entry in entries:
        metrics = all_metrics.get(entry["id"], {})
        triggers = [t for t in _triggers_for(entry, metrics)
                    if _ack_key(entry["id"], t) not in acks]
        if triggers:
            out.append({
                "id": entry["id"],
                "symbol": entry["symbol"],
                "name": entry.get("name") or "",
                "asset_type": entry["asset_type"],
                "price_eur": metrics.get("price_eur"),
                "triggers": triggers,
            })
    return out
