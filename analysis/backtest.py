"""Bestands-Rückrechnung: "Was wäre meine Rendite, wenn ich meine heutigen
Bestände am Tag X gekauft hätte?"

Nimmt die aktuellen Stückzahlen (über alle Kategorien pro Symbol aggregiert) und
bewertet sie mit dem historischen Kurs am gewählten Kaufdatum gegen den heutigen
Kurs. Das ist bewusst KEIN regelbasierter Backtest: es werden keine Kauf-/Verkaufs-
entscheidungen über die Zeit simuliert, es gibt kein Rebalancing und keine
Survivorship-Bias-Behandlung - lediglich die heutigen Bestände werden rückwirkend
bewertet. %-Rendite ist fx-unabhängig (gleicher Umrechnungskurs für beide
Zeitpunkte); €-Summen nutzen den aktuellen fx (Näherung wie bei der Kraken-
Einstands-Logik). Krypto-Historie ist begrenzt (~365 Tage CoinGecko / ~720 Tage
Kraken) - Symbole ohne Kurs am Tag X werden übersprungen und gemeldet.

Die lange, plausibilisierte Krypto-Kurshistorie (yfinance-Fallback über CoinGecko
hinaus) lebt in data/crypto_history.py - auch analysis/cycle.py (Zyklus-Score)
braucht sie für BTC.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from core import db, shadow
from data import crypto_history
from data import fx as fx_data
from data import stocks as stock_data

# EUR ist als Position eine Cash-Zeile (Krypto) - nie als Asset backtesten.
_SKIP_SYMBOLS = {"EUR"}


def _asset_types(scope: str) -> list[str]:
    if scope == "stock":
        return ["stock"]
    if scope == "crypto":
        return ["crypto"]
    return ["stock", "crypto"]


def _agg_quantities(asset_type: str) -> tuple[dict[str, float], dict[str, str]]:
    """Mengen pro Symbol über alle Kategorien summieren (ein Symbol kann in
    mehreren Konten liegen). Namen best effort mitnehmen."""
    agg: dict[str, float] = {}
    names: dict[str, str] = {}
    for p in db.list_positions(asset_type):
        if p.symbol in _SKIP_SYMBOLS:
            continue
        agg[p.symbol] = agg.get(p.symbol, 0.0) + p.quantity
        if p.name:
            names[p.symbol] = p.name
    return agg, names


def _series_eur(symbol: str, asset_type: str, days: int) -> pd.Series | None:
    """Tages-Schlusskurse in EUR als Series (Index auf Tagesdatum normalisiert)."""
    if asset_type == "crypto":
        return crypto_history.crypto_series_eur(symbol, days)
    fx = fx_data.get_fx_to_eur(stock_data.get_currency(symbol)) or 1.0
    return crypto_history.normalize_close_series(
        stock_data.get_history(symbol, crypto_history.stock_period_for_days(days)), fx
    )


def _build_curve(series_map: dict[str, tuple[float, pd.Series]],
                 start_ts: pd.Timestamp) -> pd.Series | None:
    """Gesamter Depotwert je Tag ab start_ts (Σ menge × Kurs), Reihen zusammengeführt.

    `bfill()` nach dem `ffill()`: handelt eine Position erst später zum ersten
    Mal (z.B. Aktien fehlen am Wochenende, während Krypto schon Kurse hat),
    bliebe ihre Spalte am Anfang sonst NaN -> zählt in der Summe als 0 und die
    Kurve startet künstlich zu tief, statt beim tatsächlich investierten Betrag.
    """
    parts = []
    for symbol, (qty, s) in series_map.items():
        s2 = s[s.index >= start_ts]
        if s2.empty:
            continue
        parts.append((qty * s2).rename(symbol))
    if not parts:
        return None
    combined = pd.concat(parts, axis=1).sort_index().ffill().bfill()
    return combined.sum(axis=1)


def run_backtest(scope: str, start_date: date) -> dict:
    """Backtest für scope ∈ {'total','stock','crypto'} ab start_date.

    Rückgabe: rows (je Symbol), skipped, Summen (Investiert/Wert/Rendite/Gewinn)
    und curve (Wertverlaufsreihe oder None)."""
    start_ts = pd.Timestamp(start_date)
    days = (date.today() - start_date).days + 5
    rows: list[dict] = []
    skipped: list[str] = []
    series_map: dict[str, tuple[float, pd.Series]] = {}

    for at in _asset_types(scope):
        agg, names = _agg_quantities(at)
        for symbol, qty in agg.items():
            if qty <= 0:
                continue
            s = _series_eur(symbol, at, days)
            if s is None or s.empty:
                skipped.append(symbol)
                continue
            price_x = s.asof(start_ts)
            if price_x is None or pd.isna(price_x) or float(price_x) <= 0:
                skipped.append(symbol)
                continue
            price_x = float(price_x)
            # Echter Live-Kurs statt letzter Punkt der historischen Reihe (die
            # ggf. das USD-Paar x FX oder ein älterer CoinGecko-Wert ist) -
            # damit "Wert heute" mit dem Dashboard/den echten Positionen übereinstimmt.
            live = shadow.price_eur(symbol, at)
            price_now = live if live is not None else float(s.iloc[-1])
            invest = qty * price_x
            value = qty * price_now
            rows.append({
                "Symbol": symbol,
                "Name": names.get(symbol, ""),
                "Kauf-Kurs (€)": price_x,
                "Kurs heute (€)": price_now,
                "Investiert (€)": invest,
                "Wert heute (€)": value,
                "Rendite (%)": (value / invest - 1.0) * 100.0,
            })
            series_map[symbol] = (qty, s)

    total_invest = sum(r["Investiert (€)"] for r in rows)
    total_value = sum(r["Wert heute (€)"] for r in rows)
    total_return = (total_value / total_invest - 1.0) * 100.0 if total_invest else None
    return {
        "rows": rows,
        "skipped": skipped,
        "total_invest": total_invest,
        "total_value": total_value,
        "gain_eur": total_value - total_invest,
        "total_return_pct": total_return,
        "curve": _build_curve(series_map, start_ts),
        "start_date": start_date,
    }
