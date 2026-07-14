from datetime import datetime, timezone

import pandas as pd

from data import kraken


def _ts(date_str: str) -> float:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def test_daily_balances_buy_then_sell():
    # BTC: am 01. gekauft (0.5), am 03. komplett verkauft (0.0)
    entries = [
        {"time": _ts("2026-01-01"), "asset": "XXBT", "balance": "0.5"},
        {"time": _ts("2026-01-03"), "asset": "XXBT", "balance": "0.0"},
    ]
    df = kraken.daily_balances(entries)
    assert "BTC" in df.columns
    # 01. + 02. = 0.5 (ffill), ab 03. = 0.0
    assert df.loc["2026-01-01", "BTC"] == 0.5
    assert df.loc["2026-01-02", "BTC"] == 0.5
    assert df.loc["2026-01-03", "BTC"] == 0.0


def test_daily_balances_staking_variant_summed():
    # XETH und ETH2.S werden beide auf ETH summiert
    entries = [
        {"time": _ts("2026-02-01"), "asset": "XETH", "balance": "1.0"},
        {"time": _ts("2026-02-01"), "asset": "ETH2.S", "balance": "0.5"},
    ]
    df = kraken.daily_balances(entries)
    assert abs(df.loc["2026-02-01", "ETH"] - 1.5) < 1e-9


def test_daily_balances_eur_cash_column():
    entries = [
        {"time": _ts("2026-03-01"), "asset": "ZEUR", "balance": "1000.0"},
        {"time": _ts("2026-03-01"), "asset": "EUR.HOLD", "balance": "50.0"},
    ]
    df = kraken.daily_balances(entries)
    assert abs(df.loc["2026-03-01", "EUR"] - 1050.0) < 1e-9


def test_daily_balances_empty():
    assert kraken.daily_balances([]).empty


def test_reconstruct_value_history(tmp_db, monkeypatch):
    entries = [
        {"time": _ts("2026-01-01"), "asset": "XXBT", "balance": "1.0"},
        {"time": _ts("2026-01-03"), "asset": "XXBT", "balance": "0.0"},   # verkauft
        {"time": _ts("2026-01-01"), "asset": "ZEUR", "balance": "100.0"},  # Cash
    ]
    monkeypatch.setattr(kraken, "get_ledgers", lambda: entries)

    # feste BTC-Kurshistorie in EUR
    idx = pd.date_range("2026-01-01", "2026-01-05", freq="D")
    price = pd.DataFrame({"Close": [50000.0] * len(idx)}, index=idx)
    from data import crypto as crypto_data
    monkeypatch.setattr(crypto_data, "get_history", lambda symbol, days=365: price)

    res = kraken.reconstruct_value_history()
    assert res["ab_datum"] == "2026-01-01"
    assert "BTC" in res["erfasst"] and "EUR" in res["erfasst"]

    hist = {r["snap_date"]: r["value_eur"] for r in tmp_db.list_kraken_value_history()}
    # 01.: 1 BTC * 50000 + 100 Cash = 50100
    assert abs(hist["2026-01-01"] - 50100.0) < 1e-6
    # 03.: BTC verkauft -> nur noch 100 Cash
    assert abs(hist["2026-01-03"] - 100.0) < 1e-6


def test_crypto_history_series_prefers_reconstruction(tmp_db):
    from analysis import performance
    tmp_db.save_snapshot("crypto", 999.0, "2026-05-01")
    tmp_db.replace_kraken_value_history([("2026-05-01", 111.0), ("2026-05-02", 222.0)])
    df, quelle = performance.crypto_history_series()
    assert quelle == "rekonstruiert"
    assert list(df["Wert"]) == [111.0, 222.0]
