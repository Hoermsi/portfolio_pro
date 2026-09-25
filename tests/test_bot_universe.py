import pytest

from core import bot_universe, clock


def _row(symbol, rang=1, cap=1_000_000_000.0, age_days=365, hl_volume=10_000_000.0):
    earliest = None
    if age_days is not None:
        earliest = clock.iso_utc(clock.now_utc() - __import__("datetime").timedelta(days=age_days))
    return {"symbol": symbol, "rang": rang, "marktkap_eur": cap,
            "volumen_24h_eur": cap / 10, "kurs_eur": 1.0,
            "aeltester_datenpunkt": earliest, "hl_day_volume_usd": hl_volume}


def test_apply_deterministic_filters_blocked_symbol():
    rows = [_row("TRUMP")]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"TRUMP"})
    assert qualified == []
    assert "gesperrt" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_market_cap_gate():
    rows = [_row("BTC", cap=bot_universe.MIN_MARKET_CAP_EUR - 1)]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"BTC"})
    assert qualified == []
    assert len(rejected) == 1
    assert "Marktkapitalisierung" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_age_gate():
    rows = [_row("BTC", age_days=bot_universe.MIN_LISTING_AGE_DAYS - 1)]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"BTC"})
    assert qualified == []
    assert "Tage" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_missing_age_rejected():
    rows = [_row("BTC", age_days=None)]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"BTC"})
    assert qualified == []
    assert "unbekannt" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_volume_gate():
    rows = [_row("BTC", hl_volume=bot_universe.MIN_HL_DAY_VOLUME_USD - 1)]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"BTC"})
    assert qualified == []
    assert "Volumen" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_not_on_hyperliquid():
    rows = [_row("XYZ")]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, set())
    assert qualified == []
    assert "Hyperliquid" in rejected[0]["rejected_reason"]


def test_apply_deterministic_filters_qualifies():
    rows = [_row("BTC"), _row("ETH", rang=2)]
    qualified, rejected = bot_universe.apply_deterministic_filters(rows, {"BTC", "ETH"})
    assert rejected == []
    assert [r["symbol"] for r in qualified] == ["BTC", "ETH"]
    assert "included_reason" in qualified[0]


def test_apply_deterministic_filters_sorted_by_rank():
    rows = [_row("ETH", rang=5), _row("BTC", rang=1)]
    qualified, _ = bot_universe.apply_deterministic_filters(rows, {"BTC", "ETH"})
    assert [r["symbol"] for r in qualified] == ["BTC", "ETH"]


def test_discover_candidates(monkeypatch):
    from data import crypto, hyperliquid

    monkeypatch.setattr(crypto, "get_top_market_cap",
                        lambda top_n=250: [_row("BTC"), _row("XRP", cap=100.0)])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: {"BTC", "XRP"})
    monkeypatch.setattr(hyperliquid, "day_notional_volume_usd", lambda s: 10_000_000.0)
    result = bot_universe.discover_candidates()
    assert [r["symbol"] for r in result["qualified"]] == ["BTC"]
    assert [r["symbol"] for r in result["rejected"]] == ["XRP"]


def test_discover_candidates_dedupes_symbols(monkeypatch):
    from data import crypto, hyperliquid

    monkeypatch.setattr(crypto, "get_top_market_cap",
                        lambda top_n=250: [_row("BTC", rang=1), _row("BTC", rang=2)])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: {"BTC"})
    monkeypatch.setattr(hyperliquid, "day_notional_volume_usd", lambda s: 10_000_000.0)
    result = bot_universe.discover_candidates()
    assert len(result["qualified"]) == 1


def test_discover_candidates_hyperliquid_failure_preserves_the_last_pool(monkeypatch):
    from data import crypto, hyperliquid

    monkeypatch.setattr(crypto, "get_top_market_cap", lambda top_n=250: [_row("BTC")])

    def _boom():
        raise RuntimeError("down")
    monkeypatch.setattr(hyperliquid, "supported_symbols", _boom)
    with pytest.raises(bot_universe.UniverseDiscoveryError):
        bot_universe.discover_candidates()


def test_discover_candidates_empty_market_data_is_an_error(monkeypatch):
    from data import crypto

    monkeypatch.setattr(crypto, "get_top_market_cap", lambda top_n=250: [])
    with pytest.raises(bot_universe.UniverseDiscoveryError):
        bot_universe.discover_candidates()


def test_refresh_pool_first_call_always_refreshes(tmp_db, monkeypatch):
    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    result = bot_universe.refresh_pool()
    assert result["skipped"] is False
    assert result["symbols"] == ["BTC"]
    assert bot_universe.pool_symbols() == ["BTC"]
    assert bot_universe.pool_refreshed_at() is not None


