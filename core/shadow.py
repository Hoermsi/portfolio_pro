"""Schatten-Portfolios: virtuelle, KI-gemanagte Depots als Vergleich zum echten.

Es gibt ZWEI getrennte Experimente (scope):
  - "crypto": Krypto-KI-Depot, tritt gegen das echte Krypto-Depot an
  - "stock":  Aktien-KI-Depot, tritt gegen das echte Aktien-Depot an

Jedes startet als exakte Kopie der jeweiligen Klasse des echten Portfolios.
Alle KI-Empfehlungen werden virtuell zu Live-Kursen ausgeführt (mit pauschalem
Trade-Abschlag). Cross-Type-Trades (z.B. Aktie im Krypto-Depot) werden verworfen.
"""
import json
from datetime import date

import pandas as pd

from core import db
from core.portfolio import total_value as real_total_value
from core.portfolio import valued_positions
from data import crypto as crypto_data
from data import fx as fx_data
from data import stocks as stock_data

# Pauschaler Gebühren-/Slippage-Abschlag je virtuellem Trade
TRADE_COST = 0.0025          # 0,25 %
CASH_SYMBOL = "CASH"
CASH_TYPE = "cash"

SCOPES = {"crypto": "Krypto-KI", "stock": "Aktien-KI"}


def _meta_key(scope: str) -> str:
    return f"shadow_start_{scope}"


# --- LEBENSZYKLUS ---

def exists(scope: str) -> bool:
    return db.get_meta(_meta_key(scope)) is not None


def start_info(scope: str) -> dict | None:
    raw = db.get_meta(_meta_key(scope))
    return json.loads(raw) if raw else None


def init_from_real(scope: str) -> dict:
    """Legt das Schatten-Portfolio des Scopes als Kopie der entsprechenden
    Klasse des echten Portfolios an (überschreibt ein evtl. vorhandenes).

    Nicht bepreisbare Positionen werden ausgelassen UND in `skipped` gemeldet -
    so passen Startwert (Baseline) und kopierte Positionen immer zusammen; eine
    fehlende NVDA-Bewertung kann die Vergleichsbasis nicht mehr verzerren.
    """
    db.clear_shadow(scope)
    real_vals = valued_positions(scope)
    # Mengen pro Symbol aggregieren - dasselbe Symbol kann in mehreren
    # Kategorien liegen (z.B. NVDA in 'Standard' und 'Flatex-Import').
    aggregated: dict[str, float] = {}
    start_value = 0.0
    skipped: list[str] = []
    for v in real_vals:
        if v.position.quantity <= 0:
            continue
        if v.value_eur is None:
            skipped.append(v.position.symbol)
            continue
        aggregated[v.position.symbol] = (
            aggregated.get(v.position.symbol, 0.0) + v.position.quantity
        )
        start_value += v.value_eur
    holdings: list[dict] = []
    for symbol, qty in aggregated.items():
        # EUR-Bestand (z.B. Kraken-Cash) wird als Cash-Position geführt, damit
        # der Stratege ihn direkt per 'kaufen' einsetzen kann.
        if symbol == "EUR":
            db.set_shadow_position(scope, CASH_SYMBOL, CASH_TYPE, qty)
            holdings.append({"symbol": CASH_SYMBOL, "asset_type": CASH_TYPE, "quantity": qty})
        else:
            db.set_shadow_position(scope, symbol, scope, qty)
            holdings.append({"symbol": symbol, "asset_type": scope, "quantity": qty})

    info = {
        "date": date.today().isoformat(),
        "real_start": round(start_value, 2),
        "shadow_start": round(start_value, 2),
        "skipped": sorted(set(skipped)),
        # Eingefrorene Startbestände für die Buy&Hold-/Gleichgewichts-Baseline
        # (comparison_df). Experimente von vor dieser Funktion haben dieses
        # Feld nicht - baseline_columns() degradiert dafür sauber auf "leer".
        "holdings": holdings,
    }
    db.set_meta(_meta_key(scope), json.dumps(info))
    # Startpunkt beider Verlaufsreihen auf denselben Tag/Wert setzen (konsistent
    # mit den tatsächlich aufgenommenen Positionen)
    db.save_shadow_snapshot(scope, start_value, info["date"])
    db.save_snapshot(scope, start_value, info["date"])
    return info


def reset(scope: str):
    db.clear_shadow(scope)


