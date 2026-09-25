"""agents/trader.py: KI-BERICHTERSTATTUNG über den regelbasierten Bot -
run_json_agent und alle Marktdaten-Quellen sind gemockt (nie echtes
Netzwerk/API in tests/, siehe CLAUDE.md), core.bot/core.bot_guards laufen
dagegen ECHT gegen data.hyperliquid.PaperExchange, damit die eigentliche
Ausführungskette mitgeprüft wird - Vorbild tests/test_strategist.py.

Die Rolle hat sich seit Phase 8 nochmal verschoben: agents/trader.py kann
GAR NICHTS mehr auslösen (weder eröffnen, noch schliessen, noch sperren).
Es schreibt nur noch einen Bericht - Post-Mortem der zuletzt geschlossenen
Trades plus eine Erklärung des aktuellen BTC-Tagesregimes."""
import pytest

from agents import trader
from core import bot, bot_config, db
from data.hyperliquid import PaperExchange


class _Market:
    def __init__(self, prices, funding=None):
        self.prices = dict(prices)
        self.funding = dict(funding or {})

    def price(self, s):
        return self.prices.get(s)

    def rate(self, s):
        return self.funding.get(s, 0.0)


@pytest.fixture
def trader_env(tmp_db, monkeypatch):
    """Initialisierter Bot gegen PaperExchange + alle Block-Bauer von
    agents.trader auf billige Stubs gesetzt, damit run_trading_cycle-Tests
    sich auf die Ausführungslogik konzentrieren können, nicht auf die
    Marktdaten-Beschaffung (die hat eigene, gezielte Tests weiter unten)."""
    market = _Market({"BTC": 80000.0, "ETH": 2500.0})
    exchange = PaperExchange(starting_equity_usd=250.0, price_fn=market.price,
                             funding_fn=market.rate)
    bot.initialize(exchange)
    monkeypatch.setattr(trader, "_market_block", lambda: "Markt: neutral.")
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC", "ETH"])
    monkeypatch.setattr(bot, "_current_btc_regime", lambda *a, **k: "long")
    return exchange, market


def _fake_run_json_agent(parsed, usage=None, err=None):
    usage = usage if usage is not None else {"cost_usd": 0.01, "input": 100, "output": 50}
    return lambda *a, **k: (parsed, usage, err)


# --- Schema / Prompt-Bausteine ---

def test_schema_restricts_post_mortems_to_offered_trades():
    schema = trader._schema(["BTC@2026-01-01 00:00", "ETH@2026-01-02 00:00"])
    enum = schema["properties"]["post_mortems"]["items"]["properties"]["trade"]["enum"]
    assert enum == ["BTC@2026-01-01 00:00", "ETH@2026-01-02 00:00"]
    assert "DOGE" not in enum


def test_schema_has_no_actionable_field():
    """Der Kern des Rollenwechsels (Phase 8): das Schema kennt ueberhaupt
    kein 'aktion'-Feld mehr - strukturell kann die KI nichts mehr ausloesen,
    weder eroeffnen noch schliessen noch sperren."""
    schema = trader._schema(["BTC@x"])
    assert set(schema["properties"]) == {"marktausblick", "gesamtkommentar", "post_mortems"}
    pm_props = schema["properties"]["post_mortems"]["items"]["properties"]
    assert set(pm_props) == {"trade", "einschaetzung"}


def test_closed_trade_candidates_only_returns_closed_with_unique_refs(tmp_db):
    exchange = PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: 100.0,
                             funding_fn=lambda s: 0.0)
    bot.initialize(exchange)
    bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)
    bot.close_position(exchange, "BTC", reason="test")
    bot.open_position(exchange, "ETH", "long", 30.0, 1.0, 12.5)   # bleibt offen

    candidates = trader._closed_trade_candidates()
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "BTC"
    assert candidates[0]["ref"].startswith("BTC@")


def test_system_prompt_reflects_limits_and_no_advice_disclaimer():
    limits = {**bot_config.derive_limits(5), **bot_config.strategy_constants()}
    prompt = trader._system_prompt(limits, risk=5)
    assert "keine Anlageberatung" in prompt
    assert "ECHTEN" in prompt  # Abgrenzung zum virtuellen Schatten-Depot
    assert "Du triffst KEINE Entscheidung" in prompt
    assert "post_mortems" in prompt


def test_estimate_cost_positive_and_scales_with_price():
    assert trader.estimate_cost("claude-haiku-4-5") > 0
    assert trader.estimate_cost("claude-opus-4-8") > trader.estimate_cost("claude-haiku-4-5")


def test_build_prompt_contains_risk_profile(trader_env, monkeypatch):
    exchange, _ = trader_env
    db.set_meta("risk_profile", '{"risk": 7, "target_return_pct": 12.0, "retirement_year": 2055}')
    prompt = trader.build_prompt(exchange, [], "long", [])
    assert "RISIKOPROFIL DES NUTZERS" in prompt
    assert "Risikobereitschaft 7/10" in prompt


def test_build_prompt_shows_engine_signals_and_regime(trader_env):
    """Die KI sieht dieselben Signale wie die Regel-Engine, rein als
    Kontext (keine Handlungsgrundlage mehr, siehe Modul-Docstring). Seit
    der V2-Signal-Engine keine Einstiegsschwelle mehr - Signal.tradeable
    haengt an den Pflichtbedingungen, nicht an einer Punktzahl."""
    exchange, _ = trader_env
    from core import bot_signals

    signal = bot_signals.Signal(symbol="BTC", side="long", score=70.0, long_score=70.0,
                                short_score=10.0, price=80000.0, atr_pct=1.2,
                                stop_distance_pct=2.0, funding_hourly=0.0001,
                                reasons=["Kurs über MA50"])
    prompt = trader.build_prompt(exchange, [signal], "long", [])
    assert "WAS DIE REGEL-ENGINE GERADE SIEHT" in prompt
    assert "Rangfolge" in prompt
    assert "BTC" in prompt and "handelbar" in prompt
    assert "Tagesregime" in prompt and "long" in prompt