def test_refresh_pool_within_interval_is_noop(tmp_db, monkeypatch):
    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    bot_universe.refresh_pool()

    called = []
    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: called.append(1) or {"qualified": [], "rejected": []})
    result = bot_universe.refresh_pool()
    assert result["skipped"] is True
    assert called == []
    assert bot_universe.pool_symbols() == ["BTC"]


def test_refresh_pool_after_interval_refreshes_again(tmp_db, monkeypatch):
    from core import db

    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    bot_universe.refresh_pool()
    stale = clock.now_utc() - __import__("datetime").timedelta(
        hours=bot_universe.DISCOVERY_REFRESH_INTERVAL_HOURS + 1)
    db.set_meta(bot_universe._POOL_REFRESHED_AT_META_KEY, clock.iso_utc(stale))

    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("ETH")], "rejected": []})
    result = bot_universe.refresh_pool()
    assert result["skipped"] is False
    assert bot_universe.pool_symbols() == ["ETH"]


def test_refresh_pool_force_ignores_interval(tmp_db, monkeypatch):
    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    bot_universe.refresh_pool()

    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("ETH")], "rejected": []})
    result = bot_universe.refresh_pool(force=True)
    assert result["skipped"] is False
    assert bot_universe.pool_symbols() == ["ETH"]


def test_refresh_pool_failure_does_not_advance_gate(tmp_db, monkeypatch):
    def _boom():
        raise RuntimeError("CoinGecko down")
    monkeypatch.setattr(bot_universe, "discover_candidates", _boom)
    import pytest
    with pytest.raises(RuntimeError):
        bot_universe.refresh_pool()
    assert bot_universe.pool_refreshed_at() is None
    assert bot_universe.pool_symbols() == []


def test_refresh_failure_keeps_an_existing_snapshot(tmp_db, monkeypatch):
    from core import db

    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    bot_universe.refresh_pool()
    refreshed_at = bot_universe.pool_refreshed_at()
    db.set_meta(bot_universe._POOL_REFRESHED_AT_META_KEY,
                clock.iso_utc(clock.now_utc() - __import__("datetime").timedelta(days=2)))

    monkeypatch.setattr(bot_universe, "discover_candidates",
                        lambda: (_ for _ in ()).throw(bot_universe.UniverseDiscoveryError("down")))
    with pytest.raises(bot_universe.UniverseDiscoveryError):
        bot_universe.refresh_pool()

    assert bot_universe.pool_symbols() == ["BTC"]
    assert bot_universe.pool_refreshed_at() != refreshed_at


def test_active_pool_empty_without_refresh(tmp_db):
    assert bot_universe.active_pool() == []
    assert bot_universe.pool_symbols() == []


def test_active_pool_survives_corrupted_meta(tmp_db):
    from core import db
    db.set_meta(bot_universe._POOL_META_KEY, "not json")
    assert bot_universe.active_pool() == []


def test_active_pool_filters_a_newly_blocked_cached_symbol(tmp_db):
    from core import db
    import json

    db.set_meta(bot_universe._POOL_META_KEY, json.dumps([_row("BTC"), _row("TRUMP", rang=2)]))
    assert bot_universe.pool_symbols() == ["BTC"]


def test_candidate_symbols_uses_pool(tmp_db, monkeypatch):
    from core import bot
    from data import hyperliquid

    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: ["MATIC", "ARB"])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: {"MATIC", "ARB"})
    assert bot.candidate_symbols() == ["MATIC", "ARB"]


def test_candidate_symbols_excludes_blocked_symbol(tmp_db, monkeypatch):
    from core import bot
    from data import hyperliquid

    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: ["BTC", "TRUMP"])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: {"BTC", "TRUMP"})
    assert bot.candidate_symbols() == ["BTC"]


def test_candidate_symbols_keeps_open_position_in_blocked_symbol(tmp_db, monkeypatch):
    from core import bot, db
    from data import hyperliquid

    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: ["BTC"])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: {"BTC", "TRUMP"})
    db.upsert_open_bot_position(symbol="TRUMP", side="long", size=1.0, entry_px=1.0,
                                leverage=1.0, stop_px=0.5, exchange_oid=None)
    result = bot.candidate_symbols()
    assert "TRUMP" in result
    assert "BTC" in result


def test_candidate_symbols_falls_back_when_pool_empty(tmp_db, monkeypatch):
    from core import bot
    from data import hyperliquid

    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: [])
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: set(bot.CANDIDATE_UNIVERSE))
    assert bot.candidate_symbols() == list(bot.CANDIDATE_UNIVERSE)


