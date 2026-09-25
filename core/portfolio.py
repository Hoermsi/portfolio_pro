"""Portfolio-Bewertung: verbindet Positionen aus der DB mit Live-Kursen."""
from core import db
from core.models import Position, Valuation, evaluate
from data import crypto as crypto_data
from data import fx as fx_data
from data import stocks as stock_data


def value_position(pos: Position) -> Valuation:
    if pos.asset_type == "crypto":
        price = crypto_data.get_price_eur(pos.symbol)
        return evaluate(pos, price, 1.0)  # CoinGecko liefert direkt EUR
    quote = stock_data.get_quote(pos.symbol)
    if not quote:
        return evaluate(pos, None, None)
    fx = fx_data.get_fx_to_eur(quote.get("currency", "EUR"))
    return evaluate(pos, quote.get("price"), fx)


def valued_positions(asset_type: str) -> list[Valuation]:
    positions = db.list_positions(asset_type)
    if asset_type == "crypto":
        # alle Coins in EINEM CoinGecko-Request (Rate-Limit der freien API)
        prices = crypto_data.get_prices_eur(tuple({p.symbol for p in positions}))
        return [evaluate(p, prices.get(p.symbol), 1.0) for p in positions]
    return [value_position(p) for p in positions]


def crypto_bot_valuation() -> Valuation | None:
    """Synthetische Position 'Krypto Bot' - bündelt den aktuellen Wert des
    Hyperliquid-Trading-Bot-Kontos zu EINER Zeile fürs Krypto-Depot, statt
    seine einzelnen Positionen (die core.bot bereits separat in bot_positions
    verwaltet) im normalen Depot aufzulisten. None, solange der Bot nicht
    aktiv ist oder noch keine Equity-Messung vorliegt.

    Bewusst NICHT Teil von valued_positions() - core.shadow.init_from_real()
    ruft genau diese Funktion auf, um das KI-Portfolio zu befüllen, und
    könnte mit dieser Pseudo-Position nichts anfangen (kein handelbares
    Symbol, kein Kurs). Aufrufer (views/dashboard.py, views/positions.py)
    hängen sie explizit an die Krypto-Liste an.

    id=-1 markiert die Position als nicht editierbar/löschbar - echte
    DB-Ids sind immer positiv (SQLite AUTOINCREMENT). views/positions.py
    blendet Zeilen mit id<=0 aus dem Bearbeiten/Löschen-Formular aus, sonst
    würde ein Speichern dort versuchen, eine echte Position mit Symbol
    'BOT' anzulegen (kein bei CoinGecko auflösbarer Coin).
    """
    from core import bot as bot_module
    from data import fx as fx_data

    if not bot_module.is_active():
        return None
    summary = bot_module.performance_summary()
    if not summary:
        return None
    rate = fx_data.get_fx_to_eur("USD") or 1.0
    value_eur = summary["current_eur"]
    # Einstand so gewählt, dass G/V (value_eur - cost_basis) exakt dem
    # Ein-/Auszahlungs-bereinigten PNL der Trading-Bot-Seite entspricht -
    # keine zweite, abweichende Gewinn/Verlust-Zahl für denselben Bot.
    cost_basis_eur = value_eur - summary["pnl_usd"] * rate
    position = Position(id=-1, symbol="BOT", asset_type="crypto", name="Krypto Bot",
                        currency="USD", quantity=1.0, buy_price_eur=cost_basis_eur,
                        category="Hyperliquid", source="bot")
    return evaluate(position, value_eur, 1.0)


def total_value(valuations: list[Valuation]) -> float:
    return sum(v.value_eur or 0.0 for v in valuations)


def all_priceable(valuations: list[Valuation]) -> bool:
    """True, wenn keine Position einen Bewertungsfehler hat (leere Liste = True).

    Wichtig für Tages-Snapshots: ein kurzzeitiger Kursausfall (yfinance/
    CoinGecko) darf den Verlaufswert nicht künstlich einbrechen lassen - der
    fehlende Kurs zählt in `total_value` sonst stillschweigend als 0.
    """
    return all(v.error is None for v in valuations)


def portfolio_summary() -> dict:
    """Kompakte Übersicht für Agenten/Chat: Positionen, Werte, Allokation."""
    result = {"aktien": [], "krypto": [], "gesamt_eur": 0.0}
    for asset_type, key in (("stock", "aktien"), ("crypto", "krypto")):
        vals = valued_positions(asset_type)
        for v in vals:
            result[key].append({
                "symbol": v.position.symbol,
                "name": v.position.name,
                "menge": v.position.quantity,
                "wert_eur": round(v.value_eur or 0.0, 2),
                "einstand_eur": round(v.cost_basis, 2),
                "gv_eur": round(v.gain_abs, 2) if v.has_cost else None,
                "gv_pct": round(v.gain_pct, 1) if v.has_cost else None,
                "kategorie": v.position.category,
                "fehler": v.error,
            })
        result[f"{key}_summe_eur"] = round(total_value(vals), 2)
    result["cash_eur"] = round(db.latest_cash_balance() or 0.0, 2)
    result["gesamt_eur"] = round(
        result["aktien_summe_eur"] + result["krypto_summe_eur"] + result["cash_eur"], 2
    )
    if result["gesamt_eur"] > 0:
        result["allokation"] = {
            "aktien_pct": round(result["aktien_summe_eur"] / result["gesamt_eur"] * 100, 1),
            "krypto_pct": round(result["krypto_summe_eur"] / result["gesamt_eur"] * 100, 1),
            "cash_pct": round(result["cash_eur"] / result["gesamt_eur"] * 100, 1),
        }
    return result
