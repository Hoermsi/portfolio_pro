"""Krypto-Zyklus-Score: bewertet, wie weit BTC im Vergleich zu seiner EIGENEN
Historie in Richtung eines Zyklus-Extrems gelaufen ist - Grundlage für die
"Zyklus-Position"-Leiter im Krypto-Bereich (ergänzt analysis/market_timing.py,
ersetzt es nicht: dessen market_temperature() bleibt die schnelle, 6-Momentan-
werte-Markt-Temperatur; dieses Modul ist die langsamere, mehrjährige
Zyklus-Einordnung, die für eine Verkaufsleiter eigentlich gebraucht wird).

Jeder Baustein wird über sein PERZENTIL in der eigenen Historie bewertet statt
über feste Stützpunkte (wie market_timing._score_mayer()). Absolute Zyklus-
Extreme sind über die Jahre gefallen (2017 ≫ 2021 ≫ heute) - feste Schwellen
veralten still, Perzentile kalibrieren sich selbst an dem, was tatsächlich
passiert ist. Alle sieben Bausteine sind so definiert, dass ein HÖHERER Wert
IMMER stärkere Überhitzung bedeutet - ein einheitlicher aufsteigender
Perzentilrang reicht für alle, keine Baustein-spezifische Invertierung nötig.

Fehlt ein Baustein (Quelle nicht erreichbar, Historie zu kurz), wird er aus
der Gewichtung genommen und der Rest renormiert - exakt die Mechanik aus
market_temperature() (siehe dortiger Docstring).

Gewichtung in drei Gruppen (Preis/Trend 40% / On-Chain 40% / Sentiment 20%),
NICHT nach den sieben Einzelbausteinen für sich betrachtet: vier der sieben
(mayer, pi_cycle, drawdown_ath, two_year_mult) sind letztlich alle nur
Ableitungen desselben Preis-vs-eigener-Durchschnitt-Signals und damit
hochkorreliert, keine unabhängigen Informationsquellen. Eine frühere Version
gewichtete sie einzeln mit zusammen 55% (Preis 55 / On-Chain 35 / Sentiment
10) - das zählte dasselbe Preis-Momentum effektiv vierfach und überging den
On-Chain- und Sentiment-Anteil. Die drei Gruppen tragen jetzt vergleichbares
Gewicht; innerhalb jeder Gruppe bleibt die bisherige relative Gewichtung
erhalten (mvrv_z bleibt der stärkere On-Chain-Baustein, mayer der stärkere
Preis-Baustein).

Reine Funktionen auf fertigen Serien, bis auf die *_raw()-Helfer, die über
data/onchain.py, data/crypto_history.py, data/sentiment.py auf (gecachte)
externe Quellen zugreifen. Die Trennung erlaubt, das Scoring selbst ohne
Mocking zu testen (Muster: analysis/market_timing.py).
"""
from __future__ import annotations

import pandas as pd

from data import crypto_history
from data import onchain as onchain_data
from data import sentiment

# ~10 Jahre - deckt alle bisherigen BTC-Zyklen ab, die yfinance liefert
# (Kraken/CoinGecko-Kürze ist hier kein Thema, siehe crypto_history.py).
_HISTORY_DAYS = 3650

_WEIGHTS = {
    "mvrv_z": 29, "puell": 11,                                            # On-Chain: 40
    "mayer": 11, "pi_cycle": 11, "drawdown_ath": 11, "two_year_mult": 7,   # Preis/Trend: 40
    "fear_greed_30d": 20,                                                 # Sentiment: 20
}

_LABELS = {
    "mvrv_z": "MVRV Z-Score",
    "pi_cycle": "Pi-Cycle-Ratio (SMA111 / 2×SMA350)",
    "mayer": "Mayer Multiple (Kurs / SMA200)",
    "drawdown_ath": "Abstand zum Allzeithoch",
    "puell": "Puell Multiple",
    "two_year_mult": "2-Jahres-Multiple (Kurs / SMA730)",
    "fear_greed_30d": "Fear & Greed (30-Tage-Schnitt)",
}

# Preisabhängige Bausteine - einzige, die trigger_price() variieren kann
# (siehe dortiger Docstring).
_PRICE_DEPENDENT = {"mayer", "two_year_mult", "drawdown_ath"}

_MIN_COVERAGE_FOR_REGIME = 40.0


