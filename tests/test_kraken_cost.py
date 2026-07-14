from data.kraken import compute_avg_buy_prices, split_pair


def test_split_pair():
    assert split_pair("XXBTZEUR") == ("XXBT", "EUR")
    assert split_pair("SOLEUR") == ("SOL", "EUR")
    assert split_pair("XETHZUSD") == ("XETH", "USD")
    assert split_pair("ETHUSDT") == ("ETH", "USD")
    assert split_pair("ADAUSDC") == ("ADA", "USD")
    assert split_pair("XXBTZJPY") == (None, None)   # unbekannte Quote
    assert split_pair("EUR") == (None, None)         # nur Quote, keine Basis


def test_avg_buy_price_eur_pairs():
    trades = [
        # 0.1 BTC für 5000€ + 10€ Gebühr, dann 0.1 BTC für 6000€ + 10€
        {"pair": "XXBTZEUR", "type": "buy", "cost": "5000", "fee": "10", "vol": "0.1"},
        {"pair": "XXBTZEUR", "type": "buy", "cost": "6000", "fee": "10", "vol": "0.1"},
        # Verkäufe werden ignoriert
        {"pair": "XXBTZEUR", "type": "sell", "cost": "3000", "fee": "5", "vol": "0.05"},
    ]
    avg = compute_avg_buy_prices(trades, usd_to_eur=0.9)
    # (5010 + 6010) / 0.2 = 55100
    assert abs(avg["BTC"] - 55100) < 1e-6


def test_avg_buy_price_usd_conversion_and_staking_codes():
    trades = [
        # 10 SOL für 1000 USD + 0 Fee -> 900 EUR bei fx 0.9 -> 90 €/SOL
        {"pair": "SOLUSD", "type": "buy", "cost": "1000", "fee": "0", "vol": "10"},
        # Staking-Code im Paarnamen wird aufs Basissymbol normalisiert
        {"pair": "ETH2.SEUR", "type": "buy", "cost": "2000", "fee": "0", "vol": "1"},
        # Fiat-Trade wird ignoriert
        {"pair": "EURUSD", "type": "buy", "cost": "500", "fee": "0", "vol": "500"},
        # kaputte Werte werden übersprungen
        {"pair": "ADAEUR", "type": "buy", "cost": "abc", "fee": "0", "vol": "10"},
    ]
    avg = compute_avg_buy_prices(trades, usd_to_eur=0.9)
    assert abs(avg["SOL"] - 90) < 1e-6
    assert abs(avg["ETH"] - 2000) < 1e-6
    assert "EUR" not in avg
    assert "ADA" not in avg


def test_avg_buy_price_usd_without_fx_skipped():
    trades = [{"pair": "SOLUSD", "type": "buy", "cost": "1000", "fee": "0", "vol": "10"}]
    assert compute_avg_buy_prices(trades, usd_to_eur=None) == {}
