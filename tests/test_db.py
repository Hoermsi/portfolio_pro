import json


def test_position_crud(tmp_db):
    tmp_db.save_position("NVDA", "stock", 10, 100, category="Tech")
    tmp_db.save_position("BTC", "crypto", 0.5, 30000, category="Kraken", source="kraken")

    stocks = tmp_db.list_positions("stock")
    cryptos = tmp_db.list_positions("crypto")
    assert len(stocks) == 1 and stocks[0].symbol == "NVDA"
    assert len(cryptos) == 1 and cryptos[0].source == "kraken"

    # Upsert: gleiche Kategorie überschreibt Menge/Einstand
    tmp_db.save_position("NVDA", "stock", 15, 110, category="Tech")
    stocks = tmp_db.list_positions("stock")
    assert len(stocks) == 1
    assert stocks[0].quantity == 15
    assert stocks[0].buy_price_eur == 110

    # Gleiche Aktie in anderer Kategorie = eigene Position
    tmp_db.save_position("NVDA", "stock", 5, 90, category="Trading")
    assert len(tmp_db.list_positions("stock")) == 2
    assert tmp_db.get_position_quantity("NVDA", "stock") == 20

    pos_id = tmp_db.list_positions("crypto")[0].id
    tmp_db.delete_position(pos_id)
    assert tmp_db.list_positions("crypto") == []


def test_snapshots(tmp_db):
    tmp_db.save_snapshot("stock", 1000, "2026-07-01")
    tmp_db.save_snapshot("crypto", 500, "2026-07-01")
    tmp_db.save_snapshot("stock", 1100, "2026-07-01")  # Overwrite gleicher Tag
    snaps = tmp_db.list_snapshots()
    assert len(snaps) == 2
    assert {s["total_value_eur"] for s in snaps} == {1100, 500}


def test_record_trade_updates_position_and_journal(tmp_db):
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", 4, "2026-07-01")
    tmp_db.record_trade("NVDA", "stock", "buy", 1, 130, "Depot", 2, "2026-07-02")
    position = tmp_db.list_positions("stock")[0]
    assert position.quantity == 3
    assert position.buy_price_eur == 112
    tmp_db.record_trade("NVDA", "stock", "sell", 1, 150, "Depot", 1, "2026-07-03")
    assert tmp_db.list_positions("stock")[0].quantity == 2
    assert len(tmp_db.list_transactions("stock")) == 3


def test_agent_run_log(tmp_db):
    tmp_db.log_agent_run("NVDA", "asset", 72, "Halten", 0.03, {"foo": "bar"})
    runs = tmp_db.list_agent_runs()
    assert len(runs) == 1
    assert runs[0]["recommendation"] == "Halten"
    assert abs(tmp_db.total_agent_cost() - 0.03) < 1e-9


def test_migration_from_v1_json(tmp_path, monkeypatch):
    from core import config
    legacy = tmp_path / "portfolio.json"
    legacy.write_text(json.dumps({
        "Standard": [{"symbol": "NVDA", "quantity": 10.0, "buy_price": 100.0}],
        "Flatex-Import": [{"symbol": "FR0010755611", "quantity": 0.659, "buy_price": 17.314}],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LEGACY_PORTFOLIO_JSON", legacy)

    from core import db
    db.init_db()
    positions = db.list_positions("stock")
    assert {p.symbol for p in positions} == {"NVDA", "FR0010755611"}
    flatex = next(p for p in positions if p.symbol == "FR0010755611")
    assert flatex.source == "flatex"

    # Zweiter Start migriert nicht erneut
    db.init_db()
    assert len(db.list_positions("stock")) == 2