def _fmt(key: str, value: float) -> str:
    if key == "drawdown_ath":
        return f"{value:+.1f}%"
    if key == "fear_greed_30d":
        return f"{value:.0f}/100"
    if key == "pi_cycle":
        return f"{value:.3f}"
    return f"{value:.2f}"


def _percentile_of_last(series: pd.Series | None, min_points: int = 30) -> float | None:
    """Perzentilrang (0-100) des letzten Werts innerhalb der ganzen Serie.

    Kein Lookahead: für den LIVE-Score endet 'die ganze Serie' per Definition
    beim heutigen Wert - es gibt keine Zukunft, die hereinsickern könnte. Das
    ist ausschließlich im Backtest ein Thema (analysis/cycle_backtest.py), wo
    bei jedem historischen Tag t nur Daten bis t verwendet werden dürfen.
    """
    if series is None:
        return None
    s = series.dropna()
    if len(s) < min_points:
        return None
    last = float(s.iloc[-1])
    return float((s <= last).sum()) / len(s) * 100.0


def btc_price_series() -> pd.Series | None:
    """Lange BTC/EUR-Tagesreihe (data/crypto_history, mehrjährig plausibilisiert)."""
    return crypto_history.crypto_series_eur("BTC", days=_HISTORY_DAYS)


def _mayer_series(price: pd.Series) -> pd.Series:
    return (price / price.rolling(200).mean()).dropna()


def _two_year_series(price: pd.Series) -> pd.Series:
    return (price / price.rolling(730).mean()).dropna()


def _pi_cycle_series(price: pd.Series) -> pd.Series:
    sma111 = price.rolling(111).mean()
    sma350x2 = price.rolling(350).mean() * 2
    return (sma111 / sma350x2).dropna()


def _drawdown_series(price: pd.Series) -> pd.Series:
    """Abstand zum bisherigen Allzeithoch in % (0 = am ATH, negativ darunter).
    Perzentil-Score braucht keine Invertierung: der Maximalwert der Serie ist
    per Konstruktion 0 (am ATH), das ist bereits der überhitzteste Zustand."""
    running_max = price.cummax()
    return ((price / running_max) - 1.0) * 100.0


def _onchain_series(metric: str) -> pd.Series | None:
    onchain_data.latest(metric)  # stößt Bootstrap an / hält den heutigen Wert aktuell
    rows = onchain_data.history(metric)
    if not rows:
        return None
    s = pd.Series(
        [r["value"] for r in rows],
        index=pd.to_datetime([r["d"] for r in rows]),
    )
    return s.sort_index()


def _fear_greed_30d_series() -> pd.Series | None:
    reading = sentiment.fear_greed(days=_HISTORY_DAYS)
    if not reading or not reading.get("history"):
        return None
    hist = reading["history"]
    s = pd.Series(
        [h["value"] for h in hist],
        index=pd.to_datetime([h["date"] for h in hist]),
    ).sort_index()
    return s.rolling(30).mean().dropna()


def _weekly_trend_up(price: pd.Series) -> bool | None:
    """Wochenschluss über/unter dem eigenen 20-Wochen-Durchschnitt - der
    Trend-Baustein des Regime-Klassifikators (siehe classify_regime())."""
    weekly = price.resample("W").last().dropna()
    if len(weekly) < 25:
        return None
    sma20w = weekly.rolling(20).mean()
    last_sma = sma20w.iloc[-1]
    if pd.isna(last_sma):
        return None
    return float(weekly.iloc[-1]) > float(last_sma)


