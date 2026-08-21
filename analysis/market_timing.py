"""Markt-Temperatur: bewertet die von data/sentiment.py bzw. den *_ratio()-
Helfern hier gelieferten Rohdaten zu einem Gesamtwert 0-100 (100 = maximale
Gier/Überhitzung) - getrennt für Krypto und Aktien (Parameter `market`).

Die Scorer-Funktionen selbst sind rein (kein Netzzugriff, dadurch ohne Mocking
testbar); die `*_ratio()`/`mayer_multiple()`-Rohwert-Helfer holen dagegen über
die gecachte Datenschicht (data.stocks) Kurse.

Die Gewichtung ist eine Heuristik, KEIN Backtest-Ergebnis (dafür fehlt eine
historische Sentiment-Datenbasis; core.db.sentiment_history baut sie ab jetzt
je Markt getrennt auf - für Krypto-Dominanz/Meme-Trends, die CoinGecko nur als
Momentanwert liefert, ist das der einzige Weg zu einer eigenen Historie. Die
Aktien-Indikatoren brauchen das nicht: ihre Trends stecken bereits vollständig
in der langen yfinance-Historie gegen den jeweils eigenen MA200).

Hinweis Krypto- vs. Aktien-Breite: beide flaggen "Spätzyklus", aber mit
GEGENSÄTZLICHER Mechanik - kein Vorzeichenfehler. Hohe Altcoin-Breite (viele
Coins schlagen BTC) ist Spätzyklus-GIER (Kapital rotiert in immer spekulativere
Ecken). Schmale Aktien-Marktbreite (nur noch wenige Mega-Caps tragen den Index)
ist dagegen ein Spätzyklus-WARNSIGNAL (die Rally steht auf immer weniger
Schultern) - deshalb ist der Aktien-Breite-Score invertiert, der Krypto-Score nicht.

Fehlt ein Indikator (Quelle nicht erreichbar), wird er aus der Gewichtung
herausgenommen und die übrigen Gewichte werden renormiert - ein Ausfall darf
den Gesamtwert nicht systematisch nach unten ziehen.
"""
from __future__ import annotations

from analysis import technical
from data import stocks as stock_data


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _lerp_score(value: float, lo: float, hi: float) -> float:
    """Lineare Interpolation value∈[lo,hi] -> Score∈[0,100], geklammert."""
    if hi == lo:
        return 50.0
    return _clamp((value - lo) / (hi - lo) * 100.0)


def _ma200_ratio(symbol: str) -> float | None:
    """Aktueller Kurs / gleitender 200-Tage-Durchschnitt, für symbol über
    stock_data (yfinance) - Basis für die meisten Aktien-Rohwerte.

    Defensiv (try/except): yfinance liefert unter Nebenläufigkeit gelegentlich
    strukturell kaputte Daten (z.B. vertauschte/verdoppelte Spalten), die
    technical.summarize() mit einer TypeError/ValueError statt None quittiert -
    ein Ausfall dieser einen Quelle darf die Markt-Temperatur nicht crashen.
    """
    try:
        df = stock_data.get_history(symbol, "1y")
        if df is None or df.empty or "Close" not in df:
            return None
        tech = technical.summarize(df)
        ma200 = tech.get("ma200")
        if not ma200 or ma200 <= 0:
            return None
        return tech["kurs"] / ma200
    except Exception as e:
        print(f"market_timing._ma200_ratio({symbol}): {e}")
        return None


def mayer_multiple() -> float | None:
    """BTC-Kurs geteilt durch den gleitenden 200-Tage-Durchschnitt.

    >1 = Kurs über dem MA200 (Trend intakt/überhitzt je nach Höhe),
    <1 = Kurs unter dem MA200. Historisch: >2.4 selten und überhitzt,
    <0.8 selten und unterkühlt.
    """
    return _ma200_ratio("BTC-EUR")


def sp500_ma200_ratio() -> float | None:
    """S&P 500 (^GSPC) / eigener MA200 - Aktien-Gegenstück zum Mayer Multiple.
    Läuft nie so weit über den Trend wie Krypto, daher engeres Scoring-Band."""
    return _ma200_ratio("^GSPC")


def vix_level() -> float | None:
    """Aktueller CBOE-VIX-Stand - Aktien-Gegenstück zu Fear & Greed.
    Niedrig = Sorglosigkeit/Gier, hoch = Panik (daher invers gescored)."""
    try:
        df = stock_data.get_history("^VIX", "3mo")
        if df is None or df.empty or "Close" not in df:
            return None
        close = df["Close"].dropna()
        return float(close.iloc[-1]) if not close.empty else None
    except Exception as e:
        print(f"market_timing.vix_level(): {e}")
        return None


def _rolling_ratio_pct(sym_a: str, sym_b: str) -> float | None:
    """(Kurs sym_a / Kurs sym_b), relativ zum eigenen 200-Tage-Durchschnitt
    dieses Verhältnisses, in % - gemeinsame Basis für breadth_ratio() und
    risk_appetite_ratio(). Defensiv: siehe _ma200_ratio()."""
    try:
        a = stock_data.get_history(sym_a, "1y")
        b = stock_data.get_history(sym_b, "1y")
        if a is None or b is None or a.empty or b.empty:
            return None
        ratio = (a["Close"] / b["Close"]).dropna()
        if len(ratio) < 200:
            return None
        ma200 = ratio.rolling(200).mean().iloc[-1]
        if not ma200 or ma200 <= 0:
            return None
        return float(ratio.iloc[-1] / ma200 * 100)
    except Exception as e:
        print(f"market_timing._rolling_ratio_pct({sym_a}/{sym_b}): {e}")
        return None