def test_positions_block_shows_empty_and_open_state(trader_env):
    exchange, market = trader_env
    assert "Keine offenen Positionen" in trader._positions_block(exchange)
    bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)
    block = trader._positions_block(exchange)
    assert "BTC" in block and "long" in block


def test_activity_block_reports_zero_trades_explicitly(tmp_db):
    """Genau der Zustand, der den Nutzer frueher gestört hat ('kein einziger
    Trade'), muss als klare Aussage sichtbar sein, nicht als Leerstelle."""
    exchange = PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: 100.0,
                             funding_fn=lambda s: 0.0)
    bot.initialize(exchange)
    block = trader._activity_block(exchange)
    assert "KEIN einziger Trade" in block


def test_history_block_shows_closed_trade_pnl_with_ref():
    trades = [{"ref": "BTC@2026-01-01 00:00", "symbol": "BTC", "side": "long",
              "entry_px": 80000.0, "close_px": 82000.0, "size": 0.001,
              "realized_pnl_usd": 2.0}]
    block = trader._history_block(trades)
    assert "BTC@2026-01-01 00:00" in block
    assert "+2.00" in block or "+2,00" in block


def test_history_block_without_trades():
    assert "keine geschlossenen" in trader._history_block([]).lower()


# --- run_trading_cycle: reine Berichterstattung, KEINE Ausführung ---

def test_run_trading_cycle_requires_initialized_bot(tmp_db, monkeypatch):
    market = _Market({"BTC": 80000.0})
    exchange = PaperExchange(starting_equity_usd=250.0, price_fn=market.price, funding_fn=market.rate)
    # bot.initialize() bewusst NICHT aufgerufen
    result = trader.run_trading_cycle(exchange, "claude-haiku-4-5")
    assert "error" in result


def test_run_trading_cycle_never_touches_open_positions(trader_env, monkeypatch):
    """Der Kern des Rollenwechsels: selbst wenn die KI einen Post-Mortem zu
    einer noch OFFENEN Position zurueckgeben wuerde, darf run_trading_cycle
    core.bot.open_position/close_position unter KEINEN Umstaenden aufrufen -
    es gibt strukturell keinen Code-Pfad mehr dorthin."""
    exchange, market = trader_env
    bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    fake = {
        "marktausblick": "Ruhiger Markt.", "gesamtkommentar": "Bot arbeitet planmässig.",
        "post_mortems": [],
    }
    monkeypatch.setattr(trader, "run_json_agent", _fake_run_json_agent(fake))

    def boom(*a, **k):
        raise AssertionError("run_trading_cycle darf keine echten Orders auslösen")
    monkeypatch.setattr(bot, "open_position", boom)
    monkeypatch.setattr(bot, "close_position", boom)

    result = trader.run_trading_cycle(exchange, "claude-haiku-4-5", candles_fn=lambda s: None)
    assert "error" not in result
    assert len(bot.open_positions()) == 1   # unveraendert


def test_run_trading_cycle_returns_post_mortems_and_logs_run(trader_env, monkeypatch):
    exchange, market = trader_env
    bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)
    market.prices["BTC"] = 82000.0
    bot.close_position(exchange, "BTC", reason="test")
    ref = trader._closed_trade_candidates()[0]["ref"]

    fake = {
        "marktausblick": "Bullenmarkt, BTC ueber MA50.", "gesamtkommentar": "Solide Ausfuehrung.",
        "post_mortems": [{"trade": ref, "einschaetzung": "Sauberer Trendfolge-Trade."}],
    }
    monkeypatch.setattr(trader, "run_json_agent", _fake_run_json_agent(fake))

    result = trader.run_trading_cycle(exchange, "claude-haiku-4-5", candles_fn=lambda s: None)
    assert "error" not in result
    assert result["post_mortems"] == fake["post_mortems"]
    assert "run_id" in result

    runs = db.list_agent_runs()
    assert runs and runs[0]["mode"] == "trading" and runs[0]["target"] == "TRADING-BOT"
    assert runs[0]["recommendation"] == "1 Post-Mortems"


def test_run_trading_cycle_error_with_cost_is_logged(trader_env, monkeypatch):
    monkeypatch.setattr(trader, "run_json_agent", _fake_run_json_agent(
        None, usage={"cost_usd": 0.02, "input": 10, "output": 5},
        err="Antwort abgeschnitten (max_tokens erreicht).",
    ))
    exchange, _ = trader_env
    result = trader.run_trading_cycle(exchange, "claude-haiku-4-5", candles_fn=lambda s: None)
    assert result["error"] == "Antwort abgeschnitten (max_tokens erreicht)."
    assert result["total_cost_usd"] == pytest.approx(0.02)
    runs = db.list_agent_runs()
    assert runs and runs[0]["cost_usd"] == pytest.approx(0.02)
    assert runs[0]["recommendation"] == "Fehlgeschlagen"


def test_run_trading_cycle_error_without_cost_skips_log(trader_env, monkeypatch):
    monkeypatch.setattr(trader, "run_json_agent", _fake_run_json_agent(
        None, usage={}, err="ANTHROPIC_API_KEY fehlt in der .env",
    ))
    exchange, _ = trader_env
    result = trader.run_trading_cycle(exchange, "claude-haiku-4-5", candles_fn=lambda s: None)
    assert result["error"] == "ANTHROPIC_API_KEY fehlt in der .env"
    assert db.list_agent_runs() == []
