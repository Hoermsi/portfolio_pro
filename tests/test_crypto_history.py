"""Tests für data/crypto_history.py (Kursquellen gemockt).

Verschoben aus tests/test_backtest.py, als die zugrundeliegende Logik
(_crypto_series_eur) nach data/crypto_history.py gehoben wurde - jetzt auch
von analysis/cycle.py genutzt (lange BTC-Historie fürs Zyklus-Scoring).
"""
from datetime import date, timedelta

import pandas as pd

from data import crypto as crypto_data
from data import crypto_history
from data import fx as fx_data
from data import stocks as stock_data


def _ago(days):
    return date.today() - timedelta(days=days)


def _close_df(start, ndays, price=100.0):
    """Minimaler yfinance/CoinGecko-artiger DataFrame mit 'Close' ab start."""
    idx = pd.date_range(start, periods=ndays, freq="D")
    return pd.DataFrame({"Close": [price] * ndays}, index=idx)


def _reset_cache():
    crypto_history.crypto_series_eur.cache_clear()


def test_crypto_prefers_eur_pair(monkeypatch):
    """Plausibles EUR-Paar (nativ, lange Historie) wird bevorzugt."""
    _reset_cache()

    def _yf(ticker, *a, **k):
        return _close_df(_ago(1100), 1000, price=50.0) if ticker.endswith("-EUR") else None
    monkeypatch.setattr(stock_data, "get_history", _yf)
    monkeypatch.setattr(crypto_data, "get_history",
                        lambda *a, **k: _close_df(_ago(300), 300, price=50.0))
    monkeypatch.setattr(crypto_data, "get_price_eur", lambda s: 50.0)
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda c: 0.9)

    s = crypto_history.crypto_series_eur("BTC", days=1200)
    assert s is not None
    assert s.index[0].date() <= _ago(1000)      # lange Historie
    assert float(s.iloc[0]) == 50.0             # EUR-nativ, kein FX-Faktor


def test_crypto_uses_usd_when_no_eur(monkeypatch):
    """Ohne EUR-Paar wird das plausible USD-Paar × aktueller FX genutzt."""
    _reset_cache()

    def _yf(ticker, *a, **k):
        return _close_df(_ago(700), 650, price=100.0) if ticker.endswith("-USD") else None
    monkeypatch.setattr(stock_data, "get_history", _yf)
    monkeypatch.setattr(crypto_data, "get_history", lambda *a, **k: None)
    monkeypatch.setattr(crypto_data, "get_price_eur", lambda s: 90.0)  # ≈ 100 × 0.9
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda c: 0.9)

    s = crypto_history.crypto_series_eur("UNI", days=800)
    assert s is not None
    assert float(s.iloc[0]) == 90.0             # 100 × 0.9


def test_crypto_rejects_yahoo_collision(monkeypatch):
    """Ein yfinance-Paar, dessen Kurs weit vom Live-Kurs abweicht (Ticker-
    Kollision), wird verworfen - trotz längerer Historie gewinnt CoinGecko."""
    _reset_cache()

    def _yf(ticker, *a, **k):
        # Kollisions-Microcap: winziger Kurs, aber lange Historie
        return _close_df(_ago(1500), 1400, price=0.0003) if ticker.endswith("-USD") else None
    monkeypatch.setattr(stock_data, "get_history", _yf)
    monkeypatch.setattr(crypto_data, "get_history",
                        lambda *a, **k: _close_df(_ago(300), 300, price=5.0))
    monkeypatch.setattr(crypto_data, "get_price_eur", lambda s: 5.0)  # echter Live-Kurs
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda c: 1.0)

    s = crypto_history.crypto_series_eur("UNI", days=1600)
    assert float(s.iloc[0]) == 5.0              # CoinGecko, nicht die Kollision


def test_crypto_falls_back_to_coingecko(monkeypatch):
    """Kein yfinance-Paar -> CoinGecko/Kraken (auch ohne Live-Referenz vertraut)."""
    _reset_cache()
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: None)
    monkeypatch.setattr(crypto_data, "get_history",
                        lambda *a, **k: _close_df(_ago(300), 300, price=42.0))
    monkeypatch.setattr(crypto_data, "get_price_eur", lambda s: None)
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda c: 0.9)

    s = crypto_history.crypto_series_eur("OBSCURE", days=400)
    assert s is not None
    assert float(s.iloc[0]) == 42.0


def test_crypto_longest_plausible_wins(monkeypatch):
    """Reicht CoinGecko weiter zurück als das kurze EUR-Paar, gewinnt CoinGecko."""
    _reset_cache()

    def _yf(ticker, *a, **k):
        return _close_df(_ago(200), 200, price=10.0) if ticker.endswith("-EUR") else None
    monkeypatch.setattr(stock_data, "get_history", _yf)
    monkeypatch.setattr(crypto_data, "get_history",
                        lambda *a, **k: _close_df(_ago(800), 800, price=10.0))
    monkeypatch.setattr(crypto_data, "get_price_eur", lambda s: 10.0)
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda c: 0.9)

    s = crypto_history.crypto_series_eur("ETH", days=900)
    assert s.index[0].date() <= _ago(800)       # CoinGecko-Reihe (länger)