# --- BEWERTUNG ---

def price_eur(symbol: str, asset_type: str) -> float | None:
    if asset_type == CASH_TYPE:
        return 1.0
    if asset_type == "crypto":
        return crypto_data.get_price_eur(symbol)
    quote = stock_data.get_quote(symbol)
    if not quote:
        return None
    fx = fx_data.get_fx_to_eur(quote.get("currency", "EUR"))
    # Kein Fallback auf 1.0: ein nicht ermittelbarer Wechselkurs würde eine
    # z.B. 185-USD-Aktie sonst als 185 € bewerten/handeln (~14% zu hoch) -
    # lieber "kein Kurs" als eine stillschweigend falsche Bewertung.
    if fx is None:
        return None
    return quote.get("price") * fx


def valued_shadow(scope: str) -> list[dict]:
    """Bewertet alle Positionen des Scopes in EUR (Krypto als Batch-Abruf)."""
    positions = db.shadow_positions(scope)
    crypto_symbols = tuple({p["symbol"] for p in positions if p["asset_type"] == "crypto"})
    crypto_prices = crypto_data.get_prices_eur(crypto_symbols) if crypto_symbols else {}

    result = []
    for p in positions:
        at = p["asset_type"]
        if at == CASH_TYPE:
            price = 1.0
        elif at == "crypto":
            price = crypto_prices.get(p["symbol"])
        else:
            price = price_eur(p["symbol"], at)
        value = price * p["quantity"] if price is not None else None
        result.append({
            "symbol": p["symbol"],
            "asset_type": at,
            "quantity": p["quantity"],
            "price_eur": price,
            "value_eur": value,
            "error": None if price is not None else "Kein Kurs",
        })
    return result


def total_value(scope: str, vals: list[dict] | None = None) -> float:
    vals = vals if vals is not None else valued_shadow(scope)
    return sum(v["value_eur"] or 0.0 for v in vals)


def cash_balance(scope: str) -> float:
    return db.get_shadow_quantity(scope, CASH_SYMBOL, CASH_TYPE)


# --- VIRTUELLE TRADES ---

def _add_cash(scope: str, amount: float):
    db.set_shadow_position(scope, CASH_SYMBOL, CASH_TYPE, cash_balance(scope) + amount)

def _add_quantity(scope: str, symbol: str, asset_type: str, delta: float):
    current = db.get_shadow_quantity(scope, symbol, asset_type)
    db.set_shadow_position(scope, symbol, asset_type, current + delta)