def classify_regime(score: float | None, trend_up: bool | None,
                    drawdown_pct: float | None, coverage_pct: float) -> tuple[str, str]:
    """Heuristische Zyklus-Phase aus Score + Trend + ATH-Abstand.

    Kein Backtest-Ergebnis, sondern eine dokumentierte Heuristik (wie
    market_timing.py._classify()) - der Sinn liegt darin, dass "idealer
    Verkaufszeitpunkt" in einem Bär-Regime eine andere Frage ist als in
    Euphorie, nicht darin, die Phase auf den Tag genau zu treffen.

    Rückgabe: (regime, begründung).
    """
    if score is None or trend_up is None or drawdown_pct is None:
        return "Unbekannt", "Zu wenig Datenlage für eine Zyklus-Einordnung."
    if coverage_pct < _MIN_COVERAGE_FOR_REGIME:
        return "Unbekannt", f"Nur {coverage_pct:.0f}% Indikator-Abdeckung - zu dünn für eine Phasen-Einordnung."

    deep = drawdown_pct <= -60.0
    if deep and not trend_up:
        return "Bär", f"{drawdown_pct:.0f}% unter ATH, Wochentrend abwärts."
    if deep and trend_up:
        return "Akkumulation", f"{drawdown_pct:.0f}% unter ATH, aber Wochentrend bereits aufwärts."
    if not trend_up and score >= 50:
        return "Distribution", "Wochentrend bricht bereits ab, Score noch erhöht - klassisches Topping-Muster."
    if not trend_up:
        return "Bär", "Wochenschluss unter dem 20-Wochen-Durchschnitt."
    if score < 35:
        return "Akkumulation", "Aufwärtstrend, aber Score noch niedrig - frühe Erholungsphase."
    if score < 60:
        return "Aufschwung", "Aufwärtstrend, Score im mittleren Bereich."
    if score < 80:
        return "Später Bulle", "Aufwärtstrend, Score bereits deutlich erhöht."
    return "Euphorie", "Aufwärtstrend und Score nahe der historischen Extreme."


def cycle_score() -> dict:
    """Gewichteter Zyklus-Score 0-100 aus allen verfügbaren Bausteinen, plus
    Regime-Einordnung. Rückgabeform bewusst analog zu
    market_timing.market_temperature(), damit UI-Code beide Formen gleich
    behandeln kann:

    {"score": float|None, "regime": str, "regime_reason": str,
     "breakdown": [{"key","label","score","value","text","weight_pct"}, ...],
     "unavailable": [...], "coverage_pct": float,
     "price_now": float|None, "drawdown_pct": float|None,
     "onchain_as_of": str|None}   # TT.MM.JJJJ, jüngstes Datum der On-Chain-Serie
    """
    price = btc_price_series()
    breakdown = []
    unavailable = []
    weighted_sum = 0.0
    weight_total = 0.0

    series_by_key: dict[str, pd.Series] = {}
    if price is not None and len(price.dropna()) >= 30:
        series_by_key["mayer"] = _mayer_series(price)
        series_by_key["two_year_mult"] = _two_year_series(price)
        series_by_key["pi_cycle"] = _pi_cycle_series(price)
        series_by_key["drawdown_ath"] = _drawdown_series(price)
    series_by_key["mvrv_z"] = _onchain_series("mvrv-zscore")
    series_by_key["puell"] = _onchain_series("puell-multiple")
    series_by_key["fear_greed_30d"] = _fear_greed_30d_series()

    for key, weight in _WEIGHTS.items():
        s = series_by_key.get(key)
        pct = _percentile_of_last(s)
        if pct is None:
            unavailable.append(key)
            continue
        raw_value = float(s.dropna().iloc[-1])
        breakdown.append({
            "key": key, "label": _LABELS[key], "score": round(pct, 1),
            "value": raw_value, "text": _fmt(key, raw_value), "weight_pct": weight,
        })
        weighted_sum += pct * weight
        weight_total += weight

    total_weight = sum(_WEIGHTS.values())
    coverage_pct = round(weight_total / total_weight * 100, 1) if total_weight else 0.0

    price_now = float(price.dropna().iloc[-1]) if price is not None and not price.dropna().empty else None
    drawdown_pct = None
    dd_series = series_by_key.get("drawdown_ath")
    if dd_series is not None and not dd_series.empty:
        drawdown_pct = float(dd_series.iloc[-1])

    # Jüngstes Datum der On-Chain-Serie (nicht dasselbe wie "heute" - die
    # bitcoin-data.com-Quelle wird nur einmal täglich nachgezogen, ttl_cache
    # in data/onchain.py). Für die "Datenstand"-Zeile in der UI, damit "soeben
    # abgerufen" hier nicht faelschlich einen taufrischen Wert suggeriert.
    mvrv_series = series_by_key.get("mvrv_z")
    onchain_as_of = (mvrv_series.index[-1].strftime("%d.%m.%Y")
                     if mvrv_series is not None and not mvrv_series.empty else None)

    if weight_total <= 0:
        return {
            "score": None, "regime": "Unbekannt",
            "regime_reason": "Keine Zyklus-Indikatoren verfügbar.",
            "breakdown": [], "unavailable": unavailable, "coverage_pct": 0.0,
            "price_now": price_now, "drawdown_pct": drawdown_pct, "onchain_as_of": onchain_as_of,
        }

    overall = weighted_sum / weight_total
    for row in breakdown:
        row["weight_pct"] = round(row["weight_pct"] / weight_total * 100, 1)

    trend_up = _weekly_trend_up(price) if price is not None else None
    regime, reason = classify_regime(overall, trend_up, drawdown_pct, coverage_pct)

    return {
        "score": round(overall, 1),
        "regime": regime,
        "regime_reason": reason,
        "breakdown": sorted(breakdown, key=lambda r: -r["weight_pct"]),
        "unavailable": unavailable,
        "coverage_pct": coverage_pct,
        "price_now": price_now,
        "drawdown_pct": drawdown_pct,
        "onchain_as_of": onchain_as_of,
    }


