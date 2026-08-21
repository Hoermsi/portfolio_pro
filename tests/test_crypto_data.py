"""Tests für data/crypto.py Batch-Marktdaten (ohne Netzwerkzugriff)."""
from data import crypto


def test_resolve_id_prefers_pinned_coingecko_id(tmp_db, monkeypatch):
    """Eine bestätigte CoinGecko-ID (db.set_coingecko_id) hat Vorrang vor
    SYMBOL_TO_ID und der automatischen Suche - für mehrdeutige Symbole."""
    db = tmp_db
    db.set_coingecko_id("RAY", "crypto", "raydium")

    def boom(*a, **k):
        raise AssertionError("Suche darf bei gepinnter ID nicht aufgerufen werden")

    monkeypatch.setattr(crypto, "_get", boom)
    assert crypto.resolve_id("RAY") == "raydium"


def test_resolve_id_falls_back_without_pin(tmp_db):
    """Ohne gepinnte ID greift weiterhin die hardcodierte SYMBOL_TO_ID-Map."""
    assert crypto.resolve_id("BTC") == "bitcoin"


def test_get_market_data_batch_maps_by_id(monkeypatch):
    monkeypatch.setattr(crypto, "resolve_id", lambda s: {"BTC": "bitcoin", "ETH": "ethereum"}.get(s))

    def fake_get(path, params=None):
        assert path == "/coins/markets"
        assert params["ids"] == "bitcoin,ethereum"
        return [
            {"id": "bitcoin", "name": "Bitcoin", "market_cap_rank": 1,
             "market_cap": 1e12, "total_volume": 5e10, "current_price": 50000.0,
             "ath": 70000.0, "ath_change_percentage": -28.5,
             "price_change_percentage_24h": 1.2,
             "price_change_percentage_7d_in_currency": 3.4,
             "price_change_percentage_30d_in_currency": -5.6,
             "circulating_supply": 19e6, "max_supply": 21e6},
            {"id": "ethereum", "name": "Ethereum", "market_cap_rank": 2,
             "ath_change_percentage": -40.0},
        ]

    monkeypatch.setattr(crypto, "_get", fake_get)
    result = crypto.get_market_data_batch(["BTC", "ETH"])
    assert result["BTC"]["ath_abstand_pct"] == -28.5
    assert result["BTC"]["name"] == "Bitcoin"
    assert result["ETH"]["ath_abstand_pct"] == -40.0


def test_get_market_data_batch_unresolvable_symbol_returns_empty(monkeypatch):
    monkeypatch.setattr(crypto, "resolve_id", lambda s: None)
    result = crypto.get_market_data_batch(["FOO"])
    assert result == {"FOO": {}}


def test_get_market_data_batch_api_failure_returns_empty_dicts(monkeypatch):
    monkeypatch.setattr(crypto, "resolve_id", lambda s: "bitcoin")

    def boom(path, params=None):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(crypto, "_get", boom)
    result = crypto.get_market_data_batch(["BTC"])
    assert result == {"BTC": {}}


def test_get_market_data_batch_single_request_for_many_symbols(monkeypatch):
    """Regression: darf NICHT einzeln pro Coin abfragen (CoinGecko-Rate-Limit
    - genau das hat den ATH-Abstand in der Positions-Detail-Tabelle geleert)."""
    calls = []
    monkeypatch.setattr(crypto, "resolve_id", lambda s: s.lower())

    def fake_get(path, params=None):
        calls.append(params["ids"])
        return []

    monkeypatch.setattr(crypto, "_get", fake_get)
    crypto.get_market_data_batch(["BTC", "ETH", "SOL"])
    assert len(calls) == 1
