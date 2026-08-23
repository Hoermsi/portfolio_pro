"""Walk-Forward-Backtest der Zyklus-Verkaufsleiter (analysis/cycle.py): "Wie
gut hätte diese Regel historisch tatsächlich getroffen?" - ohne diese Zahl
wäre der Rest des Features nur hübsche Chartkunst statt einer geprüften Regel.

KEIN Lookahead: cycle.py bewertet jeden Baustein über sein Perzentil in der
kompletten (heute bekannten) Historie - für einen Rücktest wäre das Zukunfts-
wissen (der Score am 1.1.2018 dürfte nichts vom Kurs im Jahr 2024 wissen).
Hier wird deshalb an JEDEM historischen Tag t nur mit Daten bis einschließlich
t gescort: pandas .expanding().rank(pct=True, method="max") ist das exakte
vektorisierte Äquivalent zu cycle._percentile_of_last() an jedem Punkt der
Serie statt nur am letzten (verifiziert: beide Definitionen - "Anteil der
Werte <= x" - stimmen für method="max" exakt überein).

Simuliert nur die VERKAUFS-Seite: eine anfangs voll gehaltene BTC-Position,
die den Sperrklinken-Verkaufsstufen folgt (core.profile.advance_cycle_tier -
einmal erreichte Stufen fallen nie zurück), mit 0,25% Handelskosten je Stufe
(core/shadow.py-Konvention). Kein simulierter Wiedereinstieg: die gestellte
Frage war "idealer VERKAUFSzeitpunkt", nicht Rebalancing - Wiedereinstiegs-
Timing ist eine eigene, mindestens ebenso unsichere Frage.

Vergleichsgrößen: die Leiter vs. Buy&Hold vs. ein hypothetischer perfekter
Verkauf exakt am historischen Allzeithoch (kostenfrei) - sowie, wie weit jeder
einzelne Verkauf unter dem jeweils NACHFOLGENDEN Hoch lag ("hat die Regel zu
früh verkauft").
"""
from __future__ import annotations

import pandas as pd

from analysis import cycle, market_timing
from core import profile

_MIN_POINTS = 30


def _expanding_pct(series: pd.Series, min_points: int = _MIN_POINTS) -> pd.Series:
    """Perzentilrang (0-100) JEDES Punkts einer Serie relativ zu allen
    VORANGEGANGENEN Punkten (inklusive sich selbst) - das vektorisierte
    Äquivalent zu cycle._percentile_of_last(), aber für die ganze Historie
    statt nur den letzten Wert. Vor `min_points` Datenpunkten: NaN (Baustein
    an diesem Tag "nicht verfügbar", wie ein zu kurzer Datensatz in cycle.py)."""
    return series.dropna().expanding(min_periods=min_points).rank(pct=True, method="max") * 100.0


def walk_forward_score(price: pd.Series, mvrv: pd.Series | None = None,
                       puell: pd.Series | None = None,
                       fear_greed_30d: pd.Series | None = None) -> pd.Series:
    """Tages-Score-Serie über die gesamte Preis-Historie, an jedem Tag ohne
    Zukunftswissen berechnet (siehe Modul-Docstring). Fehlt ein Baustein an
    einem Tag (z.B. On-Chain-Historie beginnt später als die Preisreihe),
    wird er für diesen Tag aus der Gewichtung genommen und der Rest
    renormiert - dieselbe Coverage-Mechanik wie cycle.cycle_score()."""
    raw: dict[str, pd.Series] = {
        "mayer": cycle._mayer_series(price),
        "two_year_mult": cycle._two_year_series(price),
        "pi_cycle": cycle._pi_cycle_series(price),
        "drawdown_ath": cycle._drawdown_series(price),
    }
    if mvrv is not None:
        raw["mvrv_z"] = mvrv
    if puell is not None:
        raw["puell"] = puell
    if fear_greed_30d is not None:
        raw["fear_greed_30d"] = fear_greed_30d

    pct = {k: _expanding_pct(v) for k, v in raw.items()}
    df = pd.DataFrame({k: v.reindex(price.index) for k, v in pct.items()})
    weights = pd.Series({k: cycle._WEIGHTS[k] for k in df.columns})

    weight_total = df.notna().astype(float).mul(weights, axis=1).sum(axis=1)
    weighted_sum = df.mul(weights, axis=1).sum(axis=1)  # sum() ignoriert NaN (skipna=True, Default)
    score = weighted_sum / weight_total.where(weight_total > 0)
    return score


