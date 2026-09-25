import json

import pytest


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


def test_sentiment_history_roundtrip_and_idempotent(tmp_db):
    tmp_db.save_sentiment("fear_greed", 72.0, "2026-07-01")
    tmp_db.save_sentiment("mayer", 45.0, "2026-07-01")
    tmp_db.save_sentiment("fear_greed", 80.0, "2026-07-01")  # Overwrite gleicher Tag
    rows = tmp_db.list_sentiment()
    assert len(rows) == 2
    fg = next(r for r in rows if r["indicator"] == "fear_greed")
    assert fg["value"] == 80.0


def test_sentiment_history_filters_by_indicator_and_days(tmp_db):
    tmp_db.save_sentiment("fear_greed", 50.0, "2026-06-01")
    tmp_db.save_sentiment("fear_greed", 70.0, "2026-07-15")
    tmp_db.save_sentiment("mayer", 1.2, "2026-07-15")
    only_fg = tmp_db.list_sentiment(indicator="fear_greed")
    assert len(only_fg) == 2
    assert all(r["indicator"] == "fear_greed" for r in only_fg)


def test_record_trade_updates_position_and_journal(tmp_db):
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", 4, "2026-07-01")
    tmp_db.record_trade("NVDA", "stock", "buy", 1, 130, "Depot", 2, "2026-07-02")
    position = tmp_db.list_positions("stock")[0]
    assert position.quantity == 3
    assert position.buy_price_eur == 112
    tmp_db.record_trade("NVDA", "stock", "sell", 1, 150, "Depot", 1, "2026-07-03")
    assert tmp_db.list_positions("stock")[0].quantity == 2
    assert len(tmp_db.list_transactions("stock")) == 3


def test_record_trade_without_cash_flag_leaves_cash_untouched(tmp_db):
    """Regression: fund_from_cash ist standardmäßig False - ein normaler
    Trade darf cash_log nicht anfassen (z.B. wenn aus externem Broker-Cash
    bezahlt, das nicht im Bank-Kontostand steckt)."""
    tmp_db.add_cash_entry(1000.0)
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", 4, "2026-07-01")
    assert len(tmp_db.list_cash_entries()) == 1
    assert tmp_db.latest_cash_balance() == 1000.0


def test_record_trade_funds_from_cash_on_buy(tmp_db):
    tmp_db.add_cash_entry(1000.0)
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", fees_eur=4,
                        trade_date="2026-07-01", fund_from_cash=True)
    # 1000 - (2*100 + 4) = 796
    assert tmp_db.latest_cash_balance() == 796.0
    assert len(tmp_db.list_cash_entries()) == 2


def test_record_trade_funds_from_cash_on_sell(tmp_db):
    tmp_db.add_cash_entry(1000.0)
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", trade_date="2026-07-01")
    tmp_db.record_trade("NVDA", "stock", "sell", 1, 150, "Depot", fees_eur=1,
                        trade_date="2026-07-02", fund_from_cash=True)
    # 1000 + (1*150 - 1) = 1149
    assert tmp_db.latest_cash_balance() == 1149.0


def test_record_trade_funds_from_cash_no_prior_balance(tmp_db):
    """Ohne vorherigen cash_log-Eintrag wird 0.0 als Basis angenommen."""
    tmp_db.record_trade("NVDA", "stock", "buy", 2, 100, "Depot", fees_eur=4,
                        trade_date="2026-07-01", fund_from_cash=True)
    assert tmp_db.latest_cash_balance() == -204.0
    assert len(tmp_db.list_cash_entries()) == 1


