from types import SimpleNamespace

from agents.base import (PORTFOLIO_SENIOR_SCHEMA, SENIOR_SCHEMA, compute_cost,
                         parse_json)
from data.kraken import get_eur_cash, normalize_asset


def test_portfolio_senior_schema_has_cash_vorschlaege():
    assert "cash_vorschlaege" in PORTFOLIO_SENIOR_SCHEMA["properties"]
    assert "cash_vorschlaege" in PORTFOLIO_SENIOR_SCHEMA["required"]
    # Einzelwert-Analyse bleibt ohne Cash-Feld
    assert "cash_vorschlaege" not in SENIOR_SCHEMA["properties"]
    # Item-Felder korrekt
    item = PORTFOLIO_SENIOR_SCHEMA["properties"]["cash_vorschlaege"]["items"]
    assert set(item["required"]) == {"betrag_eur", "symbol", "asset_type", "begruendung"}


def test_strategist_schema_has_cash_hinweis():
    from agents import strategist
    schema = strategist._schema_for("stock")
    assert "cash_hinweis" in schema["properties"]
    # cash_hinweis ist optional (nicht required), Empfehlungen bleiben mandats-beschränkt
    enum = schema["properties"]["empfehlungen"]["items"]["properties"]["asset_type"]["enum"]
    assert enum == ["stock", "cash"]


def test_parse_json_variants():
    assert parse_json('{"score": 70}') == {"score": 70}
    assert parse_json('Hier das Ergebnis:\n{"score": 70, "punkte": ["a"]}\nEnde.') == {
        "score": 70, "punkte": ["a"]
    }
    assert parse_json("kein json") is None
    assert parse_json(None) is None


def test_compute_cost_haiku():
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=200_000,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    c = compute_cost("claude-haiku-4-5", usage)
    # 1M In * $1 + 0.2M Out * $5 = $2.00
    assert abs(c["cost_usd"] - 2.0) < 1e-6


def test_compute_cost_cache_read_discount():
    usage = SimpleNamespace(input_tokens=0, output_tokens=0,
                            cache_read_input_tokens=1_000_000, cache_creation_input_tokens=0)
    c = compute_cost("claude-opus-4-8", usage)
    # 1M Cache-Read * $5 * 0.1 = $0.50
    assert abs(c["cost_usd"] - 0.5) < 1e-6


def test_kraken_asset_normalization():
    assert normalize_asset("XXBT") == "BTC"
    assert normalize_asset("XETH") == "ETH"
    assert normalize_asset("ETH2.S") == "ETH"
    assert normalize_asset("DOT28.S") == "DOT"
    assert normalize_asset("SOL") == "SOL"
    assert normalize_asset("ZEUR") is None
    assert normalize_asset("USDT") is not None  # Stablecoin bleibt sichtbar


def test_kraken_eur_cash_sum():
    raw = {"ZEUR": 1000.0, "XXBT": 0.5, "EUR.HOLD": 50.0, "XETH": 2.0, "ZUSD": 30.0}
    # ZEUR + EUR.HOLD = 1050, kein USD, kein Krypto
    assert abs(get_eur_cash(raw) - 1050.0) < 1e-9
    assert get_eur_cash({"XXBT": 1.0}) == 0.0


def test_crypto_eur_pegged_no_network():
    from data import crypto
    # EUR (und EUR-Stablecoins) müssen ohne Netzabruf 1.0 liefern
    assert crypto.get_prices_eur(("EUR",)) == {"EUR": 1.0}
    assert crypto.get_price_eur("EURC") == 1.0