def test_candidate_symbols_does_not_fall_back_after_a_valid_empty_snapshot(tmp_db, monkeypatch):
    from core import bot, db
    from data import hyperliquid

    db.set_meta(bot_universe._POOL_META_KEY, "[]")
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: set(bot.CANDIDATE_UNIVERSE))
    assert bot.candidate_symbols() == []


def test_candidate_symbols_caps_at_max_active(tmp_db, monkeypatch):
    from core import bot
    from data import hyperliquid

    many = [f"SYM{i}" for i in range(bot_universe.MAX_ACTIVE_CANDIDATES + 5)]
    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: many)
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: set(many))
    result = bot.candidate_symbols()
    assert len(result) == bot_universe.MAX_ACTIVE_CANDIDATES
    assert result == many[:bot_universe.MAX_ACTIVE_CANDIDATES]


def test_candidate_symbols_force_includes_open_positions_beyond_cap(tmp_db, monkeypatch):
    from core import bot, db
    from data import hyperliquid

    many = [f"SYM{i}" for i in range(bot_universe.MAX_ACTIVE_CANDIDATES)]
    monkeypatch.setattr(bot_universe, "pool_symbols", lambda: many)
    monkeypatch.setattr(hyperliquid, "supported_symbols", lambda: set(many) | {"HELD"})
    db.upsert_open_bot_position(symbol="HELD", side="long", size=1.0, entry_px=1.0,
                                leverage=1.0, stop_px=0.5, exchange_oid=None)
    result = bot.candidate_symbols()
    assert "HELD" in result
    assert len(result) == bot_universe.MAX_ACTIVE_CANDIDATES + 1


def test_get_top_market_cap_request_shape(monkeypatch):
    from data import crypto

    captured = {}

    def _fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return [{"symbol": "btc", "market_cap_rank": 1, "market_cap": 1e12,
                 "total_volume": 5e10, "current_price": 50000.0,
                 "ath_date": "2024-01-01T00:00:00.000Z",
                 "atl_date": "2020-01-01T00:00:00.000Z"}]
    monkeypatch.setattr(crypto, "_get", _fake_get)
    rows = crypto.get_top_market_cap(100)
    assert captured["path"] == "/coins/markets"
    assert captured["params"]["order"] == "market_cap_desc"
    assert "ids" not in captured["params"]
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["aeltester_datenpunkt"] == "2020-01-01T00:00:00.000Z"


def test_get_top_market_cap_degrades_on_failure(monkeypatch):
    from data import crypto

    def _boom(path, params=None):
        raise RuntimeError("network")
    monkeypatch.setattr(crypto, "_get", _boom)
    assert crypto.get_top_market_cap() == []


def test_maybe_refresh_universe_gating(tmp_db, monkeypatch):
    import bot_runner
    from core import bot_universe as bu

    calls = []
    monkeypatch.setattr(bu, "discover_candidates",
                        lambda: calls.append(1) or {"qualified": [_row("BTC")], "rejected": []})
    bot_runner._maybe_refresh_universe()
    assert len(calls) == 1
    bot_runner._maybe_refresh_universe()
    assert len(calls) == 1


def test_maybe_refresh_universe_force_flag(tmp_db, monkeypatch):
    import bot_runner
    from core import bot_universe as bu, db

    monkeypatch.setattr(bu, "discover_candidates",
                        lambda: {"qualified": [_row("BTC")], "rejected": []})
    bot_runner._maybe_refresh_universe()

    calls = []
    monkeypatch.setattr(bu, "discover_candidates",
                        lambda: calls.append(1) or {"qualified": [_row("ETH")], "rejected": []})
    db.set_meta(bot_runner._UNIVERSE_REFRESH_REQUESTED_META_KEY, "1")
    bot_runner._maybe_refresh_universe()
    assert len(calls) == 1
    assert db.get_meta(bot_runner._UNIVERSE_REFRESH_REQUESTED_META_KEY) is None
    assert bu.pool_symbols() == ["ETH"]


def test_maybe_refresh_universe_failure_logs_and_clears_force_flag(tmp_db, monkeypatch):
    import bot_runner
    from core import bot_universe as bu, db

    def _boom(force=False):
        raise RuntimeError("CoinGecko down")
    monkeypatch.setattr(bu, "refresh_pool", _boom)
    db.set_meta(bot_runner._UNIVERSE_REFRESH_REQUESTED_META_KEY, "1")
    bot_runner._maybe_refresh_universe()
    assert db.get_meta(bot_runner._UNIVERSE_REFRESH_REQUESTED_META_KEY) is None