def apply_recommendation(scope: str, rec: dict, run_id: int | None = None) -> dict:
    """Führt eine einzelne Empfehlung virtuell im Scope-Depot aus.

    rec: {aktion, symbol, asset_type, ziel_symbol, ziel_asset_type, anteil_pct,
          begruendung}. Gibt ein Ergebnis-Dict mit status/notiz zurück.
    """
    aktion = (rec.get("aktion") or "").lower()
    symbol = (rec.get("symbol") or "").strip().upper()
    asset_type = rec.get("asset_type") or scope
    ziel_symbol = (rec.get("ziel_symbol") or "").strip().upper()
    ziel_type = rec.get("ziel_asset_type") or scope
    anteil = rec.get("anteil_pct")
    anteil = float(anteil) if anteil is not None else 100.0
    anteil = max(0.0, min(100.0, anteil))
    frac = anteil / 100.0

    # Strikte Trennung: nur die eigene Asset-Klasse + Cash ist erlaubt
    allowed = {scope, CASH_TYPE}
    if (symbol and asset_type not in allowed) or (ziel_symbol and ziel_type not in allowed):
        db.add_recommendation(scope, run_id, aktion, symbol or None, asset_type,
                              ziel_symbol or None, ziel_type, anteil,
                              rec.get("begruendung", ""), None, None, False)
        return {"status": "skip",
                "notiz": f"{symbol or ziel_symbol}: falsche Asset-Klasse für dieses Depot"}

    kurs_symbol = price_eur(symbol, asset_type) if symbol else None
    kurs_ziel = price_eur(ziel_symbol, ziel_type) if ziel_symbol else None

    def _record(applied: bool):
        return db.add_recommendation(
            scope, run_id, aktion, symbol or None, asset_type if symbol else None,
            ziel_symbol or None, ziel_type if ziel_symbol else None, anteil,
            rec.get("begruendung", ""), kurs_symbol, kurs_ziel, applied,
        )

    # HALTEN: nur protokollieren
    if aktion == "halten":
        rid = _record(True)
        db.add_shadow_log(scope, "halten", symbol or None, None, None, None,
                          kurs_symbol, None, None, rid,
                          rec.get("begruendung", "")[:200])
        return {"status": "ok", "notiz": "gehalten"}

    # VERKAUFEN: Anteil der Position -> Cash
    if aktion == "verkaufen":
        qty = db.get_shadow_quantity(scope, symbol, asset_type)
        if qty <= 0 or kurs_symbol is None:
            _record(False)
            return {"status": "skip", "notiz": f"{symbol}: kein Bestand oder Kurs"}
        sell_qty = qty * frac
        value = sell_qty * kurs_symbol
        proceeds = value * (1 - TRADE_COST)
        _add_quantity(scope, symbol, asset_type, -sell_qty)
        _add_cash(scope, proceeds)
        rid = _record(True)
        db.add_shadow_log(scope, "verkaufen", symbol, CASH_SYMBOL, sell_qty, proceeds,
                          kurs_symbol, 1.0, value, rid, rec.get("begruendung", "")[:200])
        return {"status": "ok", "notiz": f"{sell_qty:.6g} {symbol} verkauft"}

    # KAUFEN: Anteil des Cash -> Position
    if aktion == "kaufen":
        cash = cash_balance(scope)
        if cash <= 0 or kurs_symbol is None:
            _record(False)
            return {"status": "skip", "notiz": f"{symbol}: kein Cash oder Kurs"}
        spend = cash * frac
        effective = spend * (1 - TRADE_COST)
        buy_qty = effective / kurs_symbol
        _add_cash(scope, -spend)
        _add_quantity(scope, symbol, asset_type, buy_qty)
        rid = _record(True)
        db.add_shadow_log(scope, "kaufen", CASH_SYMBOL, symbol, spend, buy_qty,
                          1.0, kurs_symbol, spend, rid, rec.get("begruendung", "")[:200])
        return {"status": "ok", "notiz": f"{buy_qty:.6g} {symbol} gekauft"}

    # UMSCHICHTEN: Anteil von symbol -> ziel_symbol
    if aktion == "umschichten":
        qty = db.get_shadow_quantity(scope, symbol, asset_type)
        if qty <= 0 or kurs_symbol is None or not ziel_symbol or kurs_ziel is None:
            _record(False)
            return {"status": "skip", "notiz": f"{symbol}->{ziel_symbol}: Bestand/Kurs fehlt"}
        sell_qty = qty * frac
        value = sell_qty * kurs_symbol
        effective = value * (1 - TRADE_COST)
        buy_qty = effective / kurs_ziel
        _add_quantity(scope, symbol, asset_type, -sell_qty)
        _add_quantity(scope, ziel_symbol, ziel_type, buy_qty)
        rid = _record(True)
        db.add_shadow_log(scope, "umschichten", symbol, ziel_symbol, sell_qty, buy_qty,
                          kurs_symbol, kurs_ziel, value, rid, rec.get("begruendung", "")[:200])
        return {"status": "ok", "notiz": f"{symbol} -> {ziel_symbol} ({anteil:.0f}%)"}

    _record(False)
    return {"status": "skip", "notiz": f"unbekannte Aktion: {aktion}"}


def apply_recommendations(scope: str, recs: list[dict], run_id: int | None = None) -> list[dict]:
    results = []
    for rec in recs:
        try:
            results.append(apply_recommendation(scope, rec, run_id))
        except Exception as e:
            results.append({"status": "error", "notiz": str(e)})
    record_snapshot(scope)
    return results


# --- SNAPSHOTS & VERGLEICH ---

def record_snapshot(scope: str):
    """Tageswert des Scope-Depots festhalten (falls initialisiert)."""
    if not exists(scope):
        return
    db.save_shadow_snapshot(scope, total_value(scope))