def test_record_trade_funds_from_cash_appends_cumulatively(tmp_db):
    tmp_db.add_cash_entry(1000.0)
    tmp_db.record_trade("NVDA", "stock", "buy", 1, 100, "Depot", trade_date="2026-07-01", fund_from_cash=True)
    tmp_db.record_trade("AAPL", "stock", "buy", 1, 50, "Depot", trade_date="2026-07-02", fund_from_cash=True)
    assert tmp_db.latest_cash_balance() == 850.0
    assert len(tmp_db.list_cash_entries()) == 3


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
        "Flatex-Import": [{"symbol": "IE00B4L5Y983", "quantity": 5.0, "buy_price": 80.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LEGACY_PORTFOLIO_JSON", legacy)

    from core import db
    db.init_db()
    positions = db.list_positions("stock")
    assert {p.symbol for p in positions} == {"NVDA", "IE00B4L5Y983"}
    flatex = next(p for p in positions if p.symbol == "IE00B4L5Y983")
    assert flatex.source == "flatex"

    # Zweiter Start migriert nicht erneut
    db.init_db()
    assert len(db.list_positions("stock")) == 2


def test_wal_mode_active(tmp_db):
    """Trading-Bot laeuft als eigener Prozess parallel zu Streamlit - ohne WAL
    wuerde ein Schreiber jeden Leser blockieren (siehe core/db.py:_connect())."""
    from core import db
    with db._connect() as con:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_bot_tables_created_with_expected_columns(tmp_db):
    """Reine Schema-Pruefung fuer die Trading-Bot-Tabellen - das Schema muss
    idempotent stehen, auch wenn eine Tabelle (noch) keine CRUD-Funktionen
    hat."""
    from core import db
    with db._connect() as con:
        expected = {
            "bot_positions": {"id", "symbol", "side", "size", "entry_px", "leverage",
                              "stop_px", "exchange_stop_px", "exchange_oid", "opened_at", "closed_at",
                              "close_px", "realized_pnl_usd", "status",
                              "origin", "origin_note", "decision_id", "exchange_opened_at",
                              "initial_stop_px", "strategy_version", "config_hash",
                              "partial_realized_pnl_usd"},
            "bot_orders": {"id", "created_at", "symbol", "intent", "side", "size",
                          "order_type", "requested_px", "status", "exchange_oid",
                          "fill_px", "fee_usd", "error", "decision_id", "position_id"},
            "bot_decisions": {"id", "created_at", "cycle_type", "signals_json", "run_id",
                              "action", "executed", "reason", "config_hash"},
            "bot_equity": {"id", "ts", "equity_usd", "equity_eur", "cash_usd",
                          "fees_cum_usd", "funding_cum_usd", "net_flows_usd"},
            "bot_signal_log": {"id", "ts", "candle_ts", "symbol", "side", "score",
                               "long_score", "short_score", "components_json",
                               "readings_json", "price", "atr_pct", "stop_pct",
                               "funding_hourly", "threshold", "risk_level",
                               "strategy_version", "config_hash", "action", "block_reason",
                               "decision_id", "position_id", "outcome_json",
                               "outcome_last_h"},
        }
        for table, cols in expected.items():
            actual = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            assert actual == cols, f"{table}: erwartet {cols}, war {actual}"
        # zweiter init_db()-Lauf darf nicht scheitern (CREATE TABLE IF NOT EXISTS)
    db.init_db()


def test_add_bot_equity_point_stores_net_flows(tmp_db):
    from core import db
    db.init_db()
    db.add_bot_equity_point(equity_usd=1200.0, equity_eur=1100.0, cash_usd=1200.0,
                            fees_cum_usd=0.5, funding_cum_usd=0.1, net_flows_usd=200.0)
    latest = db.latest_bot_equity()
    assert latest["net_flows_usd"] == pytest.approx(200.0)


def test_migrate_bot_equity_net_flows_adds_missing_column(tmp_db):
    """Bestandsinstallationen ohne die Spalte (vor diesem Feature angelegt)
    duerfen beim naechsten init_db() nicht scheitern - reine ALTER-TABLE-
    Ergaenzung, kein Drop+Neuanlage noetig (kein UNIQUE-Constraint betroffen)."""
    from core import db
    with db._connect() as con:
        con.execute("DROP TABLE IF EXISTS bot_equity")
        con.execute("""
            CREATE TABLE bot_equity (
                id INTEGER PRIMARY KEY, ts TEXT NOT NULL, equity_usd REAL NOT NULL,
                equity_eur REAL NOT NULL, cash_usd REAL, fees_cum_usd REAL, funding_cum_usd REAL
            )
        """)
        cols_before = {r["name"] for r in con.execute("PRAGMA table_info(bot_equity)")}
        assert "net_flows_usd" not in cols_before

    db.init_db()

    with db._connect() as con:
        cols_after = {r["name"] for r in con.execute("PRAGMA table_info(bot_equity)")}
    assert "net_flows_usd" in cols_after


def test_bot_position_origin_migration_adds_columns_to_old_db(tmp_db):
    """Bestehende DBs bekommen die Herkunfts-Spalten per ALTER TABLE
    nachgezogen (kein Drop+Neuanlage - es haengen echte Handelsdaten daran).
    Alte Zeilen gelten als 'bot': bis zu dieser Aenderung konnte nur
    core.bot.open_position eine Zeile anlegen."""
    from core import db
    with db._connect() as con:
        # Alt-Schema nachstellen: Spalten wieder wegnehmen ist in SQLite
        # umstaendlich, also die Tabelle in ihrer alten Form neu bauen.
        con.executescript(
            "DROP TABLE bot_positions;"
            "CREATE TABLE bot_positions ("
            " id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,"
            " size REAL NOT NULL, entry_px REAL NOT NULL, leverage REAL NOT NULL DEFAULT 1,"
            " stop_px REAL, exchange_oid TEXT, opened_at TEXT NOT NULL, closed_at TEXT,"
            " close_px REAL, realized_pnl_usd REAL, status TEXT NOT NULL DEFAULT 'open');"
            "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, stop_px, opened_at)"
            " VALUES ('BTC', 'long', 0.001, 80000.0, 1.0, 75000.0, '2026-01-01T00:00:00');"
        )
        assert "origin" not in {r["name"] for r in con.execute("PRAGMA table_info(bot_positions)")}

    db.init_db()

    with db._connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(bot_positions)")}
        assert {"origin", "origin_note", "decision_id", "exchange_opened_at",
                "initial_stop_px", "exchange_stop_px"} <= cols
        row = con.execute("SELECT * FROM bot_positions").fetchone()
        assert row["origin"] == "bot"
        # Best-effort-Rueckwirkung: der aktuelle stop_px ist die einzig noch
        # verfuegbare Naeherung fuer den nicht mehr rekonstruierbaren
        # urspruenglichen Stop bestehender Zeilen.
        assert row["initial_stop_px"] == 75000.0
        assert row["exchange_stop_px"] == 75000.0
        assert row["symbol"] == "BTC"      # Daten unangetastet
    db.init_db()                           # zweiter Lauf bleibt idempotent


def test_set_bot_position_exchange_opened_at_never_overwrites_a_known_value(tmp_db):
    """Der DB-seitige Teil des Nachtrags (core.bot.backfill_exchange_opened_at):
    WHERE exchange_opened_at IS NULL darf keine Ausnahme kennen - ein bereits
    bekannter echter Wert ist immer verlaesslicher als ein nachtraeglicher
    Best-effort-Fund."""
    from core import db
    pid = db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                      leverage=1.0, stop_px=75000.0, exchange_oid="1",
                                      origin="adopted", exchange_opened_at="2026-01-01T00:00:00")

    updated_first = db.set_bot_position_exchange_opened_at(pid, "2026-06-01T00:00:00")
    updated_again = db.set_bot_position_exchange_opened_at(pid, "2026-07-01T00:00:00")

    assert updated_first is False
    assert updated_again is False
    pos = db.get_bot_position("BTC")
    assert pos["exchange_opened_at"] == "2026-01-01T00:00:00"


def test_set_bot_position_exchange_opened_at_fills_a_missing_value(tmp_db):
    from core import db
    pid = db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                      leverage=1.0, stop_px=75000.0, exchange_oid="1",
                                      origin="adopted", exchange_opened_at=None)

    updated = db.set_bot_position_exchange_opened_at(pid, "2026-06-01T00:00:00")

    assert updated is True
    assert db.get_bot_position("BTC")["exchange_opened_at"] == "2026-06-01T00:00:00"