def breadth_ratio() -> float | None:
    """RSP (gleichgewichteter S&P) / SPY (kapitalgewichtet), relativ zum
    eigenen MA200 des Verhältnisses, in %. Fällt es unter 100, tragen nur noch
    wenige Mega-Caps den Index - historisch ein Spätzyklus-Warnsignal."""
    return _rolling_ratio_pct("RSP", "SPY")


def risk_appetite_ratio() -> float | None:
    """HYG (Hochzins-Anleihen) / IEF (sichere Staatsanleihen), relativ zum
    eigenen MA200, in %. Über 100 = Risk-on (Anleger greifen ins Risiko),
    darunter = Flucht in Sicherheit. Aktien-Gegenstück zur Stablecoin-Dominanz."""
    return _rolling_ratio_pct("HYG", "IEF")


# --- Scorer: Rohwert -> (Score 0-100, Klartext) ---

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


def _score_vix(vix: float | None) -> tuple[float, str] | None:
    if vix is None:
        return None
    # Invers: niedriger VIX = Sorglosigkeit/Gier, hoher VIX = Panik/Angst.
    score = 100 - _lerp_score(vix, 12, 40)
    return score, f"{vix:.1f}"


def _score_sp500_ma200(ratio: float | None) -> tuple[float, str] | None:
    if ratio is None:
        return None
    # Engeres Band als Mayer: Aktienindizes laufen nie so weit über den Trend wie Krypto.
    score = _lerp_score(ratio, 0.85, 1.20)
    return score, f"{ratio:.2f}"


def _score_stock_breadth(ratio_pct: float | None) -> tuple[float, str] | None:
    if ratio_pct is None:
        return None
    # Invers: RSP/SPY unter seinem MA200 = schmale Fuehrung = Spaetzyklus-Warnung.
    score = 100 - _lerp_score(ratio_pct, 97, 103)
    return score, f"{ratio_pct:.1f}% vom Trend"


def _score_risk_appetite(ratio_pct: float | None) -> tuple[float, str] | None:
    if ratio_pct is None:
        return None
    score = _lerp_score(ratio_pct, 97, 103)
    return score, f"{ratio_pct:.1f}% vom Trend"


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


_MARKETS = {
    "crypto": {
        "weights": {
            "fear_greed": 25, "mayer": 20, "breadth": 15,
            "btc_dominance": 15, "meme": 15, "stablecoin_dominance": 10,
        },
        "labels": {
            "fear_greed": "Fear & Greed Index",
            "mayer": "Mayer Multiple (BTC/MA200)",
            "breadth": "Altcoin-Breite (30 Tage vs. BTC)",
            "btc_dominance": "BTC-Dominanz",
            "meme": "Meme-Coin-Momentum (24h)",
            "stablecoin_dominance": "Stablecoin-Dominanz",
        },
        "scorers": {
            "fear_greed": _score_fear_greed, "mayer": _score_mayer,
            "breadth": _score_breadth, "btc_dominance": _score_btc_dominance,
            "meme": _score_meme, "stablecoin_dominance": _score_stablecoin_dominance,
        },
    },
    "stock": {
        "weights": {"vix": 30, "sp500_ma200": 30, "breadth": 20, "risk_appetite": 20},
        "labels": {
            "vix": "VIX (Angstbarometer)",
            "sp500_ma200": "S&P 500 / MA200",
            "breadth": "Marktbreite (RSP/SPY)",
            "risk_appetite": "Risikoappetit (HYG/IEF)",
        },
        "scorers": {
            "vix": _score_vix, "sp500_ma200": _score_sp500_ma200,
            "breadth": _score_stock_breadth, "risk_appetite": _score_risk_appetite,
        },
    },
}


def labels_for(market: str) -> dict[str, str]:
    """Anzeigenamen der Indikatoren des gewählten Marktes (für UI-Texte, z.B.
    die Auflistung nicht verfügbarer Indikatoren)."""
    return _MARKETS[market]["labels"]


def market_temperature(readings: dict, market: str = "crypto") -> dict:
    """Gewichteter Gesamtwert aus allen verfügbaren Indikatoren des gewählten Marktes.

    `market="crypto"`: readings erwartet fear_greed/mayer/breadth/btc_dominance/
    meme/stablecoin_dominance (Rückgaben aus data.sentiment bzw. mayer_multiple()).
    `market="stock"`: readings erwartet vix/sp500_ma200/breadth/risk_appetite
    (Rückgaben aus vix_level()/sp500_ma200_ratio()/breadth_ratio()/risk_appetite_ratio()).
    Fehlende/None-Werte werden übersprungen.

    Rückgabe: {"score": float|None, "classification": str, "breakdown": [...],
    "unavailable": [...]} - "score" ist None, wenn gar keine Quelle verfügbar war.
    """
    cfg = _MARKETS[market]
    breakdown = []
    unavailable = []
    weighted_sum = 0.0
    weight_total = 0.0

    for key, weight in cfg["weights"].items():
        result = cfg["scorers"][key](readings.get(key))
        if result is None:
            unavailable.append(key)
            continue
        score, text = result
        breakdown.append({"key": key, "label": cfg["labels"][key], "score": round(score, 1),
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
