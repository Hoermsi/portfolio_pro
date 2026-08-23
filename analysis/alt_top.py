"""Altcoin-Überhitzung: misst, wie weit der ALTCOIN-Markt gegenüber seiner
eigenen Historie gelaufen ist - unabhängig vom BTC-basierten Zyklus-Score
(analysis/cycle.py). KEIN Top-Timer: an den vier bekannten Zyklus-Extremen
(kalibriert gegen Top Nov 2021, Boden Nov 2022, Top März/Dez 2024 - Dez 2017
ausgenommen, siehe unten) trennt der Score Tops sauber vom Boden, aber der
historische Höchstwert der gesamten Serie liegt am 01.05.2021 - SECHS MONATE
VOR dem Nov-2021-Top, Alts stiegen danach noch ~50%. Bei Schwelle 75 folgten
auf Alarmtage im Schnitt SOGAR bessere Jahres-Renditen als der Durchschnitt
(kein Vorhersage-Edge: Spearman(Score, Rendite nächste 365 Tage) = -0.018).
Höhere Schwellen sind NICHT selektiver (Lift bei 85 nur noch 1.03x = Zufall) -
deshalb bewusst KEINE Verkaufsstufen-Leiter auf diesem Score, nur EIN
kalibrierter Alarm-Schwellenwert (_ALARM_THRESHOLD) als grobe Einordnung.

Methodik wie cycle.py: jeder Baustein wird über sein PERZENTIL in der eigenen
Historie bewertet statt über feste Stützpunkte, Renormierung bei fehlenden
Bausteinen, dieselbe Rückgabeform ({"score","regime","regime_reason",
"breakdown","unavailable","coverage_pct"}) - damit bestehender UI-Code beide
Formen gleich behandeln kann. Reine Funktionen auf fertigen Kursreihen, bis
auf basket_prices() (Netzzugriff über data/crypto_history).

Korb: 10 liquide Alt-Majors (nicht das eigene Depot - Survivorship-Bias und
über die Zeit instabil). UNI und MATIC bewusst ausgeschlossen: UNI trifft die
yfinance-Ticker-Kollision (fällt auf Krakens 720-Kerzen-Limit zurück, erst ab
09/2024 nutzbar), MATIC liefert nach der POL-Umbenennung keine Daten mehr.

Zwei ursprünglich erwogene Bausteine sind an echten Daten durchgefallen:
- ETH/BTC vs. eigenem 200-Tage-Schnitt: der Dez-2024-Top hatte den NIEDRIGSTEN
  gemessenen Wert alle Kalibrierungspunkte, "heute" (Referenzlauf 23.08.2026)
  den höchsten - ETH/BTC steckt in einem Mehrjahres-Abwärtstrend, "über dem
  eigenen Schnitt" heißt dort nur "Abwärtstrend pausiert", nicht Überhitzung.
- Korb-ATH-Abstand: die 2024er-Tops waren vom Median der gesamten Verteilung
  nicht unterscheidbar, weil die meisten Alts ihr 2021er-ATH nie wieder
  erreichten. Die naheliegende Reparatur (Abstand zum 365-Tage-Hoch) trennt
  zwar sauber, korreliert aber mit ~0.91 zum BTC-Zyklus-Score - das wäre
  cycle.drawdown_ath im Altcoin-Kostüm und verletzt die Unabhängigkeits-
  anforderung; der gemessene Genauigkeitsverlust ohne sie ist marginal.

Bewusst NICHT gebaut: ein "Rollover-Gate" (Score war >=75 und seither um
>=15 Punkte gefallen) sieht tagesgewichtet stark aus, ist aber episoden-
gewichtet nur 1 Treffer von 4 - ein Artefakt der einen langen 2021/22-Episode.
Nicht verlässlich genug für eine Anzeige.

Bekannte Grenzen: Kalibrierung ruht auf VIER Ereignissen - jede Aussage hier
ist eine Einschätzung, kein Backtest-Beweis (wie cycle.py). Korb ist heute
festgelegt und rückwirkend angewandt (Survivorship: DOT/ATOM waren 2021
stärker, NEAR hatte am Nov-2021-Top erst 393 Tage Historie - deshalb der
Mindesthistorie-Filter). Perzentile vor ca. 11/2020 sind durch die kurze
Vorlaufzeit der Rohserien (ab 08.11.2018) kontaminiert (Dez-2018 erreichte
88.7 - am Boden); jede Verlaufsdarstellung braucht einen sichtbaren Burn-in
von mindestens 730 Tagen. CoinGecko liefert unter Last häufig 429/401 und
alles fällt still auf Kraken/yfinance zurück - deshalb wird `basket_size` in
der UI sichtbar gemacht, damit ein geschrumpfter Korb auffällt, statt still
falsche Werte zu zeigen. `alt_breadth_btc` überschneidet sich konzeptionell
mit market_timing.py's `breadth` (Altcoin-Breite, Gewicht 15%) - der
Unterschied: hier 90-Tage-Fenster über einen festen Korb mit echter,
zurückrechenbarer Historie, dort ein CoinGecko-Top-N-Momentanwert ohne
Historie. Beide sind bewusst vorhanden, keins ist totes Duplikat.
"""
from __future__ import annotations