def test_bot_positions_unique_open_symbol_index_blocks_a_second_open_row(tmp_db):
    """Zweite Verteidigungslinie gegen zwei parallel laufende Runner-Prozesse
    (die eigentliche Sperre ist die Lockdatei in bot_runner.py) - der
    partielle UNIQUE-Index verhindert, dass zwei OFFENE Zeilen desselben
    Symbols gleichzeitig in der DB stehen."""
    import sqlite3
    from core import db
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=75000.0, exchange_oid="1")
    with pytest.raises(sqlite3.IntegrityError):
        with db._connect() as con:
            con.execute(
                "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, "
                "stop_px, opened_at, status) VALUES ('BTC', 'long', 0.002, 81000.0, 1.0, "
                "76000.0, '2026-01-01T00:00:00', 'open')"
            )


def test_bot_positions_unique_open_symbol_migration_skips_on_preexisting_conflict(tmp_db):
    """Ein bereits bestehender Verstoss (z.B. aus der Zeit vor diesem Index)
    darf init_db() nicht abstuerzen lassen - die Migration ueberspringt die
    Index-Anlage in dem Fall und versucht es beim naechsten Start erneut."""
    from core import db
    with db._connect() as con:
        con.execute("DROP INDEX IF EXISTS idx_bot_positions_open_symbol")
        for i in (1, 2):
            con.execute(
                "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, "
                "stop_px, opened_at, status) VALUES ('BTC', 'long', 0.001, 80000.0, 1.0, "
                f"75000.0, '2026-01-0{i}T00:00:00', 'open')"
            )

    db.init_db()  # darf nicht werfen, obwohl zwei offene BTC-Zeilen bestehen

    with db._connect() as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_bot_positions_open_symbol'"
        ).fetchone()
    assert exists is None


