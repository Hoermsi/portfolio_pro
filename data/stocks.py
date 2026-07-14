"""Aktien-Daten über yfinance: Kurse, Historie, Fundamentaldaten."""
import numpy as np
import pandas as pd
import yfinance as yf

from core.cache import ttl_cache


@ttl_cache(300)
def get_history(symbol: str, period: str = "1y") -> pd.DataFrame | None:
    """Tages-Historie mit Close/High/Low/Volume. None wenn nicht auflösbar."""
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"stocks.get_history({symbol}): {e}")
        return None


@ttl_cache(3600)
def get_currency(symbol: str) -> str:
    try:
        fi = yf.Ticker(symbol).fast_info
        cur = fi.get("currency") if hasattr(fi, "get") else fi["currency"]
        return (cur or "EUR").upper()
    except Exception:
        return "EUR"


@ttl_cache(120)
def get_quote(symbol: str) -> dict | None:
    """Aktueller Kurs + Währung."""
    df = get_history(symbol, "5d")
    if df is None or df.empty:
        return None
    close = df["Close"].dropna()
    if close.empty:
        return None
    return {"price": float(close.iloc[-1]), "currency": get_currency(symbol)}


@ttl_cache(3600)
def get_fundamentals(symbol: str) -> dict:
    """Fundamentaldaten für den KI-Kontext (best effort)."""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        return {}
    def _num(key):
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) and not (isinstance(v, float) and np.isnan(v)) else None
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sektor": info.get("sector"),
        "branche": info.get("industry"),
        "kgv": _num("trailingPE"),
        "kgv_forward": _num("forwardPE"),
        "marktkap": _num("marketCap"),
        "dividendenrendite": _num("dividendYield"),
        "gewinnmarge": _num("profitMargins"),
        "umsatzwachstum": _num("revenueGrowth"),
        "verschuldung_eigenkapital": _num("debtToEquity"),
        "beta": _num("beta"),
        "52w_hoch": _num("fiftyTwoWeekHigh"),
        "52w_tief": _num("fiftyTwoWeekLow"),
        "analysten_kursziel": _num("targetMeanPrice"),
        "analysten_empfehlung": info.get("recommendationKey"),
    }


@ttl_cache(3600)
def resolves(symbol: str) -> bool:
    """Schneller Check, ob yfinance für das Symbol Kursdaten liefert."""
    return get_history(symbol, "5d") is not None