import pandas as pd

from analysis import cycle
from analysis.cycle_backtest import _expanding_pct
from data import crypto_history

_BASKET = ("ETH", "LINK", "ADA", "XRP", "LTC", "SOL", "AVAX", "DOT", "ATOM", "NEAR")
_HISTORY_DAYS = 3650
_MIN_COIN_HISTORY = 365   # era-aware: ein Coin zählt erst ab 1 Jahr eigener Historie mit
_MIN_BASKET = 3           # unter 3 Coins ist ein Tages-Querschnitt nicht aussagekräftig
_MIN_BASKET_FOR_SIGNAL = 6   # unter 6 HEUTE verfügbaren Coins kein Alarm, nur "limited" - siehe alt_top_score()
_MIN_POINTS = 30          # wie cycle._percentile_of_last()

_WEIGHTS = {
    "alt_extension": 35,     # Niveau: Median Kurs/SMA200 im Korb
    "alt_ext_spread": 25,    # Streuung: p90 - Median (Blow-off-Breite)
    "alt_breadth_btc": 22,   # Rotation: % des Korbs schlägt BTC über 90 Tage
    "alt_vs_btc_trend": 18,  # Rotation: Korb-vs-BTC-Verhältnis über eigenem Trend
}

_LABELS = {
    "alt_extension": "Alt-Ausdehnung (Kurs/200-Tage-Schnitt, Median)",
    "alt_ext_spread": "Streuung im Korb (Blow-off-Breite)",
    "alt_breadth_btc": "Alt-Breite vs. BTC (90 Tage)",
    "alt_vs_btc_trend": "Alt-Korb vs. BTC (Trend)",
}

_HORIZONS = {
    "alt_extension": "200-Tage-Ausdehnung",
    "alt_ext_spread": "Streuung im Korb",
    "alt_breadth_btc": "90-Tage-Fenster vs. BTC",
    "alt_vs_btc_trend": "Verhältnis zum 200-Tage-Durchschnitt",
}

_ALARM_THRESHOLD = 70.0   # einziger kalibrierter Parameter - siehe Moduldocstring
_MIN_COVERAGE_FOR_REGIME = 40.0


def _fmt(key: str, value: float) -> str:
    if key == "alt_breadth_btc":
        return f"{value:.0f}%"
    return f"{value:.2f}"


def basket_prices() -> dict[str, pd.Series]:
    """EUR-Tagesreihen für den Alt-Korb + BTC. STRIKT SEQUENTIELL (nicht
    threaden) - parallele yfinance-Abfragen unterschiedlicher Ticker liefern
    reproduzierbar korrupte Daten (siehe views/asset_detail._fetch_stock_readings).
    Kein eigener ttl_cache hier (dict-Rückgabe) - crypto_history.crypto_series_eur
    cached bereits eine Ebene tiefer (ttl_cache(1800))."""
    out: dict[str, pd.Series] = {}
    for symbol in (*_BASKET, "BTC"):
        s = crypto_history.crypto_series_eur(symbol, days=_HISTORY_DAYS)
        if s is not None and not s.dropna().empty:
            out[symbol] = s
    return out