def trigger_price(threshold: float, cycle: dict | None = None) -> float | None:
    """Näherung: BTC-Preis, ab dem der Zyklus-Score `threshold` erreichen
    würde - unter Variation NUR der preisabhängigen Bausteine (Mayer,
    2-Jahres-Multiple, ATH-Abstand). On-Chain- (MVRV Z, Puell) und der
    Sentiment-Baustein hängen nicht direkt vom Spotpreis ab (MVRV/Puell
    brauchen Realized Cap/Hashrate, Fear&Greed ist ein eigener Index) und
    werden auf ihrem heutigen Stand eingefroren.

    Das macht den zurückgegebenen Preis eine Aussage der Form "ab BTC ≈ € X,
    WENN sich On-Chain- und Sentiment-Lage nicht wesentlich ändern" - keine
    Garantie, aber eine handelbare Größenordnung statt einer bloßen Punktzahl.

    None, wenn `threshold` selbst bei beliebig hohem Preis nicht erreichbar
    ist (zu viel Gewicht liegt auf eingefrorenen Bausteinen) oder die BTC-
    Historie zu kurz ist.
    """
    cycle = cycle or cycle_score()
    if cycle["score"] is None or cycle["price_now"] is None:
        return None

    price = btc_price_series()
    if price is None or len(price.dropna()) < 30:
        return None
    p = price.dropna()
    sma200 = float(p.rolling(200).mean().iloc[-1]) if len(p) >= 200 else None
    sma730 = float(p.rolling(730).mean().iloc[-1]) if len(p) >= 730 else None
    ath = float(p.cummax().iloc[-1])

    frozen = {row["key"]: row["score"] for row in cycle["breakdown"]
             if row["key"] not in _PRICE_DEPENDENT}
    weights = {row["key"]: row["weight_pct"] for row in cycle["breakdown"]}
    hist_by_key = {
        "mayer": _mayer_series(p) if sma200 else None,
        "two_year_mult": _two_year_series(p) if sma730 else None,
        "drawdown_ath": _drawdown_series(p),
    }

    def score_at(hypothetical_price: float) -> float:
        total = sum(frozen[k] * weights[k] for k in frozen if k in weights)
        weight_sum = sum(weights[k] for k in frozen if k in weights)
        for key in _PRICE_DEPENDENT:
            if key not in weights:
                continue
            hist = hist_by_key.get(key)
            if hist is None or hist.empty:
                continue
            if key == "mayer":
                val = hypothetical_price / sma200
            elif key == "two_year_mult":
                val = hypothetical_price / sma730
            else:  # drawdown_ath
                ref = max(ath, hypothetical_price)
                val = (hypothetical_price / ref - 1.0) * 100.0
            combined = pd.concat([hist, pd.Series([val])])
            pct = _percentile_of_last(combined, min_points=1)
            total += pct * weights[key]
            weight_sum += weights[key]
        return total / weight_sum if weight_sum else 0.0

    lo, hi = cycle["price_now"], cycle["price_now"]
    if score_at(hi) >= threshold:
        # Schon jetzt erreicht/überschritten - "ab wann" ist dann "jetzt".
        return cycle["price_now"]
    max_hi = cycle["price_now"] * 50  # großzügige Obergrenze für die Bisektion
    while score_at(hi) < threshold and hi < max_hi:
        hi *= 2
    if score_at(hi) < threshold:
        return None  # auch bei 50x Preis nicht erreichbar -> zu viel eingefrorenes Gewicht

    for _ in range(40):
        mid = (lo + hi) / 2
        if score_at(mid) < threshold:
            lo = mid
        else:
            hi = mid
    return round(hi, 2)
