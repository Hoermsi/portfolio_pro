"""Lange, plausibilisierte Tages-Kurshistorie in EUR - für beide Asset-Klassen,
aber vor allem für Krypto gedacht: CoinGecko liefert im freien Tarif nur ~365
Tage, Kraken-OHLC nur ~720 Kerzen. yfinance reicht für große Coins dagegen oft
viele Jahre zurück - diese Funktion kombiniert beide Wege.

Ursprünglich Teil von analysis/backtest.py (Bestands-Rückrechnung); hierher
gehoben, weil analysis/cycle.py (Krypto-Zyklus-Score) dieselbe lange BTC-Reihe
für Perzentil-Scoring und gleitende Mehrjahres-Durchschnitte (SMA200/350/730)
braucht. Ein Nebeneffekt: views/asset_detail.py fragt für die "5 Jahre"-Auswahl
der Einzelwertanalyse heute crypto_data.get_history(days=1825) an, was am
freien CoinGecko-Limit real nur ~365 Tage liefert - über diese Funktion ließe
sich das später beheben, ohne hier etwas ändern zu müssen.
"""
from __future__ import annotations

import pandas as pd

from core.cache import ttl_cache
from data import crypto as crypto_data
from data import fx as fx_data
from data import stocks as stock_data

# Toleranzband: aktueller Reihen-Schlusskurs vs. Live-Kurs. yfinance führt echte
# Altcoins teils unter Zahlen-Tickern (z.B. UNI7083-USD); "UNI-USD"/"UNI-EUR"
# treffen dann einen fremden Microcap - solche Kollisionen liegen um Größen-
# ordnungen daneben und werden über diesen Abgleich verworfen.
_SANITY_LOW, _SANITY_HIGH = 0.2, 5.0


def stock_period_for_days(days: int) -> str:
    """days -> gültiger yfinance-`period`-String, mit etwas Puffer nach oben."""
    if days > 1825:
        return "10y"
    if days > 1095:
        return "5y"
    if days > 365:
        return "3y"
    return "1y"


def normalize_close_series(df: pd.DataFrame | None, fx: float) -> pd.Series | None:
    """'Close'-Spalte -> bereinigte EUR-Series (Index auf Tagesdatum normalisiert,
    Duplikate pro Tag verworfen, aufsteigend sortiert)."""
    if df is None or "Close" not in df:
        return None
    close = df["Close"].dropna()
    if close.empty:
        return None
    close.index = pd.to_datetime(close.index).normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    return close * fx


def _yf_series_eur(ticker: str, days: int, fx: float) -> pd.Series | None:
    """yfinance-Tageskurse eines Tickers als EUR-Series (mit fx multipliziert)."""
    return normalize_close_series(stock_data.get_history(ticker, stock_period_for_days(days)), fx)


@ttl_cache(1800)
def crypto_series_eur(symbol: str, days: int) -> pd.Series | None:
    """Beste verfügbare, plausible Krypto-Historie in EUR aus mehreren Quellen.

    yfinance reicht für große Coins viele Jahre zurück (CoinGecko frei nur ~365
    Tage, Kraken-OHLC ~720). Quellen: EUR-Paar (nativ), USD-Paar × aktueller FX,
    CoinGecko/Kraken. Jede yfinance-Reihe wird gegen den Live-Kurs plausibilisiert
    (Schutz vor Yahoo-Ticker-Kollisionen); nur bestandene Reihen zählen. Gewählt
    wird die längste verbliebene. Ohne Live-Referenz wird die vertrauenswürdige
    CoinGecko/Kraken-Reihe bevorzugt. FX-Näherung: %-Rendite bleibt exakt (FX
    kürzt sich), €-Summen approximativ.
    """
    live = crypto_data.get_price_eur(symbol)
    cg = normalize_close_series(crypto_data.get_history(symbol, days=days), 1.0)

    def _plausible(s: pd.Series | None) -> bool:
        if s is None:
            return False
        if not live or live <= 0:
            return False  # ohne Referenz keine yfinance-Reihe blind vertrauen
        return _SANITY_LOW <= float(s.iloc[-1]) / live <= _SANITY_HIGH

    usd_fx = fx_data.get_fx_to_eur("USD") or None
    candidates: list[pd.Series] = []
    eur = _yf_series_eur(f"{symbol}-EUR", days, 1.0)
    if _plausible(eur):
        candidates.append(eur)
    if usd_fx:
        usd = _yf_series_eur(f"{symbol}-USD", days, usd_fx)
        if _plausible(usd):
            candidates.append(usd)
    if cg is not None:
        candidates.append(cg)  # CoinGecko/Kraken gilt als vertrauenswürdig

    if not candidates:
        return cg  # ohne Live-Referenz bleibt nur die (evtl. None) CoinGecko-Reihe
    # Längste Historie gewinnt; Reihenfolge bricht Gleichstände (EUR vor USD vor CG).
    return min(candidates, key=lambda s: s.index[0])
