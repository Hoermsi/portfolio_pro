"""Watchlist-Kennzahlen und Kursalarm-Auswertung.

Liefert für ein Symbol den aktuellen EUR-Kurs, die Tagesveränderung und den RSI
und wertet daraus die pro Watchlist-Eintrag konfigurierten Alarme aus
(Zielkurs über/unter, Tages-Sprung in %, RSI überkauft/überverkauft). Wird von
der Watchlist-UI (Anzeige) und vom Dashboard (ausgelöste Alarme) gemeinsam
genutzt. Externe Kursabfragen laufen über die gecachten Datenmodule.
"""
from __future__ import annotations

from analysis import technical
from core import db
from data import crypto as crypto_data
from data import fx as fx_data
from data import stocks as stock_data

_RSI_HIGH = 70.0
_RSI_LOW = 30.0


def asset_metrics(symbol: str, asset_type: str) -> dict:
    """{price_eur, day_pct, rsi} für ein Symbol. Einzelne Felder sind None, wenn
    nicht abrufbar. Watchlists sind klein -> Per-Symbol-Fetch ist vertretbar."""
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


def _triggers_for(entry: dict, metrics: dict) -> list[str]:
    """Textbausteine der ausgelösten Alarme für einen Watchlist-Eintrag."""
    triggers: list[str] = []
    price = metrics.get("price_eur")
    day_pct = metrics.get("day_pct")
    rsi = metrics.get("rsi")

    if price is not None:
        above = entry.get("target_above")
        below = entry.get("target_below")
        if above is not None and price >= above:
            triggers.append(f"über Zielkurs {above:,.2f} € (aktuell {price:,.2f} €)")
        if below is not None and price <= below:
            triggers.append(f"unter Zielkurs {below:,.2f} € (aktuell {price:,.2f} €)")

    threshold = entry.get("day_move_pct")
    if threshold is not None and day_pct is not None and abs(day_pct) >= threshold:
        triggers.append(f"Tagesbewegung {day_pct:+.1f} % (Schwelle ±{threshold:.1f} %)")

    if entry.get("rsi_alert") and rsi is not None:
        if rsi >= _RSI_HIGH:
            triggers.append(f"RSI {rsi:.0f} – überkauft")
        elif rsi <= _RSI_LOW:
            triggers.append(f"RSI {rsi:.0f} – überverkauft")
    return triggers


def evaluate_watchlist() -> list[dict]:
    """Alle Watchlist-Einträge auswerten und nur die mit ausgelösten Alarmen
    zurückgeben: {symbol, name, asset_type, price_eur, triggers:[...]}."""
    out: list[dict] = []
    for entry in db.list_watchlist():
        if (entry.get("target_above") is None and entry.get("target_below") is None
                and entry.get("day_move_pct") is None and not entry.get("rsi_alert")):
            continue  # kein Alarm konfiguriert -> nicht abfragen
        metrics = asset_metrics(entry["symbol"], entry["asset_type"])
        triggers = _triggers_for(entry, metrics)
        if triggers:
            out.append({
                "symbol": entry["symbol"],
                "name": entry.get("name") or "",
                "asset_type": entry["asset_type"],
                "price_eur": metrics.get("price_eur"),
                "triggers": triggers,
            })
    return out
