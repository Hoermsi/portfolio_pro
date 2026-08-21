"""Krypto-Markt-Temperatur: bewertet die von data/sentiment.py gelieferten
Rohdaten zu einem Gesamtwert 0-100 (100 = maximale Gier/Überhitzung).

Rein rechnerisch, kein Netzzugriff - dadurch vollständig ohne Mocking testbar.
Die Gewichtung ist eine Heuristik, KEIN Backtest-Ergebnis (dafür fehlt eine
historische Sentiment-Datenbasis; core.db.sentiment_history baut sie ab jetzt
für Dominanz/Meme-Trends auf, die CoinGecko nur als Momentanwert liefert).

Fehlt ein Indikator (Quelle nicht erreichbar), wird er aus der Gewichtung
herausgenommen und die übrigen Gewichte werden renormiert - ein Ausfall darf
den Gesamtwert nicht systematisch nach unten ziehen.
"""
from __future__ import annotations

from analysis import technical
from data import stocks as stock_data

# Heuristische Gewichte (Summe 100) - siehe Modul-Docstring.
_WEIGHTS = {
    "fear_greed": 25,
    "mayer": 20,
    "breadth": 15,
    "btc_dominance": 15,
    "meme": 15,
    "stablecoin_dominance": 10,
}

_LABELS = {
    "fear_greed": "Fear & Greed Index",
    "mayer": "Mayer Multiple (BTC/MA200)",
    "breadth": "Altcoin-Breite (30 Tage vs. BTC)",
    "btc_dominance": "BTC-Dominanz",
    "meme": "Meme-Coin-Momentum (24h)",
    "stablecoin_dominance": "Stablecoin-Dominanz",
}


def mayer_multiple() -> float | None:
    """BTC-Kurs geteilt durch den gleitenden 200-Tage-Durchschnitt.

    >1 = Kurs über dem MA200 (Trend intakt/überhitzt je nach Höhe),
    <1 = Kurs unter dem MA200. Historisch: >2.4 selten und überhitzt,
    <0.8 selten und unterkühlt.
    """
    df = stock_data.get_history("BTC-EUR", "1y")
    if df is None or df.empty or "Close" not in df:
        return None
    tech = technical.summarize(df)
    ma200 = tech.get("ma200")
    if not ma200 or ma200 <= 0:
        return None
    return tech["kurs"] / ma200


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _lerp_score(value: float, lo: float, hi: float) -> float:
    """Lineare Interpolation value∈[lo,hi] -> Score∈[0,100], geklammert."""
    if hi == lo:
        return 50.0
    return _clamp((value - lo) / (hi - lo) * 100.0)


def _score_fear_greed(reading: dict | None) -> tuple[float, str] | None:
    if not reading:
        return None
    v = reading["value"]
    return float(v), f"{v}/100 ({reading.get('classification', '')})"


def _score_mayer(multiple: float | None) -> tuple[float, str] | None:
    if multiple is None:
        return None
    # Stützpunkte grob an historischen Zyklusextremen orientiert.
    if multiple <= 0.8:
        score = _lerp_score(multiple, 0.4, 0.8) * 0.2
    elif multiple <= 1.0:
        score = 20 + _lerp_score(multiple, 0.8, 1.0) * 0.2
    elif multiple <= 1.4:
        score = 40 + _lerp_score(multiple, 1.0, 1.4) * 0.2
    elif multiple <= 2.0:
        score = 60 + _lerp_score(multiple, 1.4, 2.0) * 0.2
    elif multiple <= 2.4:
        score = 80 + _lerp_score(multiple, 2.0, 2.4) * 0.2
    else:
        score = 100.0
    return score, f"{multiple:.2f}"


def _score_breadth(reading: dict | None) -> tuple[float, str] | None:
    if not reading:
        return None
    pct = reading["pct_outperforming"]
    text = (f"{pct:.0f}% der Top-{reading['sample_size']} Altcoins schlagen BTC "
            f"über 30 Tage (BTC {reading['btc_change_30d']:+.1f}%)")
    return float(pct), text


def _score_btc_dominance(reading: dict | None) -> tuple[float, str] | None:
    if not reading or reading.get("btc_dominance") is None:
        return None
    dom = reading["btc_dominance"]
    # Niedrige Dominanz = Kapital in Altcoins (Alt-Gier); hohe = BTC-Season.
    score = 100 - _lerp_score(dom, 40, 65)
    return score, f"{dom:.1f}%"


def _score_meme(reading: dict | None) -> tuple[float, str] | None:
    if not reading:
        return None
    change = reading["change_24h_pct"]
    score = _lerp_score(change, -10, 10)
    return score, f"{change:+.1f}% (24h), Marktkap. {reading['market_cap'] / 1e9:.1f} Mrd. $"


def _score_stablecoin_dominance(reading: dict | None) -> tuple[float, str] | None:
    if not reading or reading.get("stablecoin_dominance") is None:
        return None
    dom = reading["stablecoin_dominance"]
    # Hohe Stablecoin-Dominanz = Kapital in Cash geparkt (Angst); niedrige = Risk-on.
    score = 100 - _lerp_score(dom, 4, 10)
    return score, f"{dom:.1f}%"


_SCORERS = {
    "fear_greed": _score_fear_greed,
    "mayer": _score_mayer,
    "breadth": _score_breadth,
    "btc_dominance": _score_btc_dominance,
    "meme": _score_meme,
    "stablecoin_dominance": _score_stablecoin_dominance,
}


def _classify(score: float) -> str:
    if score < 20:
        return "Extreme Angst"
    if score < 40:
        return "Angst"
    if score < 60:
        return "Neutral"
    if score < 80:
        return "Gier"
    return "Extreme Gier"


def market_temperature(readings: dict) -> dict:
    """Gewichteter Gesamtwert aus allen verfügbaren Indikatoren.

    `readings` erwartet die Schlüssel fear_greed/mayer/breadth/btc_dominance/
    meme/stablecoin_dominance mit den jeweiligen Rückgaben aus data.sentiment
    (bzw. mayer_multiple() für "mayer"). Fehlende/None-Werte werden übersprungen.

    Rückgabe: {"score": float|None, "classification": str, "breakdown": [...],
    "unavailable": [...]} - "score" ist None, wenn gar keine Quelle verfügbar war.
    """
    breakdown = []
    unavailable = []
    weighted_sum = 0.0
    weight_total = 0.0

    for key, weight in _WEIGHTS.items():
        result = _SCORERS[key](readings.get(key))
        if result is None:
            unavailable.append(key)
            continue
        score, text = result
        breakdown.append({"key": key, "label": _LABELS[key], "score": round(score, 1),
                          "text": text, "weight_pct": weight})
        weighted_sum += score * weight
        weight_total += weight

    if weight_total <= 0:
        return {"score": None, "classification": "", "breakdown": [], "unavailable": unavailable}

    overall = weighted_sum / weight_total
    # Renormierte Gewichte für die Anzeige (Summe der verfügbaren = 100%).
    for row in breakdown:
        row["weight_pct"] = round(row["weight_pct"] / weight_total * 100, 1)

    return {
        "score": round(overall, 1),
        "classification": _classify(overall),
        "breakdown": sorted(breakdown, key=lambda r: -r["weight_pct"]),
        "unavailable": unavailable,
    }