def simulate_sell_ladder(price: pd.Series, score: pd.Series, sell_thresholds: list[float],
                         fractions: tuple[float, ...] = profile.LADDER_FRACTIONS_PCT,
                         trade_cost_pct: float = 0.25) -> dict:
    """Simuliert eine anfangs voll gehaltene BTC-Position (1 Einheit) entlang
    der Sperrklinken-Verkaufsstufen. Rückgabe: Trades + Endzustand."""
    idx = price.index.intersection(score.dropna().index).sort_values()

    original_units = 1.0
    btc_units = 1.0
    cash_eur = 0.0
    sell_tier_done = 0
    trades: list[dict] = []

    for t in idx:
        s = float(score.loc[t])
        p = float(price.loc[t])
        if p <= 0:
            continue
        reached = market_timing.active_ladder_tier(s, sell_thresholds, "sell")
        if reached > sell_tier_done:
            target_sold_frac = fractions[reached - 1] / 100.0
            already_sold_frac = fractions[sell_tier_done - 1] / 100.0 if sell_tier_done else 0.0
            delta = max(0.0, target_sold_frac - already_sold_frac)
            units_to_sell = original_units * delta
            proceeds = units_to_sell * p * (1 - trade_cost_pct / 100.0)
            cash_eur += proceeds
            btc_units -= units_to_sell
            trades.append({
                "date": t.strftime("%Y-%m-%d"), "tier": reached, "score": round(s, 1),
                "price_eur": p, "units": units_to_sell, "proceeds_eur": proceeds,
            })
            sell_tier_done = reached

    if len(idx) == 0:
        return {"trades": [], "final_btc_units": original_units, "final_cash_eur": 0.0,
               "final_value_eur": None, "sell_tier_reached": 0}

    final_price = float(price.loc[idx[-1]])
    return {
        "trades": trades,
        "final_btc_units": btc_units,
        "final_cash_eur": cash_eur,
        "final_value_eur": btc_units * final_price + cash_eur,
        "sell_tier_reached": sell_tier_done,
    }


def _trades_vs_future_peak(trades: list[dict], price: pd.Series, lookforward_days: int) -> dict:
    """Für jeden Verkauf: wie weit lag der Verkaufskurs unter dem höchsten
    Kurs der folgenden `lookforward_days` Tage (oder bis zum Datenende)? -
    die ehrliche Antwort auf "hat die Regel zu früh verkauft". Ein Wert nahe
    0% heißt: kurz vorm/am lokalen Hoch verkauft. Ein hoher Wert heißt: es
    ging danach noch deutlich weiter nach oben."""
    if not trades:
        return {"avg_miss_pct": None, "worst_miss_pct": None, "per_trade": []}
    misses = []
    per_trade = []
    for tr in trades:
        t = pd.Timestamp(tr["date"])
        window = price.loc[t: t + pd.Timedelta(days=lookforward_days)]
        if window.empty or not tr.get("price_eur"):
            continue
        future_peak = float(window.max())
        miss_pct = (future_peak / tr["price_eur"] - 1.0) * 100.0
        misses.append(miss_pct)
        per_trade.append({**tr, "future_peak_eur": future_peak, "miss_vs_peak_pct": round(miss_pct, 1)})
    if not misses:
        return {"avg_miss_pct": None, "worst_miss_pct": None, "per_trade": per_trade}
    return {
        "avg_miss_pct": round(sum(misses) / len(misses), 1),
        "worst_miss_pct": round(max(misses), 1),
        "per_trade": per_trade,
    }


def score_history(price: pd.Series | None = None, mvrv: pd.Series | None = None,
                  puell: pd.Series | None = None,
                  fear_greed_30d: pd.Series | None = None) -> pd.Series:
    """Score-Verlauf für die Verlaufs-Chart-Anzeige - dieselben Default-Fetches
    wie run_backtest() (live laden, falls nicht injiziert), aber ohne dessen
    Trade-Simulation. Trifft im Normalfall auf bereits warme ttl_caches, da
    cycle.cycle_score() dieselben Quellen im selben Seitenaufruf schon lädt."""
    price = price if price is not None else cycle.btc_price_series()
    if price is None or price.dropna().empty:
        return pd.Series(dtype=float)
    mvrv = mvrv if mvrv is not None else cycle._onchain_series("mvrv-zscore")
    puell = puell if puell is not None else cycle._onchain_series("puell-multiple")
    fear_greed_30d = fear_greed_30d if fear_greed_30d is not None else cycle._fear_greed_30d_series()
    return walk_forward_score(price.dropna(), mvrv, puell, fear_greed_30d)