def test_signal_log_roundtrip_and_pending_selection(tmp_db):
    from core import db
    db.add_bot_signal_logs([
        {"ts": "2026-08-01T10:00:00", "symbol": "BTC", "side": "long",
         "score": 71.0, "action": "skipped", "block_reason": None},
        {"ts": "2026-08-20T10:00:00", "symbol": "ETH", "side": "short",
         "score": 44.0, "action": "blocked", "block_reason": "ATR zu niedrig"},
    ])

    assert db.count_bot_signal_log() == {"total": 2, "complete": 0}
    # Nur die alte Zeile liegt vor dem Stichtag -> nur sie ist nachzutragen.
    pending = db.list_bot_signal_log_pending(6, "2026-08-02T00:00:00")
    assert [p["symbol"] for p in pending] == ["BTC"]

    db.set_bot_signal_outcome(pending[0]["id"], '{"6": {"return_pct": 1.2}}', 6)
    assert db.list_bot_signal_log_pending(6, "2026-08-02T00:00:00") == []
    assert db.list_bot_signal_log_pending(12, "2026-08-02T00:00:00")[0]["symbol"] == "BTC"


def test_signal_log_dedups_same_candle_across_ticks(tmp_db):
    """Der Bot pollt alle 15 Min, eine Kerze schliesst nur stuendlich - ohne
    den Unique-Index wuerde derselbe Kandidat pro Stunde bis zu 4x geloggt
    und ein spaeteres Lernmodell haette Stunden mit durchgehendem Runner
    staerker gewichtet als Stunden mit Ausfaellen."""
    from core import db

    row = {"ts": "2026-08-01T10:00:00", "candle_ts": "2026-08-01T09:00:00",
          "symbol": "BTC", "side": "long", "score": 70.0,
          "strategy_version": "1.0", "risk_level": 5, "action": "skipped"}

    first = db.add_bot_signal_logs([row])
    second = db.add_bot_signal_logs([{**row, "ts": "2026-08-01T10:15:00"}])
    third = db.add_bot_signal_logs([{**row, "ts": "2026-08-01T10:30:00"}])

    assert first == 1
    assert second == 0
    assert third == 0
    assert db.count_bot_signal_log()["total"] == 1


def test_signal_log_allows_different_candles_or_symbols(tmp_db):
    from core import db

    base = {"symbol": "BTC", "side": "long", "score": 70.0,
           "strategy_version": "1.0", "risk_level": 5, "action": "skipped"}
    n = db.add_bot_signal_logs([
        {**base, "ts": "t1", "candle_ts": "2026-08-01T09:00:00"},
        {**base, "ts": "t2", "candle_ts": "2026-08-01T10:00:00"},   # andere Kerze
        {**base, "ts": "t3", "candle_ts": "2026-08-01T09:00:00", "symbol": "ETH"},
    ])
    assert n == 3