def _align(series_by_symbol: dict[str, pd.Series]) -> tuple[pd.DataFrame, pd.Series | None]:
    """Alt-Korb als DataFrame (Vereinigungs-Index, kleine Lücken vorwärts
    gefüllt) + BTC-Serie separat. `ffill(limit=3)` deckt vereinzelte
    Datenlücken (z.B. AVAX) ab, ohne eine tagelange Fehlmeldung stillschweigend
    fortzuschreiben."""
    btc = series_by_symbol.get("BTC")
    alt_series = {s: v for s, v in series_by_symbol.items() if s != "BTC"}
    if not alt_series:
        return pd.DataFrame(), btc
    px = pd.DataFrame(alt_series).sort_index()
    px = px.ffill(limit=3)
    if btc is not None:
        btc = btc.reindex(btc.index.union(px.index)).sort_index().ffill(limit=3)
    return px, btc


def _eligible(px: pd.DataFrame) -> pd.DataFrame:
    """Era-aware Aufnahme (KUMULATIV): ein Coin zählt an einem Tag erst mit,
    wenn er zu diesem Zeitpunkt bereits >= _MIN_COIN_HISTORY Tage eigene
    Kurshistorie hat - verhindert, dass z.B. NEAR (393 Tage Historie am
    Nov-2021-Top) den Tages-Querschnitt mit einer kaum eingeschwungenen Reihe
    verzerrt.

    ACHTUNG (Code-Review-Fund): das ist eine kumulative Zählung, KEINE
    Momentaufnahme - ein Coin bleibt "eligible", auch wenn er an einem
    SPÄTEREN Tag (z.B. heute, wegen eines API-Ausfalls) keinen Kurs mehr
    liefert, solange die Historie DAVOR ausreichte. Für "wie viele Coins
    tragen HEUTE tatsächlich bei" siehe _available_today() - basket_size in
    alt_top_score() nutzt bewusst die UND-Verknüpfung aus beidem, sonst könnte
    die angezeigte Abdeckung optimistischer sein als der tatsächliche
    Tages-Querschnitt (der die fehlenden Werte über .median(skipna=True) zwar
    korrekt behandelt, aber das wäre in der Anzeige nicht sichtbar)."""
    return px.notna().cumsum() >= _MIN_COIN_HISTORY