def _price_panel(holdings: list[dict], dates: pd.DatetimeIndex) -> dict[str, pd.Series | None]:
    """EUR-Tageskurs je gehaltenem Symbol über `dates`, vorwärtsgefüllt.
    None für ein Symbol, wenn keine Historie auflösbar ist - ein einzelner
    fehlender Kurs darf die ganze Baseline nicht zum Absturz bringen."""
    panel: dict[str, pd.Series | None] = {}
    span_days = max((date.today() - dates[0].date()).days + 5, 30)
    for h in holdings:
        sym, at = h["symbol"], h["asset_type"]
        if at == CASH_TYPE:
            panel[sym] = pd.Series(1.0, index=dates)
            continue
        try:
            if at == "crypto":
                hist = crypto_data.get_history(sym, days=min(span_days, 365))
            else:
                period = "1y" if span_days <= 365 else ("5y" if span_days <= 1825 else "10y")
                hist = stock_data.get_history(sym, period=period)
        except Exception:
            hist = None
        if hist is None or hist.empty or "Close" not in hist:
            panel[sym] = None
            continue
        close = hist["Close"]
        if at == "stock":
            fx = fx_data.get_fx_to_eur(stock_data.get_currency(sym)) or 1.0
            close = close * fx
        close = close.sort_index()
        panel[sym] = close.reindex(dates.union(close.index)).ffill().reindex(dates)
    return panel


def baseline_columns(scope: str, dates: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """Rohe EUR-Wertreihen für Buy&Hold und Gleichgewicht über `dates`.

    Leeres Dict, wenn keine eingefrorenen Startbestände hinterlegt sind (z.B.
    ein Experiment von vor dieser Funktion) oder keine Kurshistorie für die
    Startbestände auflösbar ist - degradiert bewusst still statt zu crashen.
    """
    info = start_info(scope)
    holdings = (info or {}).get("holdings")
    if not holdings:
        return {}
    panel = _price_panel(holdings, dates)
    usable = [h for h in holdings if panel.get(h["symbol"]) is not None]
    if not usable:
        return {}
    result = {"Buy&Hold": sum(panel[h["symbol"]] * h["quantity"] for h in usable)}
    if len(usable) >= 2:
        start_total = sum(h["quantity"] * panel[h["symbol"]].iloc[0] for h in usable)
        cap_each = start_total / len(usable)
        result["Gleichgewicht"] = sum(
            (cap_each / panel[h["symbol"]].iloc[0]) * panel[h["symbol"]] for h in usable
        )
    return result


def turnover_summary(scope: str) -> dict | None:
    """Anzahl/Volumen der Trades seit Experiment-Start plus Turnover-Quote
    (Volumen relativ zum durchschnittlichen Depotwert) - None ohne Experiment."""
    if not exists(scope):
        return None
    t = db.shadow_turnover(scope)
    snaps = db.list_shadow_snapshots(scope)
    info = start_info(scope) or {}
    values = [s["total_value_eur"] for s in snaps] or [info.get("shadow_start", 0.0)]
    avg_value = sum(values) / len(values) if values else 0.0
    t["turnover_ratio_pct"] = round(t["volume_eur"] / avg_value * 100, 1) if avg_value > 0 else None
    return t


def comparison_df(scope: str) -> pd.DataFrame | None:
    """Echtes vs. KI-Depot desselben Scopes, beide auf Experiment-Start = 100,
    plus Buy&Hold-/Gleichgewichts-Baseline (falls Startbestände hinterlegt sind).

    Der echte Zweig nutzt NUR die Snapshots der passenden Asset-Klasse
    (Krypto-KI vs. echtes Krypto-Depot). Es zählen nur Tage ab dem Startdatum.
    """
    info = start_info(scope)
    if not info:
        return None
    start = info["date"]

    real: dict[str, float] = {}
    for r in db.list_snapshots():
        if r["asset_type"] == scope and r["snap_date"] >= start:
            real[r["snap_date"]] = r["total_value_eur"]
    shadow_snaps = {s["snap_date"]: s["total_value_eur"]
                    for s in db.list_shadow_snapshots(scope) if s["snap_date"] >= start}
    if not shadow_snaps:
        return None

    dates = sorted(set(real) | set(shadow_snaps))
    df = pd.DataFrame([{"Datum": d, "Echt": real.get(d), "KI": shadow_snaps.get(d)}
                       for d in dates])
    df = df.set_index("Datum")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().ffill()

    for name, series in baseline_columns(scope, df.index).items():
        df[name] = series

    out = pd.DataFrame(index=df.index)
    for col in df.columns:
        series = df[col].dropna()
        if not series.empty:
            base = series.iloc[0]
            if base and base > 0:
                out[col] = df[col] / base * 100
    return out if not out.empty else None
