"""Ausstiegs-Rangliste innerhalb des Krypto-Depots: welche Positionen sollen
zuerst reduziert werden, wenn die Zyklus-Leiter (analysis/cycle.py) eine
Verkaufsstufe auslöst.

Der Zyklus-Score ist BTC-basiert - für ein reines Alt-Depot beantwortet er nur
"wie viel Risiko soll insgesamt runter", nicht "welcher Coin zuerst". Diese
Frage beantwortet dieses Modul separat: viele Alts haben kein verlässliches
Mean-Reversion-Recht (viele 2021er-Top-Coins haben ihr ATH nie wieder
gesehen) - "unter dem eigenen Schnitt = billig" ist hier deshalb KEIN
Kaufsignal wie in analysis/technical.py, sondern im Gegenteil ein Hinweis,
zuerst zu verkaufen: Kapital soll sich in den relativ stärksten Namen
konzentrieren, nicht gleichmäßig über alle Positionen verteilt bleiben.

Alle Marktkennzahlen kommen aus EINEM crypto.get_market_data_batch()-Request
plus einem parallelen RSI-Abruf (Muster: views/asset_detail.
_render_position_details) - nie eine CoinGecko-Anfrage pro Coin (sprengt das
freie Rate-Limit, siehe data/crypto.py-Docstring).

exit_score ist eine QUERSCHNITTS-Rangliste (0-100 je Baustein durch Min-Max-
Normierung INNERHALB des aktuellen Bestands) - anders als analysis/cycle.py
(Perzentil in der EIGENEN Zeit-Historie eines einzelnen Assets über Jahre)
gibt es hier keine sinnvoll lange Historie für Positionen mit teils sehr
kurzer Notierung (z.B. ONDO, JUP). Ein Vergleich der Positionen UNTEREINANDER,
JETZT, ist die robustere Frage - und genau die, die beim Auswählen "wer zuerst"
gestellt wird.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from analysis import alerts
from core import db
from data import crypto as crypto_data

_WEIGHTS = {
    "rel_strength_btc": 35,
    "ath_drawdown": 20,
    "liquidity_rank": 20,
    "momentum_7d": 15,
    "rsi": 10,
}

_LABELS = {
    "rel_strength_btc": "Relative Stärke vs. BTC (30 Tage)",
    "ath_drawdown": "Abstand zum eigenen Allzeithoch",
    "liquidity_rank": "Marktkap.-Rang (Liquidität)",
    "momentum_7d": "Kursverlauf (7 Tage)",
    "rsi": "RSI (14)",
}


def held_symbols() -> dict[str, float]:
    """Symbol -> aggregierte Menge über alle Kategorien (Muster:
    analysis/backtest._agg_quantities), ohne die EUR-Cash-Zeile."""
    agg: dict[str, float] = {}
    for p in db.list_positions("crypto"):
        if p.symbol == "EUR" or p.quantity <= 0:
            continue
        agg[p.symbol] = agg.get(p.symbol, 0.0) + p.quantity
    return agg


def _rsi_batch(symbols: list[str]) -> dict[str, float | None]:
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {s: pool.submit(alerts.asset_metrics, s, "crypto") for s in symbols}
        return {s: fut.result().get("rsi") for s, fut in futures.items()}


def _minmax_score(raw: dict[str, float | None]) -> dict[str, float | None]:
    """Min-Max-Normierung 0-100 INNERHALB der übergebenen Werte (höherer
    Rohwert -> höherer Score; die Rohwerte werden beim Aufruf bereits so
    orientiert, dass "höher = mehr Verkaufspriorität" gilt). Weniger als zwei
    Werte oder alle gleich -> neutral 50 statt Division durch 0."""
    vals = [v for v in raw.values() if v is not None]
    if len(vals) < 2 or max(vals) == min(vals):
        return {k: (50.0 if v is not None else None) for k, v in raw.items()}
    lo, hi = min(vals), max(vals)
    return {k: (None if v is None else (v - lo) / (hi - lo) * 100.0) for k, v in raw.items()}


def exit_ranking(positions: dict[str, float] | None = None) -> list[dict]:
    """Ausstiegs-Rangliste, absteigend nach exit_score (100 = zuerst
    verkaufen). Leer bei leerem Bestand oder wenn gar keine Marktdaten kommen.

    Rückgabe je Position: {"symbol", "name", "quantity", "price_eur",
    "value_eur", "exit_score", "coverage_pct", "breakdown": [...]}.
    """
    positions = positions if positions is not None else held_symbols()
    symbols = sorted(positions)
    if not symbols:
        return []

    batch = crypto_data.get_market_data_batch(symbols + ["BTC"])
    btc_30d = (batch.get("BTC") or {}).get("aenderung_30d_pct")
    rsi_by_symbol = _rsi_batch(symbols)

    raw: dict[str, dict[str, float | None]] = {
        "rel_strength_btc": {}, "ath_drawdown": {}, "liquidity_rank": {},
        "momentum_7d": {}, "rsi": {},
    }
    for s in symbols:
        row = batch.get(s) or {}
        coin_30d = row.get("aenderung_30d_pct")
        raw["rel_strength_btc"][s] = (
            -(coin_30d - btc_30d) if coin_30d is not None and btc_30d is not None else None
        )
        ath = row.get("ath_abstand_pct")
        raw["ath_drawdown"][s] = -ath if ath is not None else None  # ath_abstand_pct ist negativ
        rang = row.get("rang")
        raw["liquidity_rank"][s] = float(rang) if rang is not None else None  # höherer Rang = illiquider
        d7 = row.get("aenderung_7d_pct")
        raw["momentum_7d"][s] = -d7 if d7 is not None else None
        raw["rsi"][s] = rsi_by_symbol.get(s)

    scored = {
        "rel_strength_btc": _minmax_score(raw["rel_strength_btc"]),
        "ath_drawdown": _minmax_score(raw["ath_drawdown"]),
        "liquidity_rank": _minmax_score(raw["liquidity_rank"]),
        "momentum_7d": _minmax_score(raw["momentum_7d"]),
        "rsi": raw["rsi"],  # RSI ist bereits 0-100, keine Normierung nötig
    }

    total_weight = sum(_WEIGHTS.values())
    results = []
    for s in symbols:
        row = batch.get(s) or {}
        price = row.get("kurs_eur")
        weighted_sum = 0.0
        weight_total = 0.0
        breakdown = []
        for key, weight in _WEIGHTS.items():
            val = scored[key].get(s)
            if val is None:
                continue
            breakdown.append({"key": key, "label": _LABELS[key], "score": round(val, 1), "weight_pct": weight})
            weighted_sum += val * weight
            weight_total += weight
        exit_score = round(weighted_sum / weight_total, 1) if weight_total else None
        qty = positions[s]
        results.append({
            "symbol": s,
            "name": row.get("name") or s,
            "quantity": qty,
            "price_eur": price,
            "value_eur": (qty * price) if price else None,
            "exit_score": exit_score,
            "coverage_pct": round(weight_total / total_weight * 100, 1) if weight_total else 0.0,
            "breakdown": sorted(breakdown, key=lambda r: -r["weight_pct"]),
        })

    # Fehlender Score (keine Marktdaten) landet ans Ende, nicht an der Spitze.
    results.sort(key=lambda r: r["exit_score"] if r["exit_score"] is not None else -1, reverse=True)
    return results


def sell_list(target_pct: float, positions: dict[str, float] | None = None,
              ranking: list[dict] | None = None,
              basis: dict[str, float] | None = None,
              partial_last: bool = False) -> dict:
    """Greedy-Verkaufsliste: verkauft ganze Positionen (schwächste nach
    exit_ranking() zuerst), bis der kumulierte Wert das Ziel erreicht oder
    übersteigt. Konzentriert den Rest bewusst in den stärksten Namen, statt
    jede Position anteilig anzuschneiden - wenige Trades statt vieler kleiner.

    OHNE `basis`: `target_pct`% vom aktuellen Gesamtwert - das bisherige
    Verhalten, automatisch der Fall bei der ERSTEN Verkaufsstufe eines Zyklus
    (vor der je eine Basis gesetzt wurde).

    MIT `basis` (Symbol->Menge, der beim ersten Erreichen von Stufe 1
    eingefrorene Ausgangsbestand des Zyklus, core.profile.cycle_progress()):
    das Ziel ist `target_pct`% des Ausgangsbestands ZU HEUTIGEN PREISEN
    (kein historischer Snapshot-Preis - hält den Prozentsatz konsistent: "%
    dessen, was der ursprüngliche Bestand JETZT wert ist"), abzüglich dessen,
    was seit Zyklusbeginn bereits verkauft wurde (Mengendifferenz Basis vs.
    aktueller Bestand, zu heutigen Preisen bewertet). Behebt den Fehler, dass
    Stufe 2 sonst 66% des DANN aktuellen (schon reduzierten) Depots verkauft
    hätte statt der verbleibenden 33 Prozentpunkte des Zyklus-Ausgangswerts.

    `partial_last=True`: überschießt die letzte hinzugefügte Position übers
    Ziel hinaus, wird sie anteilig gekürzt, statt das Ziel zu überschreiten
    (Default False - erhält das "ganze Positionen"-Verhalten).

    {"target_pct", "total_value_eur", "sell_value_eur", "sell_pct_actual",
     "basis_value_eur", "already_sold_value_eur", "basis_price_unavailable",
     "to_sell": [...], "keep": [...]}
    """
    ranking = ranking if ranking is not None else exit_ranking(positions)
    valued = [r for r in ranking if r["value_eur"]]
    total_value = sum(r["value_eur"] for r in valued)

    basis_value_eur = None
    already_sold_value_eur = 0.0
    basis_price_unavailable: list[str] = []
    target_value = total_value * target_pct / 100.0

    if basis:
        price_by_symbol = {r["symbol"]: r["price_eur"] for r in ranking if r["price_eur"]}
        current_qty = {r["symbol"]: r["quantity"] for r in ranking}
        missing = [s for s in basis if s not in price_by_symbol]
        if missing:
            extra = crypto_data.get_market_data_batch(missing)
            for s in missing:
                p = (extra.get(s) or {}).get("kurs_eur")
                if p:
                    price_by_symbol[s] = p
                else:
                    basis_price_unavailable.append(s)

        computed_basis_value = 0.0
        computed_sold_value = 0.0
        for sym, basis_qty in basis.items():
            price = price_by_symbol.get(sym)
            if not price:
                continue
            computed_basis_value += basis_qty * price
            sold_qty = max(0.0, basis_qty - current_qty.get(sym, 0.0))
            computed_sold_value += sold_qty * price

        if computed_basis_value > 0:
            basis_value_eur = computed_basis_value
            already_sold_value_eur = computed_sold_value
            target_value = max(0.0, basis_value_eur * target_pct / 100.0 - already_sold_value_eur)
        # sonst: keine Basis-Position bewertbar -> Rückfall auf "ohne Basis"-Ziel oben

    if total_value <= 0:
        return {"target_pct": target_pct, "total_value_eur": 0.0, "sell_value_eur": 0.0,
               "sell_pct_actual": 0.0, "basis_value_eur": basis_value_eur,
               "already_sold_value_eur": already_sold_value_eur,
               "basis_price_unavailable": basis_price_unavailable,
               "to_sell": [], "keep": ranking}

    to_sell = []
    running = 0.0
    for r in valued:  # bereits exit_score-absteigend sortiert (schwächste zuerst)
        if running >= target_value:
            break
        to_sell.append(dict(r))
        running += r["value_eur"]

    if partial_last and to_sell and running > target_value > 0:
        last = to_sell[-1]
        prev_running = running - last["value_eur"]
        needed = target_value - prev_running
        if 0 < needed < last["value_eur"]:
            frac = needed / last["value_eur"]
            last["quantity"] = last["quantity"] * frac
            last["value_eur"] = needed
            running = prev_running + needed

    sold_symbols = {r["symbol"] for r in to_sell}
    keep = [r for r in ranking if r["symbol"] not in sold_symbols]

    if basis_value_eur:
        sell_pct_actual = round((already_sold_value_eur + running) / basis_value_eur * 100, 1)
    else:
        sell_pct_actual = round(running / total_value * 100, 1) if total_value else 0.0

    return {
        "target_pct": target_pct,
        "total_value_eur": round(total_value, 2),
        "sell_value_eur": round(running, 2),
        "sell_pct_actual": sell_pct_actual,
        "basis_value_eur": round(basis_value_eur, 2) if basis_value_eur else None,
        "already_sold_value_eur": round(already_sold_value_eur, 2) if basis_value_eur else None,
        "basis_price_unavailable": basis_price_unavailable,
        "to_sell": to_sell,
        "keep": keep,
    }