def _available_today(px: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series:
    """Coins, die HEUTE (letzte Zeile) sowohl era-eligible sind als auch
    tatsächlich einen Kurs geliefert haben. Siehe Warnung in _eligible()."""
    return eligible.iloc[-1] & px.notna().iloc[-1]


def _alt_extension_series(px: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series:
    """Median von Kurs/eigenem 200-Tage-Schnitt über den (era-gated) Korb -
    der Alt-Analog zu cycle.mayer, aber als Tages-Querschnitt über mehrere
    Coins statt einer einzelnen BTC-Reihe."""
    ext = (px / px.rolling(200).mean()).where(eligible)
    n = eligible.sum(axis=1)
    return ext.median(axis=1, skipna=True).where(n >= _MIN_BASKET)


def _alt_ext_spread_series(px: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series:
    """p90 minus Median derselben Kurs/SMA200-Verteilung - wie breit die
    Spitze des Korbs gegenüber der Mitte ausschlägt (Blow-off-Signatur)."""
    ext = (px / px.rolling(200).mean()).where(eligible)
    n = eligible.sum(axis=1)
    spread = ext.quantile(0.9, axis=1) - ext.median(axis=1, skipna=True)
    return spread.where(n >= _MIN_BASKET)


def _alt_breadth_btc_series(px: pd.DataFrame, btc: pd.Series, eligible: pd.DataFrame) -> pd.Series:
    """% des Korbs, dessen 90-Tage-Rendite die von BTC übertrifft - reines
    Rotationssignal (Kapital wandert in immer spekulativere Ecken), separat
    von der Frage, WIE HOCH die Kurse selbst stehen."""
    ret90 = px / px.shift(90) - 1.0
    btc_ret90 = (btc / btc.shift(90) - 1.0).reindex(px.index)
    beats_btc = ret90.gt(btc_ret90, axis=0).astype(float).where(eligible)
    n = eligible.sum(axis=1)
    return (beats_btc.mean(axis=1, skipna=True) * 100.0).where(n >= _MIN_BASKET)


def _alt_vs_btc_trend_series(px: pd.DataFrame, btc: pd.Series, eligible: pd.DataFrame) -> pd.Series:
    """Je Coin: eigenes Kurs-zu-BTC-Verhältnis relativ zum EIGENEN 200-Tage-
    Schnitt dieses Verhältnisses (Muster: market_timing._rolling_ratio_pct,
    hier je Coin statt einem Symbolpaar) - dann Median über den Korb. Anders
    als das reine ETH/BTC-Verhältnis (verworfen, siehe Moduldocstring) driftet
    das nicht mit dem Mehrjahres-Abwärtstrend EINES Coins gegen BTC, weil jeder
    Coin gegen seinen EIGENEN Verlauf gemessen wird, nicht gegen ein festes Band."""
    rel = px.div(btc, axis=0)
    rel_ma = rel.rolling(200).mean()
    trend = (rel / rel_ma * 100.0).where(eligible)
    n = eligible.sum(axis=1)
    return trend.median(axis=1, skipna=True).where(n >= _MIN_BASKET)


def _raw_series(px: pd.DataFrame, btc: pd.Series, eligible: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "alt_extension": _alt_extension_series(px, eligible),
        "alt_ext_spread": _alt_ext_spread_series(px, eligible),
        "alt_breadth_btc": _alt_breadth_btc_series(px, btc, eligible),
        "alt_vs_btc_trend": _alt_vs_btc_trend_series(px, btc, eligible),
    }


def _classify_alt_regime(score: float | None, trend_up: bool | None, coverage_pct: float) -> tuple[str, str]:
    """Heuristische Einordnung aus der gemessenen Verteilung (Moduldocstring) -
    KEIN Backtest-Beweis. `trend_up`: eigener Alt-Extension-Median über/unter
    seinem 30-Perioden-Schnitt als grober Trend-Ersatz (der Korb hat kein
    Wochenschluss-Äquivalent wie eine Einzelreihe)."""
    if score is None:
        return "Unbekannt", "Zu wenig Datenlage für eine Alt-Überhitzungs-Einordnung."
    if coverage_pct < _MIN_COVERAGE_FOR_REGIME:
        return "Unbekannt", f"Nur {coverage_pct:.0f}% Bausteine verfügbar - zu dünn für eine Einordnung."
    if trend_up is False and score >= 60:
        return "Alt-Distribution", "Trend bricht bereits ab, Score noch erhöht - möglicher Topping-Übergang."
    if score >= _ALARM_THRESHOLD:
        return "Alt-Überhitzung", f"Score über der kalibrierten Schwelle ({_ALARM_THRESHOLD:.0f})."
    if score >= 50:
        return "Erhöht", "Alt-Markt merklich gelaufen, aber unter der Alarm-Schwelle."
    if score >= 30:
        return "Neutral", "Weder auffällig überhitzt noch ausverkauft."
    return "Ausverkauf", "Alt-Markt historisch günstig gegenüber der eigenen Spanne."


def alt_top_score() -> dict:
    """Altcoin-Überhitzungs-Score 0-100 aus 4 Bausteinen, Rückgabeform analog
    zu cycle.cycle_score() (siehe dortigen Vertrag), plus:
    - `basket_size`: Coins, die HEUTE tatsächlich beitragen (era-eligible UND
      mit aktuellem Kurs, siehe _available_today() - sinkt sowohl bei zu
      junger Historie als auch bei stillen Fetch-Ausfällen einzelner Coins).
    - `limited` (bool): True, wenn basket_size < _MIN_BASKET_FOR_SIGNAL (6) -
      die Querschnitts-Statistiken selbst bleiben korrekt (skipna), aber bei
      so wenigen Coins ist v.a. das 90.Perzentil (alt_ext_spread) faktisch nur
      Interpolation zwischen zwei Werten und nicht alarm-würdig belastbar.
    - `alarm` (bool, score >= _ALARM_THRESHOLD UND NICHT limited - reine
      Anzeigehilfe, kein Automatismus)."""
    prices = basket_prices()
    px, btc = _align(prices)

    if px.empty or btc is None or btc.dropna().empty:
        return {
            "score": None, "regime": "Unbekannt",
            "regime_reason": "Keine Altcoin-Kursdaten verfügbar.",
            "breakdown": [], "unavailable": list(_WEIGHTS), "coverage_pct": 0.0,
            "basket_size": 0, "limited": True, "alarm": False,
        }

    eligible = _eligible(px)
    basket_size = int(_available_today(px, eligible).sum())
    limited = basket_size < _MIN_BASKET_FOR_SIGNAL
    raw = _raw_series(px, btc, eligible)

    breakdown = []
    unavailable = []
    weighted_sum = 0.0
    weight_total = 0.0
    for key, weight in _WEIGHTS.items():
        s = raw.get(key)
        pct = cycle._percentile_of_last(s, min_points=_MIN_POINTS)
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

    if weight_total <= 0:
        return {
            "score": None, "regime": "Unbekannt",
            "regime_reason": "Keine Alt-Überhitzungs-Bausteine verfügbar.",
            "breakdown": [], "unavailable": unavailable, "coverage_pct": 0.0,
            "basket_size": basket_size, "limited": limited, "alarm": False,
        }

    overall = weighted_sum / weight_total
    for row in breakdown:
        row["weight_pct"] = round(row["weight_pct"] / weight_total * 100, 1)

    ext_series = raw.get("alt_extension")
    trend_up = None
    if ext_series is not None and len(ext_series.dropna()) >= 30:
        ext_ma = ext_series.rolling(30).mean()
        last_ext, last_ma = ext_series.iloc[-1], ext_ma.iloc[-1]
        if pd.notna(last_ext) and pd.notna(last_ma):
            trend_up = bool(last_ext > last_ma)

    regime, reason = _classify_alt_regime(round(overall, 1), trend_up, coverage_pct)

    return {
        "score": round(overall, 1),
        "regime": regime,
        "regime_reason": reason,
        "breakdown": sorted(breakdown, key=lambda r: -r["weight_pct"]),
        "unavailable": unavailable,
        "coverage_pct": coverage_pct,
        "basket_size": basket_size,
        "limited": limited,
        "alarm": overall >= _ALARM_THRESHOLD and not limited,
    }


def score_history() -> pd.Series:
    """Score-Verlauf für die Verlaufs-Chart-Anzeige, live geladen (Muster:
    analysis.cycle_backtest.score_history). Trifft im Normalfall auf bereits
    warme ttl_caches, da alt_top_score() denselben Korb im selben Seitenaufruf
    schon lädt. Erste 730 Tage sind kontaminiert (siehe Modul-Docstring) -
    Zuschneiden ist Aufgabe der Aufrufer/UI, nicht dieser Funktion."""
    px, btc = _align(basket_prices())
    if px.empty or btc is None or btc.dropna().empty:
        return pd.Series(dtype=float)
    return walk_forward_alt_score(px, btc)


def walk_forward_alt_score(px: pd.DataFrame, btc: pd.Series) -> pd.Series:
    """Tages-Score-Serie über die gesamte Korb-Historie, an jedem Tag ohne
    Zukunftswissen (analysis.cycle_backtest._expanding_pct - NICHT
    cycle_backtest.walk_forward_score wiederverwenden: die schlägt
    cycle._WEIGHTS nach und bricht bei diesen neuen Baustein-Keys)."""
    eligible = _eligible(px)
    raw = _raw_series(px, btc, eligible)
    pct = {k: _expanding_pct(v, min_points=_MIN_POINTS) for k, v in raw.items()}
    df = pd.DataFrame({k: v.reindex(px.index) for k, v in pct.items()})
    weights = pd.Series({k: _WEIGHTS[k] for k in df.columns})

    weight_total = df.notna().astype(float).mul(weights, axis=1).sum(axis=1)
    weighted_sum = df.mul(weights, axis=1).sum(axis=1)
    return weighted_sum / weight_total.where(weight_total > 0)
