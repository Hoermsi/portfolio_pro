"""Tests für data/sentiment.py (Netzwerk vollständig gemockt)."""
import pytest

from data import sentiment


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _reset_caches():
    for fn in (sentiment.fear_greed, sentiment.global_metrics,
              sentiment.altcoin_breadth_30d, sentiment.meme_market,
              sentiment.coinbase_app_rank):
        fn.cache_clear()
    yield
    for fn in (sentiment.fear_greed, sentiment.global_metrics,
              sentiment.altcoin_breadth_30d, sentiment.meme_market,
              sentiment.coinbase_app_rank):
        fn.cache_clear()


def test_fear_greed_parses_and_orders_history(monkeypatch):
    payload = {"data": [
        {"value": "72", "value_classification": "Greed", "timestamp": "1700000200"},
        {"value": "65", "value_classification": "Greed", "timestamp": "1700000100"},
    ]}
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(payload))
    result = sentiment.fear_greed()
    assert result["value"] == 72
    assert result["classification"] == "Greed"
    # chronologisch aufsteigend (ältester zuerst)
    assert len(result["history"]) == 2
    assert result["history"][0]["value"] == 65
    assert result["history"][1]["value"] == 72


def test_fear_greed_network_error_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise Exception("offline")
    monkeypatch.setattr(sentiment.requests, "get", _boom)
    assert sentiment.fear_greed() is None


def test_fear_greed_empty_data_returns_none(monkeypatch):
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp({"data": []}))
    assert sentiment.fear_greed() is None


def test_global_metrics_computes_stablecoin_dominance(monkeypatch):
    payload = {"data": {"market_cap_percentage": {
        "btc": 59.65, "eth": 11.04, "usdt": 4.2, "usdc": 1.1, "sol": 3.0,
    }}}
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(payload))
    result = sentiment.global_metrics()
    assert result["btc_dominance"] == 59.65
    assert result["eth_dominance"] == 11.04
    assert result["stablecoin_dominance"] == pytest.approx(5.3)


def test_global_metrics_missing_data_returns_none(monkeypatch):
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp({"data": {}}))
    assert sentiment.global_metrics() is None


def test_altcoin_breadth_excludes_stablecoins_and_wrapped(monkeypatch):
    coins = [
        {"symbol": "btc", "price_change_percentage_30d_in_currency": 10.0},
        {"symbol": "eth", "price_change_percentage_30d_in_currency": 20.0},   # schlägt BTC
        {"symbol": "sol", "price_change_percentage_30d_in_currency": 5.0},    # schlägt BTC nicht
        {"symbol": "usdt", "price_change_percentage_30d_in_currency": 0.01},  # ausgeschlossen
        {"symbol": "wbtc", "price_change_percentage_30d_in_currency": 9.9},   # ausgeschlossen
    ]
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(coins))
    result = sentiment.altcoin_breadth_30d()
    assert result["sample_size"] == 2       # nur eth + sol zählen
    assert result["btc_change_30d"] == 10.0
    assert result["pct_outperforming"] == pytest.approx(50.0)  # nur eth schlägt btc


def test_altcoin_breadth_no_btc_returns_none(monkeypatch):
    coins = [{"symbol": "eth", "price_change_percentage_30d_in_currency": 5.0}]
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(coins))
    assert sentiment.altcoin_breadth_30d() is None


def test_meme_market_finds_category(monkeypatch):
    categories = [
        {"id": "layer-1", "market_cap": 1e12, "market_cap_change_24h": 1.0, "volume_24h": 1e9},
        {"id": "meme-token", "market_cap": 3.07e10, "market_cap_change_24h": 6.59, "volume_24h": 4.4e9},
    ]
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(categories))
    result = sentiment.meme_market()
    assert result["market_cap"] == pytest.approx(3.07e10)
    assert result["change_24h_pct"] == pytest.approx(6.59)


def test_meme_market_category_missing_returns_none(monkeypatch):
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp([{"id": "layer-1"}]))
    assert sentiment.meme_market() is None


def test_coinbase_app_rank_found(monkeypatch):
    payload = {"feed": {"results": [
        {"name": "Instagram", "artistName": "Meta"},
        {"name": "Coinbase: Buy Bitcoin & ETH", "artistName": "Coinbase, Inc."},
    ]}}
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(payload))
    assert sentiment.coinbase_app_rank() == 2


def test_coinbase_app_rank_not_found(monkeypatch):
    payload = {"feed": {"results": [{"name": "Instagram", "artistName": "Meta"}]}}
    monkeypatch.setattr(sentiment.requests, "get", lambda *a, **k: _FakeResp(payload))
    assert sentiment.coinbase_app_rank() is None


def test_all_sources_exception_safe(monkeypatch):
    """Jede Quelle muss bei jedem Fehler None liefern, nie werfen."""
    def _boom(*a, **k):
        raise Exception("kaputt")
    monkeypatch.setattr(sentiment.requests, "get", _boom)
    assert sentiment.fear_greed() is None
    assert sentiment.global_metrics() is None
    assert sentiment.altcoin_breadth_30d() is None
    assert sentiment.meme_market() is None
    assert sentiment.coinbase_app_rank() is None