def test_migration_reconciles_adopted_origin_from_decisions(tmp_db):
    """Der reale Fall: fuenf Positionen wurden laut bot_decisions automatisch
    uebernommen, standen aber nach der Migration auf origin='bot' - sie
    waeren sonst faelschlich als eigene Bot-Entscheidungen ins Lernen
    eingeflossen."""
    from core import db
    with db._connect() as con:
        con.executescript(
            "DROP TABLE bot_positions;"
            "CREATE TABLE bot_positions ("
            " id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,"
            " size REAL NOT NULL, entry_px REAL NOT NULL, leverage REAL NOT NULL DEFAULT 1,"
            " stop_px REAL, exchange_oid TEXT, opened_at TEXT NOT NULL, closed_at TEXT,"
            " close_px REAL, realized_pnl_usd REAL, status TEXT NOT NULL DEFAULT 'open');"
        )
        con.execute(
            "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, stop_px, "
            "opened_at) VALUES ('ETH', 'long', 0.1, 2500.0, 1.0, 2480.0, "
            "'2026-08-28T00:31:33')")
        # Eine SPAETERE, tatsaechlich vom Bot eroeffnete Position im selben
        # Symbol - darf NICHT umetikettiert werden (Zeitabstand > 60s).
        con.execute(
            "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, stop_px, "
            "opened_at, status) VALUES ('LINK', 'long', 1.0, 10.0, 1.0, 9.5, "
            "'2026-08-28T00:31:33', 'closed')")
        con.execute(
            "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, stop_px, "
            "opened_at) VALUES ('LINK', 'long', 2.0, 12.0, 1.0, 11.0, "
            "'2026-08-29T09:00:00')")
        con.execute(
            "INSERT INTO bot_decisions (created_at, cycle_type, action, executed, signals_json) "
            "VALUES ('2026-08-28T00:31:33', 'deterministic', 'position_adopted', 1, ?)",
            ('{"symbol": "ETH", "stop_px": 2480.0}',))
        con.execute(
            "INSERT INTO bot_decisions (created_at, cycle_type, action, executed, signals_json) "
            "VALUES ('2026-08-28T00:31:33', 'deterministic', 'position_adopted', 1, ?)",
            ('{"symbol": "LINK", "stop_px": 9.5}',))

    db.init_db()

    with db._connect() as con:
        eth = con.execute("SELECT origin FROM bot_positions WHERE symbol='ETH' "
                          "AND status='open'").fetchone()
        assert eth["origin"] == "adopted"
        link_open = con.execute("SELECT origin FROM bot_positions WHERE symbol='LINK' "
                                "AND status='open'").fetchone()
        assert link_open["origin"] == "bot"   # zeitlich zu weit weg -> unangetastet


def test_signal_log_upgrades_skipped_to_opened_within_same_candle(tmp_db):
    """DER Kernfall aus dem Review: ein Kandidat wird beim ERSTEN 15-Minuten-
    Takt einer Stunde uebersprungen (z.B. Schwelle noch zu hoch), aber
    innerhalb DERSELBEN Kerze (candle_ts unveraendert) bei einem spaeteren
    Takt tatsaechlich eroeffnet (z.B. weil zwischenzeitlich ein Slot frei
    wurde und die Schwelle sank). Ein reines INSERT OR IGNORE haette den
    echten Trade dauerhaft als 'skipped' verzeichnet."""
    from core import db

    base = {"symbol": "BTC", "side": "long", "candle_ts": "2026-08-01T09:00:00",
           "strategy_version": "1.0", "risk_level": 5}

    first = db.add_bot_signal_logs([
        {**base, "ts": "2026-08-01T10:00:00", "score": 60.0, "action": "skipped"}])
    second = db.add_bot_signal_logs([
        {**base, "ts": "2026-08-01T10:15:00", "score": 61.0, "action": "opened",
        "decision_id": 42}])

    assert first == 1
    assert second == 1                      # die Aktualisierung zaehlt als Treffer
    rows = db.list_bot_signal_log()
    assert len(rows) == 1                    # weiterhin nur EINE Zeile fuer die Kerze
    assert rows[0]["action"] == "opened"
    assert rows[0]["decision_id"] == 42
    assert rows[0]["ts"] == "2026-08-01T10:15:00"


def test_signal_log_never_downgrades_an_opened_row(tmp_db):
    """Einmal 'opened' bleibt 'opened' - ein spaeterer, weniger
    aussagekraeftiger Takt (z.B. weil das Symbol danach woanders blockiert
    wuerde) darf den Trade-Nachweis nicht wieder verwerfen."""
    from core import db

    base = {"symbol": "BTC", "side": "long", "candle_ts": "2026-08-01T09:00:00",
           "strategy_version": "1.0", "risk_level": 5}

    db.add_bot_signal_logs([
        {**base, "ts": "2026-08-01T10:00:00", "score": 61.0, "action": "opened",
        "decision_id": 42}])
    n = db.add_bot_signal_logs([
        {**base, "ts": "2026-08-01T10:15:00", "score": 55.0, "action": "skipped"}])

    assert n == 0
    row = db.list_bot_signal_log()[0]
    assert row["action"] == "opened"
    assert row["decision_id"] == 42