def run_backtest(price: pd.Series | None = None, mvrv: pd.Series | None = None,
                 puell: pd.Series | None = None, fear_greed_30d: pd.Series | None = None,
                 sell_thresholds: list[float] | None = None,
                 trade_cost_pct: float = 0.25, lookforward_days: int = 400,
                 burn_in_days: int = 365) -> dict:
    """Kompletter Walk-Forward-Backtest. Alle Serien-Parameter sind für Tests
    injizierbar - ohne Angabe werden sie live geladen (BTC-Historie,
    On-Chain-/Sentiment-Historie, aktuelle Ladder-Konfiguration).

    `burn_in_days`: Handelssperre für die ersten N Tage der Preis-Historie
    (Default 1 Jahr). NICHT dasselbe wie _MIN_POINTS (30) in _expanding_pct:
    30 Punkte reichen, damit ein Baustein technisch "verfügbar" ist, aber in
    einer derart kleinen Stichprobe kippt das Perzentil bei ein paar zufälligen
    Aufwärtstagen sofort auf 100 - beobachtet an echten Daten: die BTC-Serie
    ab Aug. 2016 löste ohne Burn-in binnen 3 Wochen (!) eine volle Verkaufs-
    stufe bei ~555 EUR aus und ruinierte damit den gesamten 10-Jahres-Rücktest
    (Endwert 554 EUR statt uebersichtlicher Buy&Hold-Werte). Die Perzentil-
    Berechnung selbst läuft unverändert ab Tag 30 (walk_forward_score bleibt
    unangetastet) - nur das SIMULIEREN von Trades wird um `burn_in_days`
    verzögert, indem die Score-Werte davor auf NaN gesetzt werden (simulate_
    sell_ladder ignoriert NaN-Tage ohnehin über score.dropna()).

    {"available": bool, ...} - "available": False mit "reason", wenn die
    BTC-Historie zu kurz für einen sinnvollen Rücktest ist.
    """
    price = price if price is not None else cycle.btc_price_series()
    if price is None or len(price.dropna()) < _MIN_POINTS * 2:
        return {"available": False, "reason": "Zu wenig BTC-Historie für einen Backtest."}
    price = price.dropna()

    if mvrv is None:
        mvrv = cycle._onchain_series("mvrv-zscore")
    if puell is None:
        puell = cycle._onchain_series("puell-multiple")
    if fear_greed_30d is None:
        fear_greed_30d = cycle._fear_greed_30d_series()
    if sell_thresholds is None:
        sell_thresholds = profile.ladder_config("crypto")["sell"]

    score = walk_forward_score(price, mvrv, puell, fear_greed_30d)
    cutoff = price.index[0] + pd.Timedelta(days=burn_in_days)
    tradeable_score = score.where(score.index >= cutoff)

    sim = simulate_sell_ladder(price, tradeable_score, sell_thresholds, trade_cost_pct=trade_cost_pct)
    if sim["final_value_eur"] is None:
        return {"available": False, "reason": "Score-Serie liefert keine bewertbaren Tage."}

    buy_hold_value = float(price.iloc[-1])   # 1 BTC-Einheit, nie verkauft
    perfect_top_value = float(price.max())   # 1 BTC-Einheit exakt am historischen Hoch, kostenfrei
    gaps = _trades_vs_future_peak(sim["trades"], price, lookforward_days)

    return {
        "available": True,
        "sell_thresholds": sell_thresholds,
        "trade_cost_pct": trade_cost_pct,
        "start_date": price.index[0].strftime("%Y-%m-%d"),
        "end_date": price.index[-1].strftime("%Y-%m-%d"),
        "ladder_value_eur": sim["final_value_eur"],
        "buy_hold_value_eur": buy_hold_value,
        "perfect_top_value_eur": perfect_top_value,
        "ladder_vs_buy_hold_pct": (sim["final_value_eur"] / buy_hold_value - 1.0) * 100.0
                                  if buy_hold_value else None,
        "ladder_vs_perfect_pct": (sim["final_value_eur"] / perfect_top_value - 1.0) * 100.0
                                 if perfect_top_value else None,
        "trades": gaps["per_trade"] or sim["trades"],
        "trade_count": len(sim["trades"]),
        "sell_tier_reached": sim["sell_tier_reached"],
        "avg_miss_vs_next_peak_pct": gaps["avg_miss_pct"],
        "worst_miss_vs_next_peak_pct": gaps["worst_miss_pct"],
    }
