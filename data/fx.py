"""Währungsumrechnung nach EUR über yfinance.

yfinance liefert unter Last/Nebenläufigkeit gelegentlich einen völlig falschen
Wert (z.B. den Kurs eines anderen Tickers). Ein FX-Kurs nach EUR liegt für alle
gängigen Fiat-Währungen in einem engen Band - Ausreißer werden verworfen und
durch den zuletzt bekannten guten Kurs ersetzt, damit eine Position nicht
plötzlich um Faktor 200 fehlbewertet wird.
"""
import threading
import time

import pandas as pd
import yfinance as yf

# Plausibles Band für "1 Einheit Fremdwährung in EUR":
# USD~0.9, GBP~1.15, CHF~1.05, JPY~0.006, GBp~0.009 ... nichts liegt über ~2.
_FX_MIN, _FX_MAX = 1e-5, 5.0
_TTL = 3600.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, float]] = {}   # currency -> (monotonic_ts, rate)
_last_good: dict[str, float] = {}             # currency -> zuletzt plausibler Kurs


def _fetch(pair: str) -> float | None:
    try:
        data = yf.download(pair, period="5d", interval="1d", progress=False, auto_adjust=True)
        if data is None or data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return float(data["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"fx._fetch({pair}): {e}")
        return None


def get_fx_to_eur(currency: str) -> float | None:
    """Wechselkurs currency -> EUR. EUR -> 1.0. Unplausible Werte werden verworfen
    (Fallback: zuletzt bekannter guter Kurs, sonst None)."""
    if not currency or currency.upper() == "EUR":
        return 1.0
    # yfinance meldet GB-Aktien oft in Pence (GBp)
    if currency == "GBp":
        gbp = get_fx_to_eur("GBP")
        return gbp / 100 if gbp else None

    cur = currency.upper()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(cur)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    rate = _fetch(f"{cur}EUR=X")
    if rate is not None and _FX_MIN < rate < _FX_MAX:
        with _lock:
            _cache[cur] = (now, rate)
            _last_good[cur] = rate
        return rate

    # Fehlgeschlagen oder unplausibel -> nicht (lange) cachen, letzten guten Wert nutzen
    if rate is not None:
        print(f"fx.get_fx_to_eur({cur}): unplausibler Kurs {rate} verworfen")
    with _lock:
        return _last_good.get(cur)
