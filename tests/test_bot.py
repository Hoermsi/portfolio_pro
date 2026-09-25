"""core/bot.py: die Engine gegen data.hyperliquid.PaperExchange - nie gegen
echtes Netzwerk (Preise/Funding sind injiziert, wie in test_hyperliquid.py).
Deckt den kompletten Lebenszyklus ab: initialize -> open_position (inkl.
jedes core.bot_guards-Vetos) -> Zyklus (Stop-Trigger, Equity-Boden "alles
schliessen") -> close_position -> reset."""
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core import bot, bot_config, bot_signals, clock, config, db
from data.hyperliquid import AccountState, OrderResult, PaperExchange
from data.hyperliquid import Position as HLPosition


@pytest.fixture(autouse=True)
def _default_bullish_regime(monkeypatch):
    """Ohne Tagesregime-Daten (die autouse-Netzwerksperre in conftest.py
    patcht hyperliquid.candles_df global auf None) würde core.bot_signals.
    regime() ÜBERALL auf 'neutral' zurückfallen - Gate 1 (Tagesregime) würde
    dann JEDEN Test blockieren, der überhaupt einen Trade erwartet, obwohl
    dieser Test gar nicht das Regime selbst prüfen will. Autouse-Default:
    'long', passend zu den ueberall verwendeten synthetischen Aufwärtsreihen
    (_uptrend_candles() etc.). Tests, die Regime-Verhalten SELBST prüfen
    wollen, überschreiben das gezielt (siehe z.B.
    test_run_signal_cycle_blocks_new_entries_while_a_position_exceeds_leverage_limit
    fuer ein Gegenbeispiel, das seinen eigenen Regime-Wert braucht)."""
    monkeypatch.setattr(bot_signals, "regime", lambda *a, **k: "long")


@pytest.fixture(autouse=True)
def _default_ample_liquidity(monkeypatch):
    """Dieselbe Netzwerksperre macht data.hyperliquid.day_notional_volume_usd()
    ueberall None (Gate 7, Liquiditaet) - ohne Default wuerde JEDER Test, der
    einen Trade erwartet, an einer Bedingung scheitern, die er gar nicht
    pruefen will. Autouse-Default: weit ueber bot_signals.MIN_24H_VOLUME_USD."""
    import data.hyperliquid as hl
    monkeypatch.setattr(hl, "day_notional_volume_usd", lambda *a, **k: 1e9)


@pytest.fixture(autouse=True)
def _default_generous_portfolio_risk(monkeypatch):
    """Phase 5 fuegt zwei neue, bewusst ENGE Standard-Limits ein
    (max_portfolio_heat_pct, max_same_side_positions - core.bot_guards.
    check_portfolio_heat/check_same_side_concentration). Die meisten Tests
    hier eroeffnen Positionen mit grosszuegigen, runden Test-Stop-Abstaenden
    (10-12,5 %), um etwas ANDERES zu pruefen (Order-Buchung, Nachkauf, PnL-
    Realisierung) - ohne diesen Default wuerden sie an einer Dimension
    scheitern, die sie gar nicht testen wollen. Ueber _RISK_ANCHORS (nicht
    bot_limits() direkt gepatcht), damit ein gezielter
    bot_config.save_bot_limits(...)-Override in einzelnen Tests (siehe
    test_open_position_blocks_on_portfolio_heat_limit u.a.) weiterhin greift."""
    monkeypatch.setitem(bot_config._RISK_ANCHORS, "max_portfolio_heat_pct", (20.0, 20.0, 20.0))
    monkeypatch.setitem(bot_config._RISK_ANCHORS, "max_same_side_positions", (20, 20, 20))


def _synthetic_candles(n=120, start=100.0):
    """Genug ruhige, leicht schwankende Kerzen fuer ein berechenbares ATR
    (core.bot_signals.MIN_CANDLES=90) - Werte selbst sind fuer die
    Adoptions-Tests irrelevant, nur dass ueberhaupt ein ATR herauskommt."""
    closes = start + np.sin(np.linspace(0, 12, n)) * (start * 0.01)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * 1.003
    lows = np.minimum(opens, closes) * 0.997
    index = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes,
                        "Volume": np.full(n, 1000.0)}, index=index)


class _Market:
    """Steuerbare Preis-/Funding-Quelle fuer PaperExchange, direkt manipulierbar."""

    def __init__(self, prices: dict, funding: dict | None = None):
        self.prices = dict(prices)
        self.funding = dict(funding or {})

    def price(self, symbol):
        return self.prices.get(symbol)

    def rate(self, symbol):
        return self.funding.get(symbol, 0.0)


def _exchange(equity=250.0, prices=None, funding=None):
    # prices={} muss ein LEERES Preis-Buch bedeuten (Test: kein Preis
    # verfuegbar) - "prices or {...}" wuerde das faelschlich als "nicht
    # angegeben" behandeln, da ein leeres dict falsy ist.
    if prices is None:
        prices = {"BTC": 80000.0, "ETH": 2500.0}
    market = _Market(prices, funding)
    return PaperExchange(starting_equity_usd=equity, price_fn=market.price,
                         funding_fn=market.rate), market


def _open_ok(exchange, symbol="BTC", side="long", notional=50.0, leverage=1.0, stop_px=None):
    """`stop_px` bleibt hier bewusst ein absoluter Preis (lesbarer als eine
    Prozentzahl in den Testfaellen) - intern in stop_distance_pct
    umgerechnet, weil core.bot.open_position() seit der Fill-Preis-Korrektur
    (der Stop wird ERST aus dem bestaetigten Fill berechnet, nicht vorab aus
    einem Scan-Zeitpunkt-Kurs) eine Distanz statt eines fertigen Preises
    erwartet."""
    price = exchange.mid_price(symbol)
    if stop_px is None:
        stop_px = price * 0.9 if side == "long" else price * 1.1
    stop_distance_pct = abs(price - stop_px) / price * 100
    return bot.open_position(exchange, symbol, side, notional, leverage, stop_distance_pct)


# --- LIFECYCLE ---

def test_initialize_sets_start_info_and_first_equity_point(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    info = bot.initialize(exchange)
    assert info["equity_start_usd"] == 250.0
    assert bot.is_active() is True
    assert bot.start_info()["equity_start_usd"] == 250.0
    assert db.latest_bot_equity()["equity_usd"] == 250.0
    assert info["started_at"] is not None


def test_initialize_raises_if_already_active(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    with pytest.raises(bot.BotError):
        bot.initialize(exchange)


def test_reset_clears_everything(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange)
    bot.reset()
    assert bot.is_active() is False
    assert bot.open_positions() == []
    assert db.list_bot_orders() == []
    assert db.list_bot_equity() == []
    assert bot_config.is_killed() is False


def test_reset_forces_safe_dry_run(tmp_db):
    bot_config.set_dry_run(False)
    bot.reset()
    assert bot_config.dry_run_enabled() is True


def test_reset_clears_signal_log(tmp_db, monkeypatch):
    """Seit dem Umbau auf Strategie V2 (4h-Swing) ueberlebt bot_signal_log
    einen Reset NICHT mehr - anders als zuvor. Die alte Ausnahme ("reine
    Beobachtungsdatensammlung fuer eine spaetere Lernauswertung") fuehrte
    dazu, dass 1430 Zeilen alle unter derselben strategy_version liefen,
    obwohl sich die Konfiguration darunter mehrfach geaendert hatte - als
    Lerngrundlage war das bereits vermischt und nicht mehr sauber nutzbar.
    Ein Reset ist jetzt wirklich ein Neustart, keine Teil-Loeschung."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())
    rows_before = db.list_bot_signal_log()
    assert rows_before   # der Scan hat mindestens die BTC-Zeile geloggt

    bot.reset()

    assert db.list_bot_signal_log() == []


# --- open_position: happy path ---

def test_open_long_happy_path_creates_position_and_order(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = _open_ok(exchange, side="long", notional=50.0)
    assert result["status"] == "filled"

    positions = bot.open_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTC"
    assert positions[0]["side"] == "long"
    assert positions[0]["status"] == "open"

    orders = db.list_bot_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "filled"
    assert orders[0]["intent"] == "open_long"
    assert orders[0]["position_id"] == positions[0]["id"]


def test_open_short_happy_path(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = _open_ok(exchange, side="short", notional=50.0, stop_px=88000.0)
    assert result["status"] == "filled"
    assert bot.open_positions()[0]["side"] == "short"


def test_open_position_accumulates_fees(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, notional=50.0)
    fees = float(db.get_meta("bot_fees_cum_usd"))
    assert fees == pytest.approx(50.0 * 0.00045)


def test_open_position_nachkauf_upserts_same_row(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=70000.0)
    first_id = bot.open_positions()[0]["id"]

    market.prices["BTC"] = 81000.0
    _open_ok(exchange, symbol="BTC", side="long", notional=30.0, stop_px=70000.0)

    positions = bot.open_positions()
    assert len(positions) == 1  # keine zweite Zeile
    assert positions[0]["id"] == first_id
    assert positions[0]["size"] > 50.0 / 80000.0  # Menge ist gewachsen


# --- open_position: core.bot_guards-Vetos, jedes einzeln ---

def test_open_position_blocked_without_stop_px_no_db_position(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = bot.open_position(exchange, "BTC", "long", 50.0, 1.0, stop_distance_pct=None)
    assert result["status"] == "blocked"
    assert bot.open_positions() == []
    order = db.list_bot_orders()[0]
    assert order["status"] == "blocked"
    assert order["fill_px"] is None


def test_open_position_blocked_by_kill_switch(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    bot_config.trip_kill_switch("Test")
    result = _open_ok(exchange)
    assert result["status"] == "blocked"
    assert bot.open_positions() == []


def test_open_position_blocked_in_demo_mode(tmp_db, monkeypatch):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    # DEMO_DB_PATH auf den tmp-Testpfad ziehen (statt DB_PATH auf DEMO_DB_PATH):
    # live_trading_allowed() prueft nur die Gleichheit beider Pfade - so bleibt
    # die tmp_db-Datenbank aktiv (mit bereits angelegtem Schema) und der Guard
    # sieht trotzdem "Demo-Modus aktiv".
    monkeypatch.setattr(config, "DEMO_DB_PATH", config.DB_PATH)
    result = _open_ok(exchange)
    assert result["status"] == "blocked"


def test_open_position_blocked_over_position_size_limit(tmp_db):
    exchange, _ = _exchange(equity=100.0)  # max_position_pct=35% -> 35$ erlaubt
    bot.initialize(exchange)
    result = _open_ok(exchange, notional=50.0)  # 50% > 35%
    assert result["status"] == "blocked"
    assert "%" in result["reason"]


def test_open_position_blocked_over_plausibility_limit(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    # max_reasonable = 250 * 35% * 2 = 175$
    result = _open_ok(exchange, notional=500.0)
    assert result["status"] == "blocked"
    assert "unplausibel" in result["reason"]


def test_open_position_respects_max_positions_limit(tmp_db):
    exchange, _ = _exchange(equity=1000.0, prices={
        "BTC": 80000.0, "ETH": 2500.0, "SOL": 100.0, "AVAX": 30.0, "DOT": 6.0, "ATOM": 8.0,
    })
    bot.initialize(exchange)
    # Stufe 10 = 5 Positionsplaetze (core.bot_config._RISK_ANCHORS). Die
    # Positionszahl haengt seit dem Risikoregler an der Stufe, nicht mehr an
    # einem festen Default - der Test setzt sie deshalb ausdruecklich.
    bot_config.save_risk_level(10)
    symbols = ["BTC", "ETH", "SOL", "AVAX", "DOT"]
    for sym in symbols:
        r = _open_ok(exchange, symbol=sym, notional=30.0)
        assert r["status"] == "filled", (sym, r)
    result = _open_ok(exchange, symbol="ATOM", notional=30.0)
    assert result["status"] == "blocked"
    assert "Positionen" in result["reason"]


def test_open_position_respects_daily_trade_limit(tmp_db):
    """max_trades_per_day ist seit dem Umbau eine feste Strategie-Konstante
    (core.bot_signals.MAX_TRADES_PER_DAY) - kein Override mehr moeglich, nur
    noch max_positions (siehe core/bot_config.py-Modul-Docstring)."""
    exchange, _ = _exchange(equity=1000.0, prices={
        "BTC": 80000.0, "ETH": 2500.0, "SOL": 100.0, "AVAX": 30.0, "DOT": 6.0, "ATOM": 8.0,
        "NEAR": 4.0, "LTC": 90.0, "LINK": 15.0, "XRP": 1.0, "ADA": 0.5,
    })
    bot.initialize(exchange)
    # max_positions ueberschreiben, damit dieser Test wirklich das Tageslimit
    # prueft und nicht schon vorher an der Positionsanzahl haengt.
    limit = bot_signals.MAX_TRADES_PER_DAY
    bot_config.save_bot_limits(max_positions=limit + 10)
    symbols = ["BTC", "ETH", "SOL", "AVAX", "DOT", "ATOM", "NEAR", "LTC", "LINK", "XRP"][:limit]
    for i, sym in enumerate(symbols):
        r = _open_ok(exchange, symbol=sym, notional=10.0)
        assert r["status"] == "filled", (i, sym, r)  # das feste Tageslimit
    result = _open_ok(exchange, symbol="ADA", notional=10.0)
    assert result["status"] == "blocked"
    assert "Tageslimit" in result["reason"]


def test_open_position_respects_symbol_cooldown(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    bot.close_position(exchange, "BTC", reason="test")

    result = _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    assert result["status"] == "blocked"
    assert "Cooldown" in result["reason"]


def test_open_position_blocks_on_portfolio_heat_limit(tmp_db):
    """core.bot_guards.check_portfolio_heat: Dollar-Risiko bis zum Stop, nicht
    die Nominale (check_total_exposure) - hier eng genug gesetzt, dass schon
    die erste Order darueber liegt."""
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    bot_config.save_bot_limits(max_portfolio_heat_pct=0.1)
    result = _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    assert result["status"] == "blocked"
    assert "Portfolio-Heat" in result["reason"]


def test_open_position_blocks_on_same_side_concentration(tmp_db):
    """core.bot_guards.check_same_side_concentration: mehrere gleichgerichtete
    Positionen auf verschiedenen Symbolen sind eine gebuendelte Richtungswette
    - max_positions bewusst hoch gesetzt, damit NICHT dieser (unabhaengige)
    Guard den zweiten Long blockiert."""
    exchange, _ = _exchange(equity=1000.0, prices={"BTC": 80000.0, "ETH": 2500.0})
    bot.initialize(exchange)
    bot_config.save_bot_limits(max_same_side_positions=1, max_positions=10)
    r1 = _open_ok(exchange, symbol="BTC", side="long", notional=30.0)
    assert r1["status"] == "filled", r1
    result = _open_ok(exchange, symbol="ETH", side="long", notional=30.0)
    assert result["status"] == "blocked"
    assert "gleichgerichtet" in result["reason"]


def test_open_position_blocks_after_loss_streak(tmp_db, monkeypatch):
    """core.bot_guards.check_loss_streak: bot_signals.LOSS_STREAK_LIMIT
    BOT-Verlierer in Folge (ueber verschiedene Symbole) sperren neue
    Einstiege - unabhaengig vom Symbol des naechsten Versuchs."""
    symbols = ["BTC", "ETH", "SOL", "AVAX", "DOT"]
    prices = {"BTC": 80000.0, "ETH": 2500.0, "SOL": 100.0, "AVAX": 30.0, "DOT": 6.0}
    exchange, market = _exchange(equity=1000.0, prices=prices)
    bot.initialize(exchange)
    bot_config.save_risk_level(10)   # genug Positionsplaetze fuer die Serie
    # MAX_TRADES_PER_DAY (feste Konstante, 6) reicht nicht fuer 4 Runden
    # Eroeffnung+Schliessung (= 8 Trades) plus den letzten Testversuch - der
    # Test soll die Verlustserie pruefen, nicht das Tageslimit.
    monkeypatch.setattr(bot_signals, "MAX_TRADES_PER_DAY", 20)

    for sym in symbols[:bot_signals.LOSS_STREAK_LIMIT]:
        r = _open_ok(exchange, symbol=sym, side="long", notional=10.0)
        assert r["status"] == "filled", (sym, r)
        market.prices[sym] *= 0.90   # 10 % im Minus -> garantierter Verlierer
        close = bot.close_position(exchange, sym, reason="test")
        assert close["realized_pnl_usd"] < 0, (sym, close)

    result = _open_ok(exchange, symbol=symbols[bot_signals.LOSS_STREAK_LIMIT], notional=10.0)
    assert result["status"] == "blocked"
    assert "Verlierer in Folge" in result["reason"]


def test_open_position_leverage_blocked_without_favorable_funding(tmp_db):
    exchange, _ = _exchange(equity=250.0, funding={"BTC": 0.0002})  # positiv = Longs zahlen
    bot.initialize(exchange)
    result = bot.open_position(exchange, "BTC", "long", 30.0, leverage=2.0, stop_distance_pct=12.5)
    assert result["status"] == "blocked"
    assert "Funding" in result["reason"] or "Hebel" in result["reason"]


def test_open_position_leverage_allowed_with_favorable_funding(tmp_db):
    exchange, _ = _exchange(equity=250.0, funding={"BTC": -0.0001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)   # 2x Hebel gibt es erst ganz oben am Regler
    result = bot.open_position(exchange, "BTC", "long", 30.0, leverage=2.0, stop_distance_pct=12.5)
    assert result["status"] == "filled"


def test_stop_px_follows_the_actual_fill_not_a_stale_scan_price(tmp_db):
    """DER Kernfall aus dem Review: zwischen core.bot_signals.evaluate()
    (Grundlage fuer entry_signal.price) und dem tatsaechlichen Fill koennen
    weitere Boersenaufrufe liegen, in denen sich der Kurs bewegt. Ein Stop,
    der VORHER aus dem Scan-Kurs berechnet worden waere, haette ein anderes
    Risiko abgesichert als das, auf dem position_size_usd() die
    Positionsgroesse bemessen hat. Diese Fake-Exchange liefert bei JEDEM
    mid_price()-Aufruf einen ANDEREN Kurs, simuliert also genau diese
    Bewegung zwischen zwei Boersenabfragen."""
    from data.hyperliquid import AccountState, OrderResult

    class _MovingPriceExchange:
        """Jeder mid_price()-Aufruf liefert den naechsten Wert aus der Liste -
        der erste ist der "Scan-Zeitpunkt"-Kurs (core.bot.open_position()
        fragt ihn frueh fuer entry_px_estimate ab), ein spaeterer der Kurs,
        zu dem der Markt-Order-Fill tatsaechlich passiert."""

        def __init__(self, prices):
            self._prices = list(prices)
            self._fill_px = None

        def mid_price(self, symbol):
            return self._prices.pop(0) if self._prices else self._fill_px

        def funding_rate_hourly(self, symbol):
            return None

        def account_state(self):
            positions = []
            if self._fill_px is not None:
                positions = [HLPosition(symbol="BTC", side="long", size=50.0 / self._fill_px,
                                       entry_px=self._fill_px, leverage=1.0,
                                       unrealized_pnl_usd=0.0)]
            return AccountState(equity_usd=250.0, withdrawable_usd=250.0, positions=positions)

        def open_long(self, symbol, notional_usd, leverage, stop_distance_pct):
            # Der FILL passiert zu einem Kurs, der sich vom anfangs
            # abgefragten "Scan-Kurs" unterscheidet - simuliert Bewegung
            # waehrend Guard-Pruefung und Order-Ausfuehrung.
            self._fill_px = self.mid_price(symbol)
            stop_px = self._fill_px * (1 - stop_distance_pct / 100.0)
            return OrderResult(status="filled", fill_px=self._fill_px,
                               filled_size=notional_usd / self._fill_px, fee_usd=0.01,
                               stop_oid="stop-1", leverage=leverage, stop_px=stop_px)

    # 100.0 = Scan-Kurs (fuer entry_px_estimate), 110.0 = tatsaechlicher Fill.
    exchange = _MovingPriceExchange([100.0, 110.0])
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 50.0, 1.0, stop_distance_pct=2.0)

    assert result["status"] == "filled"
    pos = bot.open_positions()[0]
    # 2 % unter dem FILL (110), nicht unter dem veralteten Scan-Kurs (100) -
    # waere der Stop faelschlich aus 100 berechnet worden, stuende hier 98.0.
    assert pos["stop_px"] == pytest.approx(110.0 * 0.98)
    assert pos["initial_stop_px"] == pytest.approx(110.0 * 0.98)


def test_open_position_stores_the_applied_not_requested_leverage(tmp_db):
    """Hyperliquid erlaubt nur Ganzzahl-Hebel - LiveExchange._ensure_leverage
    rundet 1,9x auf 1x ab. Die DB muss den TATSAECHLICH gesetzten Hebel
    zeigen, nicht den angeforderten, sonst weicht die Anzeige vom echten
    Boersen-Risiko ab."""
    exchange, _ = _exchange(equity=250.0, funding={"BTC": -0.0001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)
    result = bot.open_position(exchange, "BTC", "long", 30.0, leverage=1.9, stop_distance_pct=12.5)

    assert result["status"] == "filled"
    pos = bot.open_positions()[0]
    assert pos["leverage"] == 1.0    # abgerundet, nicht 1.9


def test_open_position_invalid_side_returns_error_not_crash(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = bot.open_position(exchange, "BTC", "sideways", 30.0, 1.0, 70000.0)
    assert result["status"] == "error"


# --- close_position ---

def test_close_position_realizes_pnl(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=70000.0)

    market.prices["BTC"] = 81000.0
    result = bot.close_position(exchange, "BTC", reason="test")
    assert result["status"] == "filled"
    assert result["realized_pnl_usd"] > 0

    positions = db.list_open_bot_positions()
    assert positions == []


def test_close_position_without_open_position_errors(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = bot.close_position(exchange, "BTC", reason="test")
    assert result["status"] == "error"


def test_close_position_still_works_during_kill_switch(tmp_db):
    """Kill-Switch stoppt neue Einstiege, aber core.bot_guards.evaluate_exit_order
    laesst Schliessungen bewusst durch - siehe core/bot_guards.py."""
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    bot_config.trip_kill_switch("Test")
    result = bot.close_position(exchange, "BTC", reason="test")
    assert result["status"] == "filled"


def test_close_position_still_blocked_in_demo_mode(tmp_db, monkeypatch):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    monkeypatch.setattr(config, "DEMO_DB_PATH", config.DB_PATH)
    result = bot.close_position(exchange, "BTC", reason="test")
    assert result["status"] == "blocked"


# --- close_all_positions(): manueller "Alle Positionen schließen"-Knopf ---

def test_close_all_positions_closes_every_open_position(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    _open_ok(exchange, symbol="ETH", notional=30.0, stop_px=2000.0)
    assert len(bot.open_positions()) == 2

    results = bot.close_all_positions(exchange)

    assert {r["symbol"] for r in results} == {"BTC", "ETH"}
    assert all(r["result"]["status"] == "filled" for r in results)
    assert bot.open_positions() == []
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "manual_close_all" for d in decisions)


def test_close_all_positions_one_failure_does_not_block_the_others(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    _open_ok(exchange, symbol="ETH", notional=30.0, stop_px=2000.0)
    del market.prices["BTC"]  # PaperExchange.close_position scheitert ohne Preis

    results = bot.close_all_positions(exchange)

    by_symbol = {r["symbol"]: r["result"] for r in results}
    assert by_symbol["BTC"]["status"] == "error"
    assert by_symbol["ETH"]["status"] == "filled"
    assert [p["symbol"] for p in bot.open_positions()] == ["BTC"]


def test_close_all_positions_without_any_open_position_logs_nothing(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    results = bot.close_all_positions(exchange)
    assert results == []
    assert not any(d["action"] == "manual_close_all" for d in db.list_bot_decisions())


# --- deterministischer Zyklus ---

def test_cycle_triggers_stop_and_closes_position(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=30.0, stop_px=75000.0)

    market.prices["BTC"] = 74000.0  # unter dem Stop
    result = bot.run_deterministic_cycle(exchange)

    assert len(result["stop_events"]) == 1
    assert result["stop_events"][0]["symbol"] == "BTC"
    assert db.list_open_bot_positions() == []
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "stop_triggered" for d in decisions)


def test_cycle_no_stop_when_price_above_threshold(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=30.0, stop_px=70000.0)
    result = bot.run_deterministic_cycle(exchange)
    assert result["stop_events"] == []
    assert len(db.list_open_bot_positions()) == 1


def test_cycle_records_equity_point(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    before = len(db.list_bot_equity())
    bot.run_deterministic_cycle(exchange)
    assert len(db.list_bot_equity()) == before + 1


def test_cycle_applies_funding_for_paper_positions(tmp_db):
    exchange, market = _exchange(equity=250.0, funding={"BTC": 0.0})  # zunaechst neutral
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=70000.0, leverage=1.0)
    equity_before = exchange.account_state().equity_usd

    market.funding["BTC"] = 0.001  # 0,1%/h - Long zahlt jetzt drauf
    bot.run_deterministic_cycle(exchange, interval_hours=1.0)
    equity_after = exchange.account_state().equity_usd

    assert equity_after < equity_before
    funding_cum = float(db.get_meta("bot_funding_cum_usd"))
    assert funding_cum > 0


def _seed_large_paper_position(exchange, symbol="BTC", notional=200.0, entry_px=80000.0):
    """Legt eine ueberproportional grosse Position direkt in Exchange UND DB an -
    umgeht bewusst core.bot_guards (die haben eigene, dedizierte Tests). Hier
    geht es nur um die Reaktion von run_deterministic_cycle auf einen bereits
    unterschrittenen Equity-Boden, nicht um die Entry-Guards selbst.

    Groesse wird NACH dem Fill aus dem Kontostand gelesen (Ground Truth),
    nicht naiv aus notional/entry_px vorausberechnet - PaperExchange wendet
    beim Fill Slippage an (siehe data.hyperliquid.slipped_price), die
    tatsaechliche interne Groesse weicht von der naiven Rechnung deshalb um
    einen kleinen, aber realen Betrag ab. Genau dieselbe Differenz wuerde
    core.bot.open_position() ueber _find_position() ebenfalls als Ground
    Truth uebernehmen - eine hier abweichend seedende DB-Zeile taeuschte
    einen spaeteren Schliess-Aufruf faelschlich als Teilschliessung vor."""
    exchange.open_long(symbol, notional_usd=notional, leverage=1.0, stop_distance_pct=1.0)
    fresh = next(p for p in exchange.account_state().positions if p.symbol == symbol)
    db.upsert_open_bot_position(symbol=symbol, side="long", size=fresh.size,
                                entry_px=entry_px, leverage=1.0, stop_px=1.0, exchange_oid=None)


def test_cycle_equity_floor_needs_two_confirmations_before_closing(tmp_db):
    """core.bot.equity_reading()'s Plausibilitaetsbremse greift hier NICHT
    (Faktor 0.4, weit ueber der 0.2-Schwelle) - das ist ein echter, wenn auch
    extremer Handelsverlust, kein Datenfehler. Trotzdem darf die ERSTE
    Boden-Messung allein noch nichts zwangsschliessen (_EQUITY_FLOOR_
    CONFIRMATIONS) - genau eine einzelne Messung hat den realen Vorfall
    ausgeloest."""
    exchange, market = _exchange(equity=250.0)
    bot.initialize(exchange)
    _seed_large_paper_position(exchange, notional=200.0, entry_px=80000.0)

    market.prices["BTC"] = 20000.0  # -75% -> Equity faellt auf ~40% des Starts
    first = bot.run_deterministic_cycle(exchange)
    assert first["kill_switch_active"] is False
    assert len(db.list_open_bot_positions()) == 1
    assert db.get_meta("bot_equity_floor_hits") == "1"

    second = bot.run_deterministic_cycle(exchange)

    assert second["kill_switch_active"] is True
    assert db.list_open_bot_positions() == []
    assert bot_config.is_killed() is True
    info = bot_config.kill_switch_info()
    assert "Boden" in info["reason"]


def test_cycle_equity_floor_hits_reset_on_recovery(tmp_db):
    exchange, market = _exchange(equity=250.0)
    bot.initialize(exchange)
    _seed_large_paper_position(exchange, notional=200.0, entry_px=80000.0)

    market.prices["BTC"] = 20000.0
    bot.run_deterministic_cycle(exchange)
    assert db.get_meta("bot_equity_floor_hits") == "1"

    market.prices["BTC"] = 80000.0  # Preis erholt sich vor der zweiten Messung
    bot.run_deterministic_cycle(exchange)

    assert db.get_meta("bot_equity_floor_hits") is None
    assert bot_config.is_killed() is False
    assert len(db.list_open_bot_positions()) == 1


def test_cycle_after_kill_switch_new_entries_stay_blocked(tmp_db):
    exchange, market = _exchange(equity=250.0)
    bot.initialize(exchange)
    _seed_large_paper_position(exchange, notional=200.0, entry_px=80000.0)
    market.prices["BTC"] = 20000.0
    bot.run_deterministic_cycle(exchange)
    bot.run_deterministic_cycle(exchange)  # zweite Bestaetigung loest den Boden aus

    market.prices["BTC"] = 80000.0  # Erholung aendert nichts - Sperrklinke
    result = _open_ok(exchange, symbol="ETH", notional=10.0)
    assert result["status"] == "blocked"


# --- Phase 3: Entscheidungstakt (nur bei neu geschlossener 4h-Kerze) ---

def _four_hour_candles(n=10, closed_at="2026-01-01", freq="4h"):
    """Tz-aware wie data.hyperliquid.candles_df() es seit dem UTC-Umbau
    liefert - die letzte Zeile ist die noch LAUFENDE Kerze, exakt wie dort."""
    idx = pd.date_range(closed_at, periods=n, freq=freq, tz="UTC")
    closes = np.full(n, 100.0)
    return pd.DataFrame({"Open": closes, "High": closes * 1.001, "Low": closes * 0.999,
                        "Close": closes, "Volume": np.full(n, 1.0)}, index=idx)


def test_due_for_decision_true_on_first_call(tmp_db):
    df = _four_hour_candles()
    candle_ts = bot._due_for_decision(decision_candles_fn=lambda s: df)
    assert candle_ts == str(df.index[-2])


def test_due_for_decision_false_once_marker_set(tmp_db):
    df = _four_hour_candles()
    candle_ts = bot._due_for_decision(decision_candles_fn=lambda s: df)
    db.set_meta("bot_last_decision_candle", candle_ts)
    assert bot._due_for_decision(decision_candles_fn=lambda s: df) is None


def test_due_for_decision_true_after_a_new_candle_closes(tmp_db):
    old_df = _four_hour_candles(n=10)
    db.set_meta("bot_last_decision_candle", str(old_df.index[-2]))
    new_df = _four_hour_candles(n=11)   # eine Kerze weiter
    assert bot._due_for_decision(decision_candles_fn=lambda s: new_df) == str(new_df.index[-2])


def test_due_for_decision_none_on_missing_or_thin_data(tmp_db):
    assert bot._due_for_decision(decision_candles_fn=lambda s: None) is None
    assert bot._due_for_decision(decision_candles_fn=lambda s: _four_hour_candles(n=1)) is None

    def _raises(s):
        raise RuntimeError("kein Netz")
    assert bot._due_for_decision(decision_candles_fn=_raises) is None


def test_deterministic_cycle_skips_decision_layer_without_new_candle(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    df = _four_hour_candles()
    db.set_meta("bot_last_decision_candle", str(df.index[-2]))  # bereits gesehen

    result = bot.run_deterministic_cycle(exchange, decision_candles_fn=lambda s: df)

    assert result["decision_candle"] is None
    assert result["signals"]["entry"] is None
    assert "4h" in result["signals"]["skipped"]
    assert result["trailing_updates"] == []
    # Keine Signal-Entscheidung geschrieben - der Sicherheits-Teil (Equity-
    # Punkt) laeuft trotzdem, aber signal_scan/signal_entry_* nicht.
    actions = {d["action"] for d in db.list_bot_decisions()}
    assert "signal_scan" not in actions


def test_deterministic_cycle_runs_decision_layer_on_new_candle(tmp_db, monkeypatch):
    exchange, _ = _exchange(equity=1000.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    df_4h = _four_hour_candles()

    result = bot.run_deterministic_cycle(
        exchange, candles_fn=lambda s: _uptrend_candles(),
        decision_candles_fn=lambda s: df_4h)

    assert result["decision_candle"] == str(df_4h.index[-2])
    assert result["signals"]["entry"] is not None
    assert result["signals"]["entry"]["symbol"] == "BTC"
    assert db.get_meta("bot_last_decision_candle") == str(df_4h.index[-2])


def test_deterministic_cycle_does_not_consume_decision_budget_on_failure(tmp_db, monkeypatch):
    """Ein voruebergehender Fehler im Entscheidungs-Teil (dasselbe Prinzip wie
    bot_runner._maybe_refresh_costs' Tages-Gate) darf die einzige
    Entscheidungsgelegenheit dieses 4h-Fensters nicht verbrauchen - sonst
    haette ein einzelner Ausfall ein komplettes Fenster gekostet."""
    exchange, _ = _exchange()
    bot.initialize(exchange)
    df_4h = _four_hour_candles()

    def _broken_scan(*a, **k):
        raise RuntimeError("Boersenfehler")
    monkeypatch.setattr(bot.bot_signals, "scan", _broken_scan)

    result = bot.run_deterministic_cycle(
        exchange, candles_fn=lambda s: None, decision_candles_fn=lambda s: df_4h)

    assert "error" in result["signals"]
    assert db.get_meta("bot_last_decision_candle") is None


# --- Phase 3: Signal-Reset (Wiedereinstieg nur nach nachweislich totem Setup) ---

def test_reentry_allowed_for_a_symbol_that_was_never_closed(tmp_db):
    signal = bot_signals.Signal(symbol="BTC", side="long", score=80.0)
    assert bot._reentry_check("BTC", signal, clock.now_utc()) is None


def test_reentry_blocked_immediately_after_close(tmp_db):
    db.disarm_bot_symbol("BTC", "long")
    signal = bot_signals.Signal(symbol="BTC", side="short", score=80.0)
    reason = bot._reentry_check("BTC", signal, clock.now_utc())
    assert reason is not None
    assert "Signal-Reset" in reason


def test_reentry_blocked_after_min_wait_while_setup_still_active(tmp_db):
    disarmed_at = clock.now_utc() - timedelta(hours=5)
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))
    # readings nicht leer: eine ECHTE Neubewertung, kein Datenfehler (siehe
    # test_reentry_stays_blocked_on_a_data_error unten fuer den Datenfehler-Fall).
    still_long = bot_signals.Signal(symbol="BTC", side="long", score=80.0,
                                    readings={"price": 100.0})
    reason = bot._reentry_check("BTC", still_long, clock.now_utc())
    assert reason is not None
    assert "weiterhin aktiv" in reason
    # Bleibt disarmed - keine stillschweigende Freigabe.
    assert db.get_bot_symbol_state("BTC")["armed"] == 0


def test_reentry_allowed_once_setup_reversed_after_min_wait(tmp_db):
    disarmed_at = clock.now_utc() - timedelta(hours=5)
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))
    reversed_signal = bot_signals.Signal(symbol="BTC", side="short", score=80.0,
                                         readings={"price": 100.0})
    assert bot._reentry_check("BTC", reversed_signal, clock.now_utc()) is None
    assert db.get_bot_symbol_state("BTC")["armed"] == 1


def test_reentry_allowed_once_setup_no_longer_tradeable_after_min_wait(tmp_db):
    disarmed_at = clock.now_utc() - timedelta(hours=5)
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))
    dead = bot_signals.Signal(symbol="BTC", side="long", score=0.0, blocked_by="kein Trend",
                              readings={"price": 100.0})
    assert bot._reentry_check("BTC", dead, clock.now_utc()) is None


def test_reentry_stays_blocked_on_a_data_error(tmp_db):
    """Ein blosser Kursdatenfehler ist KEIN Nachweis, dass das Setup weg ist
    (reproduziert von einer externen Pruefung, 13.09.2026): core.bot_signals.
    evaluate() liefert bei zu wenigen/nicht abrufbaren Kursdaten ein Signal
    mit side=None UND leerem readings - vorher entsperrte `signal.side ==
    last_side` (sofort False bei side=None) das Symbol trotzdem."""
    disarmed_at = clock.now_utc() - timedelta(hours=5)
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))
    data_error = bot_signals.Signal(symbol="BTC",
                                    blocked_by="Zu wenige Kerzen (mindestens 90 nötig).")
    assert data_error.readings == {}
    reason = bot._reentry_check("BTC", data_error, clock.now_utc())
    assert reason is not None
    assert "auswertbare Kursdaten" in reason
    # Bleibt disarmed - derselbe Grundsatz wie beim "weiterhin aktiv"-Fall oben.
    assert db.get_bot_symbol_state("BTC")["armed"] == 0


def test_reentry_never_blocks_permanently_on_a_corrupt_timestamp(tmp_db):
    db.disarm_bot_symbol("BTC", "long", disarmed_at="nicht-geparsed")
    signal = bot_signals.Signal(symbol="BTC", side="long", score=80.0)
    assert bot._reentry_check("BTC", signal, clock.now_utc()) is None
    assert db.get_bot_symbol_state("BTC")["armed"] == 1


def test_close_position_disarms_the_symbol(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=30.0, stop_px=70000.0)
    bot.close_position(exchange, "BTC", reason="test")

    state = db.get_bot_symbol_state("BTC")
    assert state["armed"] == 0
    assert state["last_side"] == "long"


def test_run_signal_cycle_blocks_immediate_reentry_after_close(tmp_db, monkeypatch):
    """Derselbe Aufwaertstrend, der BTC gerade zum Handeln gebracht hat, darf
    unmittelbar nach dem Schluss NICHT sofort wieder dieselbe Position
    eroeffnen - genau der Fall aus dem CLAUDE.md-Feedback (17 Wiedereinstiege
    in dasselbe Symbol innerhalb von 24h)."""
    exchange, _ = _exchange(equity=1000.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    db.disarm_bot_symbol("BTC", "long")   # gerade erst geschlossen

    result = bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    assert result["entry"] is None
    rows = db.list_bot_signal_log()
    btc_rows = [r for r in rows if r["symbol"] == "BTC"]
    assert btc_rows and btc_rows[0]["action"] == "disarmed"


def test_run_signal_cycle_rearms_a_symbol_once_its_setup_is_no_longer_tradeable(tmp_db, monkeypatch):
    """Bugfix-Regressionstest: `_reentry_check()` selbst erlaubt ein Rearm
    schon bei `not signal.tradeable` (siehe
    test_reentry_allowed_once_setup_no_longer_tradeable_after_min_wait) -
    aber die Kandidatenschleife in run_signal_cycle() rief sie fuer genau
    diesen Fall nie auf: `if ... or not signal.tradeable: continue` sortierte
    ein nicht handelbares Signal schon VOR dem _reentry_check()-Aufruf aus.
    Bei ALLOW_SHORT=False (die einzige Seite ist 'long') bedeutete das: der
    einzige Fall, der ueberhaupt bis zu _reentry_check() kam
    (tradeable=True, side='long'), war exakt der Fall, den sie blockiert -
    ein disarmed Symbol konnte sich auf normalem Weg nie wieder scharf
    stellen. Dieser Test faehrt den GANZEN Zyklus (nicht nur _reentry_check
    direkt) und beweist das Rearm end-to-end."""
    exchange, _ = _exchange(equity=1000.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    disarmed_at = clock.now_utc() - timedelta(hours=5)   # laengst ueber der 4h-Wartezeit
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))

    # Regime bleibt global 'long' (autouse-Fixture); BTC selbst hat aber
    # einen Abwaertstrend -> Gate 2 blockt -> signal.tradeable=False, genau
    # der Fall, den _reentry_check() als "Setup nachweislich weg" werten
    # soll.
    result = bot.run_signal_cycle(exchange, candles_fn=lambda s: _downtrend_candles())

    assert result["entry"] is None
    assert db.get_bot_symbol_state("BTC")["armed"] == 1


def test_run_signal_cycle_rearms_a_symbol_even_while_all_position_slots_are_full(tmp_db, monkeypatch):
    """Zweiter Teil desselben Bugfixes wie der Test oben (externe Pruefung,
    13.09.2026): _reentry_check() lief vorher NUR innerhalb der `else`-
    Verzweigung der Einstiegspruefung - komplett uebersprungen, sobald
    z.B. bereits alle Positionsplaetze belegt sind (der Slot-Guard greift
    dann VOR dieser Verzweigung). Ein Symbol, dessen Setup sich waehrend
    genau dieser Zeit aufloest, verpasste dadurch seine eigene Freischaltung
    - nicht erst, weil kein Kandidat gewinnt, sondern weil die
    Wiedereinstiegspruefung fuer dieses Symbol ueberhaupt nie lief."""
    exchange, _ = _exchange(equity=1000.0, prices={"BTC": 100.0, "SOL": 50.0})
    bot.initialize(exchange)
    bot_config.save_bot_limits(max_positions=1)
    _open_ok(exchange, symbol="SOL", side="long", notional=30.0, stop_px=40.0)   # belegt den einzigen Slot
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    disarmed_at = clock.now_utc() - timedelta(hours=5)   # laengst ueber der 4h-Wartezeit
    db.disarm_bot_symbol("BTC", "long", disarmed_at=clock.iso_utc(disarmed_at))

    # BTC selbst im Abwaertstrend -> Gate 2 blockt -> signal.tradeable=False,
    # genau der Fall, den _reentry_check() als "Setup nachweislich weg"
    # werten soll - unabhaengig davon, dass der Slot-Guard (max_positions=1,
    # SOL belegt ihn bereits) jeden Neueinstieg ohnehin verhindert haette.
    # SOL selbst bekommt bewusst EINEN Aufwaertstrend (nicht denselben
    # Abwaertstrend wie BTC): Schritt 1 in run_signal_cycle() prueft aktive
    # Ausstiege fuer JEDE offene Position anhand IHRER EIGENEN Kerzen - ein
    # gemeinsamer Abwaertstrend haette SOLs eigenen Ausstieg (Kurs/MA20/MA50)
    # ausgeloest und den Slot schon VOR der eigentlichen Testpruefung
    # geraeumt, unabhaengig vom hier geprueften Bug.
    def _candles_for(symbol):
        return _downtrend_candles() if symbol == "BTC" else _uptrend_candles()

    result = bot.run_signal_cycle(exchange, candles_fn=_candles_for)

    assert result["entry"] is None
    assert "belegt" in (result["skipped"] or "")   # Slot-Guard greift wie erwartet
    assert db.get_bot_symbol_state("BTC")["armed"] == 1


def test_clear_bot_state_resets_symbol_state_and_decision_marker(tmp_db):
    db.disarm_bot_symbol("BTC", "long")
    db.set_meta("bot_last_decision_candle", "2026-01-01 00:00:00+00:00")

    bot.reset()

    assert db.get_bot_symbol_state("BTC") is None
    assert db.get_meta("bot_last_decision_candle") is None


# --- Sonderfaelle rund um Preise/Equity ---

def test_open_position_without_price_never_fills(tmp_db):
    """Ohne Preis kann core.bot_guards.check_stop_loss_present den Stop nicht
    gegen einen Einstiegskurs validieren (entry_px faellt auf 0.0 zurueck) und
    blockiert deshalb schon vor dem Exchange-Aufruf - so oder so darf NIE eine
    Position entstehen."""
    exchange, market = _exchange(prices={})  # kein Preis fuer BTC bekannt
    bot.initialize(exchange)
    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 70000.0)
    assert result["status"] != "filled"
    assert bot.open_positions() == []


# --- Notausstieg bei fehlgeschlagener Stop-Order (LiveExchange-Sonderfall) ---
#
# PaperExchange kann "Eroeffnung gefuellt, aber Stop fehlgeschlagen" nie
# erzeugen (sie setzt .error nie auf einem erfolgreichen Fill) - dieser Fall
# ist ausschliesslich data.hyperliquid.LiveExchange._place_entry_and_stop
# vorbehalten. Fuer eine ungesicherte, gehebelte Position mit echtem Geld ist
# das trotzdem der sicherheitskritischste Pfad in core.bot.open_position -
# deshalb hier mit einem dedizierten Fake nachgebildet statt PaperExchange.

class _NakedStopExchange:
    """Simuliert exakt den LiveExchange-Sonderfall aus data/hyperliquid.py:
    open_long() liefert status='filled' UND ein error (Stop-Order
    fehlgeschlagen). close_succeeds steuert, ob der anschliessende
    Notausstieg (core.bot.close_position) selbst gelingt."""

    def __init__(self, close_succeeds: bool):
        self._close_succeeds = close_succeeds
        self._open = False
        self.close_calls = 0

    def account_state(self):
        positions = [HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, unrealized_pnl_usd=0.0)] if self._open else []
        return AccountState(equity_usd=250.0, withdrawable_usd=250.0, positions=positions)

    def mid_price(self, symbol):
        return 80000.0

    def funding_rate_hourly(self, symbol):
        return 0.0

    def open_long(self, symbol, notional_usd, leverage, stop_px):
        self._open = True
        return OrderResult(status="filled", fill_px=80000.0, filled_size=0.001, fee_usd=0.036,
                           error="EROEFFNET, aber Stop-Order fehlgeschlagen: ambiguous")

    def open_short(self, symbol, notional_usd, leverage, stop_px):
        raise AssertionError("in diesem Test nicht erwartet")

    def close_position(self, symbol):
        self.close_calls += 1
        if self._close_succeeds:
            self._open = False
            return OrderResult(status="filled", fill_px=80000.0, filled_size=0.001, fee_usd=0.036)
        return OrderResult(status="error", error="Börse nicht erreichbar")


def test_open_position_emergency_closes_naked_position_when_stop_fails(tmp_db):
    exchange = _NakedStopExchange(close_succeeds=True)
    bot.initialize(exchange)
    assert bot_config.first_live_order_pending() is True

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert result["status"] == "error"
    assert "sofort geschlossen" in result["reason"]
    assert exchange.close_calls == 1
    # Der Notausstieg hat tatsaechlich gegriffen - keine offene, ungesicherte
    # Position darf danach in der DB stehen bleiben.
    assert bot.open_positions() == []
    # DER Kernfall aus dem Review: der Sicherheitsnachweis der gedeckelten
    # ersten Live-Order gilt NICHT als erbracht, wenn die Stop-Order
    # fehlschlug - selbst wenn der Notausstieg sie noch rettet. Sonst wuerde
    # die naechste Order bereits mit der vollen (ungedeckelten)
    # Positionsgroesse laufen, obwohl der Nachweis gerade gescheitert ist.
    assert bot_config.first_live_order_pending() is True


def test_open_position_reports_loudly_when_emergency_close_also_fails(tmp_db):
    exchange = _NakedStopExchange(close_succeeds=False)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert result["status"] == "error"
    assert "NOTAUSSTIEG FEHLGESCHLAGEN" in result["reason"]
    # Schlimmster Fall: die Position bleibt ungesichert offen - das MUSS
    # sichtbar in der DB bleiben, damit ein Mensch manuell eingreifen kann,
    # statt in einem "status: error" String zu verschwinden.
    assert len(bot.open_positions()) == 1
    # Auch hier: kein Sicherheitsnachweis erbracht, der Deckel bleibt aktiv.
    assert bot_config.first_live_order_pending() is True


class _CleanLiveExchange:
    """Live-artige (kein apply_funding) Boerse, bei der Eroeffnung UND Stop
    anstandslos gelingen - fuer die Gegenprobe zum Naked-Stop-Fall."""

    def __init__(self, price=80000.0, foreign_stop_orders=None):
        self._price = price
        self._open = False
        self._foreign_stop_orders = list(foreign_stop_orders or [])
        self.cancelled = []

    def account_state(self):
        positions = [HLPosition(symbol="BTC", side="long", size=0.001, entry_px=self._price,
                               leverage=1.0, unrealized_pnl_usd=0.0)] if self._open else []
        return AccountState(equity_usd=250.0, withdrawable_usd=250.0, positions=positions)

    def mid_price(self, symbol):
        return self._price

    def funding_rate_hourly(self, symbol):
        return 0.0

    def open_long(self, symbol, notional_usd, leverage, stop_distance_pct):
        self._open = True
        stop_px = self._price * (1 - stop_distance_pct / 100.0)
        return OrderResult(status="filled", fill_px=self._price, filled_size=0.001,
                          fee_usd=0.036, stop_oid="stop-1", leverage=leverage, stop_px=stop_px)

    def open_short(self, symbol, notional_usd, leverage, stop_distance_pct):
        raise AssertionError("in diesem Test nicht erwartet")

    def frontend_open_orders(self):
        return self._foreign_stop_orders

    def cancel_order(self, symbol, oid):
        self.cancelled.append((symbol, oid))
        return True


def test_open_position_sweeps_orphaned_foreign_stops_on_the_symbol(tmp_db):
    """Review-Punkt 6 (Runde 3): LiveExchange.replace_stop_order() schluckt
    einen fehlgeschlagenen Storno des ALTEN Stops beim Nachziehen best
    effort (siehe dessen Docstring) - die DB verliert dessen OID komplett.
    Eine NEUE Position im selben Symbol muss solche verwaisten Trigger-Orders
    trotzdem finden und stornieren, sonst koennte der alte Stop sie zu einem
    voelling unbeteiligten Kurs schliessen."""
    orphan = {"coin": "BTC", "oid": "orphan-stop-42", "isTrigger": True}
    exchange = _CleanLiveExchange(foreign_stop_orders=[orphan])
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert result["status"] == "filled"
    assert ("BTC", "orphan-stop-42") in exchange.cancelled
    # der GERADE gesetzte eigene Stop darf nicht versehentlich mitstorniert
    # werden, selbst wenn er (Zufall der Test-OIDs) in derselben Liste laege.
    own_stop_still_open = "stop-1" not in [oid for _, oid in exchange.cancelled]
    assert own_stop_still_open


def test_open_position_marks_first_live_order_done_only_on_full_success(tmp_db):
    """Gegenprobe: laeuft alles glatt (Fill, Stop OHNE Fehler, DB-Eintrag),
    gilt der Nachweis als erbracht und der Deckel faellt."""
    exchange = _CleanLiveExchange()
    bot.initialize(exchange)
    assert bot_config.first_live_order_pending() is True

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert result["status"] == "filled"
    assert bot_config.first_live_order_pending() is False


# --- Reconciliation, Stop-Stornierung, exchange_kind, echte Kosten ---
#
# Alle live-spezifischen Verhalten (Reconciliation, cancel_order,
# exchange_kind="live", refresh_live_costs) sind mit PaperExchange nicht
# erzeugbar - sie hat weder ein unabhaengiges Boersen-Gedaechtnis noch die
# Live-Methoden. Deshalb ein dediziertes Fake, das core.bot NICHT als Paper
# erkennt (kein apply_funding), mit voll steuerbarem Boersen-Zustand.

class _FakeLiveExchange:
    def __init__(self, equity_usd=250.0, positions=None, prices=None,
                close_fails=False, fills=None, funding_total=0.0, flows_total=0.0,
                equity_trusted=True, stop_orders_supported=False, stop_fails=False):
        self.equity_usd = equity_usd
        self.positions = list(positions or [])
        self.prices = dict(prices or {})
        self.close_fails = close_fails
        self.cancelled = []
        self.close_calls = []
        self._fills = fills if fills is not None else []
        self._funding_total = funding_total
        self.funding_since_calls = []
        self._flows_total = flows_total
        self.flows_since_calls = []
        self.equity_trusted = equity_trusted
        self._stop_fails = stop_fails
        self.stop_orders_placed = []
        if not stop_orders_supported:
            # replace_stop_order ist KEIN Teil von ExchangeProtocol (siehe
            # data.hyperliquid.LiveExchange.replace_stop_order-Docstring) -
            # der Duck-Typing-Check in core.bot.adopt_unknown_positions()
            # muss sein Fehlen sauber erkennen, wie bei apply_funding/
            # cancel_order. Instanz-Attribut statt Klassen-Attribut, damit
            # NUR diese Instanz die Methode "verliert" - ein del auf der
            # Klasse wuerde sie fuer alle anderen Tests im selben Lauf
            # ebenfalls entfernen.
            self.replace_stop_order = None

    def account_state(self):
        return AccountState(equity_usd=self.equity_usd, withdrawable_usd=self.equity_usd,
                            positions=list(self.positions), equity_trusted=self.equity_trusted)

    def mid_price(self, symbol):
        return self.prices.get(symbol)

    def funding_rate_hourly(self, symbol):
        return 0.0

    def open_long(self, symbol, notional_usd, leverage, stop_px):
        raise AssertionError("in diesem Test nicht erwartet")

    def open_short(self, symbol, notional_usd, leverage, stop_px):
        raise AssertionError("in diesem Test nicht erwartet")

    def close_position(self, symbol):
        self.close_calls.append(symbol)
        if self.close_fails:
            return OrderResult(status="error", error="Börse nicht erreichbar")
        price = self.prices.get(symbol, 0.0)
        self.positions = [p for p in self.positions if p.symbol != symbol]
        return OrderResult(status="filled", fill_px=price, filled_size=0.001, fee_usd=0.01)

    def replace_stop_order(self, symbol, side, size, stop_px, old_oid):
        if self._stop_fails:
            return None, f"{symbol}: Stop-Platzierung fehlgeschlagen (Test)."
        self.stop_orders_placed.append((symbol, side, size, stop_px, old_oid))
        return f"stop-{symbol}", None

    def cancel_order(self, symbol, oid):
        self.cancelled.append((symbol, oid))
        return True

    def recent_fills(self, since_ms):
        return self._fills

    def funding_payments_usd(self, since_ms):
        self.funding_since_calls.append(since_ms)
        return self._funding_total

    def deposit_withdrawal_usd(self, since_ms):
        self.flows_since_calls.append(since_ms)
        return self._flows_total


# --- Nachgezogene Stops: lokaler Wunsch != bestaetigter Exchange-Stop ---

def test_trailing_stop_accumulates_small_moves_against_confirmed_exchange_stop(tmp_db, monkeypatch):
    exchange = _FakeLiveExchange(prices={"BTC": 120.0}, stop_orders_supported=True)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                                leverage=1.0, stop_px=90.0, exchange_stop_px=90.0,
                                exchange_oid="old-stop")
    proposed = iter((90.3, 90.6))  # einzeln < 0,5 %, zusammen > 0,5 %
    monkeypatch.setattr(bot.bot_signals, "trailing_stop_px",
                        lambda *args, **kwargs: next(proposed))

    first = bot.update_trailing_stops(exchange, candles_fn=lambda symbol: object())
    pos = db.get_bot_position("BTC")
    assert first[0]["exchange_updated"] is False
    assert pos["stop_px"] == pytest.approx(90.3)
    assert pos["exchange_stop_px"] == pytest.approx(90.0)

    second = bot.update_trailing_stops(exchange, candles_fn=lambda symbol: object())
    pos = db.get_bot_position("BTC")
    assert second[0]["exchange_updated"] is True
    assert exchange.stop_orders_placed[0][-2] == pytest.approx(90.6)
    assert pos["stop_px"] == pytest.approx(90.6)
    assert pos["exchange_stop_px"] == pytest.approx(90.6)
    assert pos["exchange_oid"] == "stop-BTC"


def test_trailing_stop_retries_after_exchange_failure(tmp_db, monkeypatch):
    exchange = _FakeLiveExchange(prices={"BTC": 120.0}, stop_orders_supported=True,
                                 stop_fails=True)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                                leverage=1.0, stop_px=90.0, exchange_stop_px=90.0,
                                exchange_oid="old-stop")
    monkeypatch.setattr(bot.bot_signals, "trailing_stop_px",
                        lambda *args, **kwargs: 99.0)

    bot.update_trailing_stops(exchange, candles_fn=lambda symbol: object())
    pos = db.get_bot_position("BTC")
    assert pos["stop_px"] == pytest.approx(99.0)
    assert pos["exchange_stop_px"] == pytest.approx(90.0)

    exchange._stop_fails = False
    monkeypatch.setattr(bot.bot_signals, "trailing_stop_px",
                        lambda *args, **kwargs: None)
    retried = bot.update_trailing_stops(exchange, candles_fn=lambda symbol: object())
    pos = db.get_bot_position("BTC")
    assert retried[0]["exchange_updated"] is True
    assert len(exchange.stop_orders_placed) == 1
    assert pos["exchange_stop_px"] == pytest.approx(99.0)


def test_open_position_blocked_when_runner_degraded(tmp_db):
    """Mehrere Zyklen in Folge fehlgeschlagen (core.bot_config.
    runner_degraded()) - core.bot_guards.check_runner_health() sperrt neue
    Einstiege, bis wieder ein Zyklus sauber durchlaeuft. Bestehende
    Positionen/Stops sind davon unberuehrt (nur evaluate_entry_order, nicht
    evaluate_adoption/exit)."""
    for _ in range(bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES):
        bot_config.record_cycle_failure()
    exchange, _ = _exchange()
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 2.0)

    assert result["status"] == "blocked"
    assert "Zyklen in Folge" in result["reason"]


def test_reconcile_closes_phantom_position_and_logs_it(tmp_db):
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0})
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    result = bot.reconcile_with_exchange(exchange)

    assert result["phantom_closed"] == ["BTC"]
    assert result["unknown_positions"] == []
    assert bot.open_positions() == []
    orders = db.list_bot_orders()
    assert any(o["intent"] == "exchange_reconciled_close" for o in orders)
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "reconciled_close" for d in decisions)


def test_reconcile_trips_kill_switch_on_side_mismatch(tmp_db):
    """Richtungswechsel ist ein Integritaetsfehler, kein normaler Abgleich -
    frueher endete reconcile_with_exchange() fuer ein in DB UND Boerse
    vorhandenes Symbol mit einem blossen `continue`, ohne Seite/Groesse/
    Einstand je zu vergleichen."""
    live_pos = HLPosition(symbol="BTC", side="short", size=0.001, entry_px=80000.0,
                          leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[live_pos], prices={"BTC": 80000.0})
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    bot.reconcile_with_exchange(exchange)

    assert bot_config.is_killed() is True
    pos = db.get_bot_position("BTC")
    assert pos["side"] == "long"   # unveraendert - kein Auto-Fix bei einem Richtungswechsel
    assert pos["status"] == "open"
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "reconciled_side_mismatch" for d in decisions)


def test_reconcile_updates_db_size_to_match_exchange(tmp_db):
    """Manuelle Teilschliessung ausserhalb des Bots: die Boerse zeigt weniger
    Groesse als die DB - die DB muss nachgezogen werden, bevor Heat-/
    Exposure-/Trailing-Berechnungen im selben Takt die alte Groesse lesen."""
    live_pos = HLPosition(symbol="BTC", side="long", size=0.4, entry_px=80000.0,
                          leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[live_pos], prices={"BTC": 80000.0})
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    bot.reconcile_with_exchange(exchange)

    pos = db.get_bot_position("BTC")
    assert pos["size"] == pytest.approx(0.4)
    decisions = db.list_bot_decisions()
    matching = [d for d in decisions if d["action"] == "reconciled_size_adjusted"]
    assert len(matching) == 1
    import json
    payload = json.loads(matching[0]["signals_json"])
    assert payload["old_size"] == pytest.approx(1.0)
    assert payload["new_size"] == pytest.approx(0.4)


def test_reconcile_leaves_initial_stop_px_untouched_on_entry_px_drift(tmp_db):
    """Einstandskurs-Drift wird nur protokolliert - initial_stop_px/stop_px
    bleiben unangetastet (CLAUDE.md: initial_stop_px ist unveraenderlich),
    eine automatische Neuberechnung waere selbst eine neue, ungeprüfte
    Entscheidung."""
    live_pos = HLPosition(symbol="BTC", side="long", size=1.0, entry_px=84000.0,
                          leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[live_pos], prices={"BTC": 84000.0})
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    bot.reconcile_with_exchange(exchange)

    pos = db.get_bot_position("BTC")
    assert pos["entry_px"] == pytest.approx(80000.0)   # unveraendert
    assert pos["stop_px"] == pytest.approx(70000.0)    # unveraendert
    assert pos["initial_stop_px"] == pytest.approx(70000.0)   # unveraendert
    decisions = db.list_bot_decisions()
    matching = [d for d in decisions if d["action"] == "reconciled_entry_px_drift"]
    assert len(matching) == 1
    assert matching[0]["executed"] == 0


def test_reconcile_flags_unknown_exchange_position_and_trips_kill_switch(tmp_db):
    """Ohne replace_stop_order (stop_orders_supported=False, Default) kann
    die Position nicht sicher uebernommen werden - bleibt unbekannt, wie
    vor der Adoptions-Funktion."""
    unknown = HLPosition(symbol="ETH", side="long", size=1.0, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown])
    bot.initialize(exchange)

    result = bot.reconcile_with_exchange(exchange)

    assert result["unknown_positions"] == ["ETH"]
    assert result["adopted"] == []
    assert bot_config.is_killed() is True
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "unknown_exchange_position" for d in decisions)


# --- Automatische Übernahme unbekannter Börsen-Positionen (adopt_unknown_positions) ---

def test_reconcile_auto_adopts_unknown_position_with_fresh_stop(tmp_db):
    # 0.02 ETH zu 2600 $ = 52 $ Nominale auf 250 $ Equity (~21 %) - passt in
    # max_position_pct. Frueher stand hier size=1.0, also 1040 % der Equity:
    # das lief nur durch, weil die Uebernahme an allen Guards vorbeiging.
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert result["adopted"] == ["ETH"]
    assert result["unknown_positions"] == []
    assert bot_config.is_killed() is False
    pos = bot.open_positions()[0]
    assert pos["symbol"] == "ETH"
    assert pos["exchange_oid"] == "stop-ETH"
    assert pos["stop_px"] < 2600.0  # Long -> Stop unter dem aktuellen Kurs
    assert pos["origin"] == "adopted"  # nie als Bot-Entscheidung zaehlen
    assert pos["origin_note"]
    assert len(exchange.stop_orders_placed) == 1
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "position_adopted" for d in decisions)
    # Bestandsvorgang gehoert in denselben Audit-Trail wie der Phantom-Schluss
    assert any(o["intent"] == "exchange_adopt" and o["status"] == "filled"
               for o in db.list_bot_orders())


def test_reconcile_refuses_adoption_that_breaks_position_size(tmp_db):
    """Eine von Hand eroeffnete Position, die groesser ist als das
    Positionslimit erlaubt, darf der Bot NICHT stillschweigend uebernehmen -
    genau dieser Weg lief frueher an jedem Guard vorbei."""
    oversized = HLPosition(symbol="ETH", side="long", size=1.0, entry_px=2500.0,
                           leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[oversized],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert result["adopted"] == []
    assert result["unknown_positions"] == ["ETH"]
    assert bot.open_positions() == []
    assert bot_config.is_killed() is True
    assert exchange.stop_orders_placed == []
    assert any(o["intent"] == "exchange_adopt" and o["status"] == "blocked"
               for o in db.list_bot_orders())


def test_reconcile_refuses_adoption_above_leverage_limit(tmp_db):
    """Hebel wurde bei der Uebernahme woertlich von der Boerse kopiert - eine
    von Hand mit 20x eroeffnete Position landete so mit 20x im Bestand,
    obwohl das harte Limit bei 2x liegt."""
    leveraged = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                           leverage=20.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[leveraged],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert result["adopted"] == []
    assert result["unknown_positions"] == ["ETH"]
    assert bot_config.is_killed() is True


def test_reconcile_adoption_respects_cumulative_exposure(tmp_db):
    """Der eigentliche gemeldete Fall: fuenf einzeln unauffaellige Longs,
    deren SUMME das Gesamt-Exposure-Limit reisst. Der Zaehler muss innerhalb
    der Schleife mitwachsen, sonst ist jede Position fuer sich harmlos."""
    bot_config.save_bot_limits(max_total_exposure_pct=50.0, max_positions=10,
                               max_position_pct=25.0)
    symbols = [f"SYM{i}" for i in range(5)]
    # je 30 $ auf 250 $ Equity = 12 % einzeln, 60 % zusammen -> ab der
    # fuenften Position ist bei 50 % Schluss.
    unknowns = [HLPosition(symbol=s, side="long", size=3.0, entry_px=10.0,
                          leverage=1.0, unrealized_pnl_usd=0.0) for s in symbols]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=unknowns,
                                 prices={s: 10.0 for s in symbols}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert len(result["adopted"]) == 4      # 4 x 30 $ = 120 $ = 48 % < 50 %
    assert len(result["unknown_positions"]) == 1
    assert bot_config.is_killed() is True   # der Rest gehoert vor menschliche Augen


def test_reconcile_does_not_adopt_when_stop_placement_fails(tmp_db):
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown], prices={"ETH": 2600.0},
                                 stop_orders_supported=True, stop_fails=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert result["adopted"] == []
    assert result["unknown_positions"] == ["ETH"]
    assert bot.open_positions() == []
    assert bot_config.is_killed() is True
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "position_adopt_failed" for d in decisions)


def test_reconcile_does_not_adopt_without_enough_candles_for_atr(tmp_db):
    """Kein ATR berechenbar (zu wenige Kerzen) -> keine Uebernahme, statt
    einen Stop-Abstand zu raten."""
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown], prices={"ETH": 2600.0},
                                 stop_orders_supported=True)
    bot.initialize(exchange)
    too_few = _synthetic_candles(n=10)

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: too_few)

    assert result["adopted"] == []
    assert result["unknown_positions"] == ["ETH"]
    assert bot.open_positions() == []


def test_reconcile_adopts_up_to_the_position_limit_then_kills(tmp_db):
    """Mehr unbekannte Positionen als max_positions: die passenden werden
    uebernommen (und damit abgesichert), der Rest zieht den Kill-Switch.

    Frueher entschied ein Mengen-Vorgate alles-oder-nichts und verglich die
    Unbekannten ALLEIN mit max_positions - das liess bei bestehendem Bestand
    zu viele durch und entzog im umgekehrten Fall auch denen den Schutz-Stop,
    die problemlos ins Limit gepasst haetten."""
    limits = bot_config.bot_limits()
    symbols = [f"SYM{i}" for i in range(limits["max_positions"] + 1)]
    unknowns = [HLPosition(symbol=s, side="long", size=1.0, entry_px=10.0,
                          leverage=1.0, unrealized_pnl_usd=0.0) for s in symbols]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=unknowns,
                                 prices={s: 10.0 for s in symbols}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    assert len(result["adopted"]) == limits["max_positions"]
    assert len(result["unknown_positions"]) == 1
    assert len(exchange.stop_orders_placed) == limits["max_positions"]
    assert bot_config.is_killed() is True


def test_reconcile_matching_state_is_a_noop(tmp_db):
    matching = HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                          leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[matching], prices={"BTC": 80000.0})
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    result = bot.reconcile_with_exchange(exchange)

    assert result == {"phantom_closed": [], "phantom_closed_details": [], "unknown_positions": [], "adopted": []}
    assert bot_config.is_killed() is False
    assert len(bot.open_positions()) == 1


def test_reconcile_is_noop_for_paper_exchange(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    result = bot.reconcile_with_exchange(exchange)
    assert result == {"phantom_closed": [], "phantom_closed_details": [], "unknown_positions": [], "adopted": []}


def test_check_stop_triggers_marks_failed_close_as_not_closed(tmp_db):
    exchange = _FakeLiveExchange(
        positions=[HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                              leverage=1.0, unrealized_pnl_usd=0.0)],
        prices={"BTC": 60000.0}, close_fails=True,
    )
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    events = bot.check_stop_triggers(exchange)

    assert len(events) == 1
    assert events[0]["closed"] is False


def test_check_stop_retries_failed_close_after_price_bounces(tmp_db):
    exchange = _FakeLiveExchange(
        positions=[HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                              leverage=1.0, unrealized_pnl_usd=0.0)],
        prices={"BTC": 60000.0}, close_fails=True,
    )
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    first = bot.check_stop_triggers(exchange)
    assert first[0]["closed"] is False
    assert db.get_meta("bot_stop_retry:BTC") == "1"

    exchange.close_fails = False
    exchange.prices["BTC"] = 80000.0  # stop condition no longer hit
    second = bot.check_stop_triggers(exchange)

    assert second[0]["closed"] is True
    assert bot.open_positions() == []
    assert db.get_meta("bot_stop_retry:BTC") is None


def test_cycle_does_not_signal_llm_review_on_failed_stop_close(tmp_db):
    """result['stop_events'] steuert bot_runner._maybe_run_llm_review's
    ereignisgetriggerten LLM-Aufruf - ein fehlgeschlagener Close darf den
    NICHT ausloesen (sonst ein bezahlter LLM-Call pro 15-Minuten-Takt ohne
    dass sich je etwas aendert)."""
    exchange = _FakeLiveExchange(
        positions=[HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                              leverage=1.0, unrealized_pnl_usd=0.0)],
        prices={"BTC": 60000.0}, close_fails=True,
    )
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="111")

    result = bot.run_deterministic_cycle(exchange)

    assert result["stop_events"] == []
    assert len(result["failed_stop_closes"]) == 1
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "stop_close_failed" and d["executed"] == 0 for d in decisions)


def test_close_position_cancels_stop_order_on_success(tmp_db):
    exchange = _FakeLiveExchange(
        positions=[HLPosition(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                              leverage=1.0, unrealized_pnl_usd=0.0)],
        prices={"BTC": 82000.0},
    )
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid="999")

    result = bot.close_position(exchange, "BTC", reason="test")

    assert result["status"] == "filled"
    assert exchange.cancelled == [("BTC", "999")]


def test_close_position_paper_exchange_skips_cancel_without_error(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    result = bot.close_position(exchange, "BTC", reason="test")
    assert result["status"] == "filled"  # kein AttributeError, obwohl PaperExchange kein cancel_order hat


def test_initialize_stores_exchange_kind(tmp_db):
    paper_exchange, _ = _exchange()
    info = bot.initialize(paper_exchange)
    assert info["exchange_kind"] == "paper"
    bot.reset()

    live_exchange = _FakeLiveExchange()
    info = bot.initialize(live_exchange)
    assert info["exchange_kind"] == "live"


def test_exchange_kind_mismatch_detects_paper_to_live_switch(tmp_db):
    paper_exchange, _ = _exchange()
    bot.initialize(paper_exchange)

    assert bot.exchange_kind_mismatch(_FakeLiveExchange()) is True
    assert bot.exchange_kind_mismatch(paper_exchange) is False


def test_exchange_kind_mismatch_false_for_legacy_anchor_without_field(tmp_db):
    """Ein Anker von VOR diesem Fix hat kein 'exchange_kind'-Feld - darf den
    Bot nicht rueckwirkend blockieren, nur ab jetzt getrackt werden."""
    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 250.0}')
    assert bot.exchange_kind_mismatch(_FakeLiveExchange()) is False


def test_refresh_live_costs_overwrites_not_accumulates(tmp_db):
    exchange = _FakeLiveExchange(
        fills=[{"fee": "0.05"}, {"fee": "0.03"}], funding_total=1.25,
    )
    bot.initialize(exchange)
    db.set_meta("bot_fees_cum_usd", "999.0")  # veraltete Schaetzung, muss ueberschrieben werden
    db.set_meta("bot_funding_cum_usd", "888.0")

    refreshed = bot.refresh_live_costs(exchange)

    assert refreshed is True
    assert float(db.get_meta("bot_fees_cum_usd")) == pytest.approx(0.08)
    assert float(db.get_meta("bot_funding_cum_usd")) == pytest.approx(1.25)


def test_refresh_live_costs_returns_false_for_paper_exchange(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    assert bot.refresh_live_costs(exchange) is False


def test_refresh_live_costs_without_active_bot_returns_false(tmp_db):
    exchange = _FakeLiveExchange(fills=[{"fee": "0.05"}], funding_total=1.0)
    assert bot.refresh_live_costs(exchange) is False


# --- _cost_window_since_ms(): kein Doppelzaehlen einer Einzahlung, die kurz
# VOR dem eigentlichen initialize()-Aufruf, aber am selben Kalendertag,
# ankam (bereits in equity_start_usd enthalten). ---

def test_cost_window_uses_started_at_when_present(tmp_db):
    info = {"date": "2026-08-25", "started_at": "2026-08-25T23:46:50"}
    expected = int(datetime(2026, 8, 25, 23, 46, 50).timestamp() * 1000)
    assert bot._cost_window_since_ms(info) == expected


def test_cost_window_falls_back_to_first_bot_equity_row(tmp_db):
    """Bestandsinstallationen von VOR dieser Aenderung haben kein
    'started_at' im bot_start-Meta - der allererste bot_equity-Punkt (von
    initialize() im selben Atemzug geschrieben) ist der praezisere Ersatz
    gegenueber Mitternacht des Kalendertags."""
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    info = {"date": bot.start_info()["date"]}  # kein 'started_at'
    first = db.first_bot_equity()
    expected = int(datetime.fromisoformat(first["ts"]).timestamp() * 1000)
    assert bot._cost_window_since_ms(info) == expected


def test_cost_window_falls_back_to_midnight_without_any_history(tmp_db):
    info = {"date": "2026-08-25"}  # weder started_at noch ein bot_equity-Punkt
    from datetime import timezone
    expected = int(datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp() * 1000)
    assert bot._cost_window_since_ms(info) == expected


# --- Ein-/Auszahlungen + Session-Startkapital ---

def test_refresh_live_flows_overwrites_not_accumulates(tmp_db):
    exchange = _FakeLiveExchange(flows_total=150.0)
    bot.initialize(exchange)
    db.set_meta("bot_net_flows_usd", "999.0")  # veralteter Wert, muss ueberschrieben werden

    refreshed = bot.refresh_live_flows(exchange)

    assert refreshed is True
    assert float(db.get_meta("bot_net_flows_usd")) == pytest.approx(150.0)


def test_refresh_live_flows_returns_false_for_paper_exchange(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    assert bot.refresh_live_flows(exchange) is False


def test_refresh_live_flows_without_active_bot_returns_false(tmp_db):
    exchange = _FakeLiveExchange(flows_total=50.0)
    assert bot.refresh_live_flows(exchange) is False


def test_record_session_start_overwrites_on_every_call_without_touching_anchor(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    assert bot.equity_start_usd() == 250.0

    exchange._cash_usd = 300.0
    written = bot.record_session_start(exchange)

    assert written == 300.0
    assert bot.session_start_usd() == 300.0
    # Der sicherheitskritische Anker bleibt unveraendert - nur die Session-
    # Baseline fuer die "Startkapital"-Anzeige wandert mit dem Neustart.
    assert bot.equity_start_usd() == 250.0


def test_session_start_usd_falls_back_to_equity_start_usd_when_unset(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    assert bot.session_start_usd() == 250.0


def test_session_start_usd_none_when_bot_never_started(tmp_db):
    assert bot.session_start_usd() is None


def test_performance_summary_pnl_usd_is_flow_adjusted(tmp_db):
    exchange, market = _exchange(equity=1000.0)
    bot.initialize(exchange)

    # Simuliert eine Einzahlung von 200 $ ohne jeden Handelsgewinn: die
    # Equity springt auf 1200, aber pnl_usd darf das NICHT als Gewinn zeigen.
    exchange._cash_usd = 1200.0
    db.set_meta("bot_net_flows_usd", "200.0")
    bot.record_equity_snapshot(exchange)

    summary = bot.performance_summary()

    assert summary["net_flows_usd"] == pytest.approx(200.0)
    assert summary["pnl_usd"] == pytest.approx(0.0)
    # net_pnl_usd bleibt bewusst UNbereinigt (sicherheitskritische Groessen
    # wie das 90-Tage-Kriterium haengen daran) - hier zeigt es die Einzahlung
    # noch als scheinbaren Gewinn, exakt wie vor dieser Aenderung.
    assert summary["net_pnl_usd"] == pytest.approx(200.0)


# --- equity_reading(): Vertrauenswuerdigkeit einer Equity-Messung ---

def test_equity_reading_trusted_by_default(tmp_db):
    exchange = _FakeLiveExchange(equity_usd=100.0)
    reading = bot.equity_reading(exchange)
    assert reading["trusted"] is True
    assert reading["usd"] == 100.0
    assert reading["reason"] == ""


def test_equity_reading_untrusted_when_exchange_flags_it(tmp_db):
    exchange = _FakeLiveExchange(equity_usd=5.92, equity_trusted=False)
    reading = bot.equity_reading(exchange)
    assert reading["trusted"] is False
    assert reading["reason"] != ""


def test_equity_reading_flags_implausible_drop_even_if_exchange_trusts_it(tmp_db):
    """Regressionstest fuer den realen Vorfall: die Boerse selbst markierte
    den Fehlwert damals NICHT als unsicher (Kontomodus wurde ja erkannt,
    daher equity_trusted=True auf Boersenebene) - erst der Vergleich gegen
    die eigene Historie deckt den unplausiblen Sprung auf."""
    bot.initialize(_FakeLiveExchange(equity_usd=44.50))
    reading = bot.equity_reading(_FakeLiveExchange(equity_usd=5.92, equity_trusted=True))
    assert reading["trusted"] is False
    assert "5.92" in reading["reason"]


def test_equity_reading_allows_a_real_severe_loss(tmp_db):
    """Ein echter Handelsverlust (hier: 60% in einem Takt) darf NICHT als
    Datenfehler abgefangen werden - dafuer ist check_equity_floor da."""
    bot.initialize(_FakeLiveExchange(equity_usd=250.0))
    reading = bot.equity_reading(_FakeLiveExchange(equity_usd=100.0, equity_trusted=True))
    assert reading["trusted"] is True


def test_equity_reading_skips_drop_check_without_prior_history(tmp_db):
    reading = bot.equity_reading(_FakeLiveExchange(equity_usd=1.0, equity_trusted=True))
    assert reading["trusted"] is True


# --- record_equity_snapshot(): kein Punkt bei nicht vertrauenswuerdiger Messung ---

def test_record_equity_snapshot_writes_nothing_when_untrusted(tmp_db):
    exchange = _FakeLiveExchange(equity_usd=5.92, equity_trusted=False)
    result = bot.record_equity_snapshot(exchange)
    assert result["trusted"] is False
    assert db.latest_bot_equity() is None


def test_record_equity_snapshot_writes_when_trusted(tmp_db):
    exchange = _FakeLiveExchange(equity_usd=44.5)
    result = bot.record_equity_snapshot(exchange)
    assert result["trusted"] is True
    assert db.latest_bot_equity()["equity_usd"] == pytest.approx(44.5)


# --- adjusted_start_usd(): Ein-/Auszahlungs-bereinigter Sicherheits-Anker ---

def test_adjusted_start_usd_without_flows_equals_start(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    assert bot.adjusted_start_usd() == pytest.approx(250.0)


def test_adjusted_start_usd_shifts_up_after_deposit(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    db.set_meta("bot_net_flows_usd", "50.0")
    bot.record_equity_snapshot(exchange)
    assert bot.adjusted_start_usd() == pytest.approx(300.0)


def test_adjusted_start_usd_shifts_down_after_withdrawal(tmp_db):
    exchange, _ = _exchange(equity=250.0)
    bot.initialize(exchange)
    db.set_meta("bot_net_flows_usd", "-50.0")
    bot.record_equity_snapshot(exchange)
    assert bot.adjusted_start_usd() == pytest.approx(200.0)


def test_adjusted_start_usd_none_when_bot_never_started(tmp_db):
    assert bot.adjusted_start_usd() is None


# --- Paper-Neustart: DB-Geister sauber schliessen ---

def test_paper_restart_closes_ghost_positions_from_db(tmp_db):
    """PaperExchange haelt Positionen nur im Prozessspeicher, die DB
    ueberlebt einen Neustart des Runners.

    Frueher stieg reconcile_with_exchange() fuer Paper sofort wieder aus
    ("per Konstruktion konsistent"). Nach jedem Neustart standen die alten
    DB-Positionen als Geister da, die die frisch gebaute Papier-Boerse nicht
    kannte - und JEDER Schliessversuch scheiterte dauerhaft mit "keine offene
    Position", ohne dass der Zustand je bereinigt wurde."""
    exchange, _ = _exchange(prices={"BTC": 110.0})
    bot.initialize(exchange)
    # Zustand aus einem frueheren Prozess.
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                                leverage=1.0, stop_px=90.0, exchange_oid="paper-stop-BTC")
    assert bot.close_position(exchange, "BTC")["status"] == "error"  # das alte Sackgassen-Verhalten

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda s: _synthetic_candles())

    assert result["phantom_closed"] == ["BTC"]
    assert bot.open_positions() == []
    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"]
    assert closed[0]["realized_pnl_usd"] == pytest.approx(10.0)   # (110 - 100) * 1
    assert any(d["action"] == "reconciled_close" for d in db.list_bot_decisions())


def test_paper_reconcile_is_a_noop_when_state_matches(tmp_db):
    """Der Abgleich darf im Normalbetrieb nichts anfassen - sonst wuerde er
    bei jedem Takt Positionen schliessen, die voellig in Ordnung sind."""
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)

    result = bot.reconcile_with_exchange(exchange, candles_fn=lambda s: _synthetic_candles())

    assert result == {"phantom_closed": [], "phantom_closed_details": [], "unknown_positions": [], "adopted": []}
    assert len(bot.open_positions()) == 1


# --- Realisiertes Ergebnis ist netto ---

def test_realized_pnl_is_net_of_fees(tmp_db):
    """bot_positions.realized_pnl_usd rechnete brutto, performance_summary
    dagegen auf der (netto) Equity - die beiden Zahlen liessen sich nie zur
    Deckung bringen, und ein Trade mit einem Kursgewinn kleiner als die
    Round-Trip-Gebuehr erschien in der Historie als Gewinn."""
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=70000.0)

    market.prices["BTC"] = 81000.0
    result = bot.close_position(exchange, "BTC", reason="test")

    assert result["status"] == "filled"
    assert result["fees_usd"] > 0
    assert result["realized_pnl_usd"] == pytest.approx(
        result["gross_pnl_usd"] - result["fees_usd"])
    stored = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    assert stored["realized_pnl_usd"] == pytest.approx(result["realized_pnl_usd"])


def test_tiny_winner_is_a_loser_after_fees(tmp_db):
    """Der eigentliche Punkt: eine Kursbewegung unterhalb der Round-Trip-
    Kosten ist KEIN Gewinn, auch wenn der Kurs in die richtige Richtung lief."""
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=70000.0)

    market.prices["BTC"] = 80000.0 * 1.0005   # +0,05 %, unter den Kosten
    result = bot.close_position(exchange, "BTC", reason="test")

    assert result["gross_pnl_usd"] < 0 or result["realized_pnl_usd"] < result["gross_pnl_usd"]
    assert result["realized_pnl_usd"] < 0


# --- Herkunft ---

def test_bot_opened_position_is_marked_as_bot_origin(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)

    pos = bot.open_positions()[0]
    assert pos["origin"] == "bot"


# --- Signal-/Outcome-Datensatz (reine Datensammlung) ---

def test_signal_cycle_logs_every_candidate(tmp_db, monkeypatch):
    """Eine Zeile je KANDIDAT je Takt - nicht nur je Trade. Uebersprungene
    und blockierte Signale sind fuer eine spaetere Auswertung genauso wichtig
    wie ausgefuehrte ("Fehler der Untaetigkeit")."""
    exchange, _ = _exchange(prices={"BTC": 100.0, "ETH": 100.0})
    bot.initialize(exchange)
    candles = _synthetic_candles()
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC", "ETH"])

    bot.run_signal_cycle(exchange, candles_fn=lambda s: candles)

    rows = db.list_bot_signal_log()
    assert {r["symbol"] for r in rows} == {"BTC", "ETH"}
    assert all(r["action"] in ("opened", "skipped", "blocked") for r in rows)
    assert all(r["strategy_version"] for r in rows)
    # Rohwerte muessen mitgeschrieben werden - inklusive der Kerzen-
    # MAs/ATR, die als Pflichtbedingung (Gate 2), nicht als Punktzahl, in
    # die Entscheidung einfliessen. ("volume"/"efficiency" gehoerten zu
    # Gate 6/3, die seit STRATEGY_VERSION "2.2-swing4h" entfernt sind, siehe
    # core.bot_signals.readings()'s Docstring.)
    import json as _json
    readings = _json.loads(rows[0]["readings_json"])
    assert "ma20" in readings and "ma50" in readings and "atr_pct" in readings


def _uptrend_candles(n=160, start=100.0, per_candle_pct=0.25):
    """Sauberer Aufwaertstrend - stark genug, dass evaluate() das Signal als
    handelbar durchlaesst."""
    closes = start * (1 + per_candle_pct / 100) ** np.arange(n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({"Open": opens, "High": closes * 1.004, "Low": opens * 0.996,
                        "Close": closes, "Volume": np.full(n, 1000.0)}, index=index)


def _downtrend_candles(n=160, start=100.0, per_candle_pct=0.25):
    """Gegenstueck zu _uptrend_candles: Kurs faellt stetig, MA20 liegt unter
    MA50 und der letzte Kurs liegt unter MA50 - Gate 2 (core.bot_signals.
    evaluate) blockt damit ein 'long'-Regime-Signal (tradeable=False), OHNE
    das Regime selbst zu aendern (das bleibt global 'long', siehe
    _default_bullish_regime). Fuer den Reentry-Test wird genau das gebraucht:
    ein Signal, dessen SEITE weiterhin 'long' ist (vom Regime vorgegeben),
    das aber wegen der eigenen Kursstruktur nicht handelbar ist."""
    closes = start * (1 - per_candle_pct / 100) ** np.arange(n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({"Open": opens, "High": opens * 1.004, "Low": closes * 0.996,
                        "Close": closes, "Volume": np.full(n, 1000.0)}, index=index)


def test_blocked_entry_is_not_logged_as_opened(tmp_db, monkeypatch):
    """Eine vom Guard abgelehnte Order ist eine verpasste Gelegenheit, keine
    Bot-Entscheidung - sie darf nie als 'opened' in die Trainingsdaten."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    bot_config.trip_kill_switch("Test")

    bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    rows = db.list_bot_signal_log()
    assert rows and all(r["action"] != "opened" for r in rows)


def test_guard_blocked_entry_gets_its_own_reason_not_generic_skipped(tmp_db, monkeypatch):
    """Review-Punkt 5 (Runde 3): der GEWAEHLTE Kandidat, an einem Guard
    INNERHALB von open_position() gescheitert (hier check_total_exposure,
    bewusst nicht Kill-Switch/max_positions - die filtert run_signal_cycle
    schon VOR der Kandidatenwahl heraus), landete vorher im generischen
    'skipped'-Eimer - ununterscheidbar von 'kein Kandidat ueber der
    Schwelle', obwohl der genaue Ablehnungsgrund in outcome['reason']
    laengst vorlag."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])
    bot_config.save_bot_limits(max_total_exposure_pct=1.0)

    bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    rows = db.list_bot_signal_log()
    btc = [r for r in rows if r["symbol"] == "BTC"][0]
    assert btc["action"] == "blocked"
    assert "Exposure" in (btc["block_reason"] or "") or "exposure" in (btc["block_reason"] or "")
    assert btc["position_id"] is None   # Guard hat VOR jeder Order abgelehnt


def test_candidate_failing_only_min_notional_gets_its_own_reason_not_generic_skipped(
        tmp_db, monkeypatch):
    """Phase 2 (Kapital-Realitaet), B6: ein Kandidat kann JEDE der sieben
    Pflichtbedingungen bestehen und trotzdem nie zu einer Order werden, weil
    die daraus berechnete Zielgroesse unter der Mindestordergroesse der
    Boerse liegt (core.bot_signals.position_size_usd). Dieser Fehlschlag
    passiert VOR jedem Aufruf von open_position()/evaluate_entry_order() -
    `attempted_symbol`/`entry_outcome` (fuer genau diese Unterscheidung
    gedacht, siehe _log_signal_scan-Docstring) wurden fuer diesen Fall vorher
    nie gesetzt, der Kandidat verschwand im generischen 'skipped'-Eimer,
    identisch zu 'kein Kandidat ueber der Schwelle' - obwohl die Boerse eine
    Order fuer dieses Konto bei DIESER Konfiguration niemals akzeptiert
    haette, unabhaengig vom Chart."""
    # Equity so klein, dass jede risikobasierte ODER konzentrationsbasierte
    # Zielgroesse weit unter MIN_NOTIONAL_USD (10 $) faellt, unabhaengig vom
    # konkreten Stop-Abstand des Kandidaten.
    exchange, _ = _exchange(equity=1.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])

    result = bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    assert result["entry"] is None
    assert "Mindestordergröße" in (result["skipped"] or "")
    rows = db.list_bot_signal_log()
    btc = [r for r in rows if r["symbol"] == "BTC"][0]
    assert btc["action"] == "blocked"
    assert "Mindestordergröße" in (btc["block_reason"] or "")


def test_signal_log_records_position_id_on_a_real_entry(tmp_db, monkeypatch):
    """Review-Punkt 5: position_id war bislang IMMER None, auch fuer die
    Zeile des tatsaechlich eroeffneten Kandidaten - obwohl core.bot.open_position()
    die ID laengst zurueckgibt. Ohne sie laesst sich eine 'opened'-Zeile in
    bot_signal_log nie mit der zugehoerigen bot_positions-Zeile verknuepfen."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["BTC"])

    bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    rows = db.list_bot_signal_log()
    opened = [r for r in rows if r["action"] == "opened"]
    assert opened and opened[0]["position_id"] is not None
    assert opened[0]["position_id"] == bot.open_positions()[0]["id"]


# --- R-Multiple-Korrektheit: initial_stop_px bleibt unveraenderlich ---

def test_initial_stop_px_survives_trailing(tmp_db):
    """Der Kernfall aus dem Review: ein anfangs 2 $ riskierender Stop wird
    fast bis zum Einstieg nachgezogen und der Trade gewinnt 4 $. Ohne die
    unveraenderliche Kopie haette das R-Multiple faelschlich das Vielfache
    des tatsaechlichen Risikos gezeigt."""
    exchange, market = _exchange(prices={"BTC": 100.0})
    bot.initialize(exchange)
    # stop_px=98.0 ist nur die VORGABE fuer die Distanzberechnung (2 %) -
    # der tatsaechliche Stop wird seit der Fill-Preis-Korrektur aus dem
    # bestaetigten (leicht slippage-behafteten) Fill berechnet, daher die
    # Toleranz statt eines exakten Werts.
    _open_ok(exchange, symbol="BTC", side="long", notional=50.0, stop_px=98.0)

    pos = bot.open_positions()[0]
    initial = pos["initial_stop_px"]
    assert initial == pytest.approx(98.0, abs=0.1)

    db.update_bot_position_stop("BTC", 99.9)   # fast bis zum Einstieg nachgezogen

    pos = bot.open_positions()[0]
    assert pos["stop_px"] == 99.9              # aktueller Stop wandert mit
    assert pos["initial_stop_px"] == initial   # urspruenglicher bleibt fix


def test_adopted_position_gets_initial_stop_px(tmp_db):
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    pos = bot.open_positions()[0]
    assert pos["initial_stop_px"] == pos["stop_px"]


# --- _exchange_open_time: aktueller Positionszyklus, nicht der neueste Fill ---

def test_exchange_open_time_uses_earliest_open_after_last_close(tmp_db):
    """Mehrere Nachkaeufe: der Zyklus beginnt beim ERSTEN Open-Fill NACH dem
    letzten vollstaendigen Close - nicht beim neuesten Fill ueberhaupt. Sonst
    waere die Position juenger erschienen, als sie tatsaechlich ist."""
    fills = [
        {"coin": "ETH", "dir": "Open Long", "time": 1_000_000},   # laengst vergangener Zyklus
        {"coin": "ETH", "dir": "Close Long", "time": 2_000_000},  # vollstaendig geschlossen
        {"coin": "ETH", "dir": "Open Long", "time": 3_000_000},   # AKTUELLER Zyklus beginnt hier
        {"coin": "ETH", "dir": "Open Long", "time": 4_000_000},   # Nachkauf im selben Zyklus
    ]
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True,
                                 fills=fills)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    pos = bot.open_positions()[0]
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    expected = _dt.fromtimestamp(3_000_000 / 1000, tz=_tz.utc).isoformat(timespec="seconds")
    assert pos["exchange_opened_at"] == expected


def test_exchange_open_time_none_without_any_open_fill(tmp_db):
    fills = [{"coin": "ETH", "dir": "Close Long", "time": 1_000_000}]
    unknown = HLPosition(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                         leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[unknown],
                                 prices={"ETH": 2600.0}, stop_orders_supported=True,
                                 fills=fills)
    bot.initialize(exchange)
    candles = _synthetic_candles()

    bot.reconcile_with_exchange(exchange, candles_fn=lambda symbol: candles)

    pos = bot.open_positions()[0]
    assert pos["exchange_opened_at"] is None


# --- Einmaliger Nachtrag von exchange_opened_at fuer LAENGST uebernommene Positionen ---

def test_backfill_exchange_opened_at_fills_in_the_missing_timestamp(tmp_db):
    """Review ('Aktueller Zustand', Runde 3): 5 real bereits uebernommene
    Positionen hatten exchange_opened_at = NULL, weil das Feld erst NACH
    ihrer Uebernahme eingefuehrt wurde. Der Nachtrag muss dieselbe Logik wie
    eine NEUE Uebernahme nutzen (fruehester Open-Fill nach dem letzten
    Close) und die DB-Zeile aktualisieren."""
    fills = [{"coin": "ETH", "dir": "Open Long", "time": 3_000_000}]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"ETH": 2600.0},
                                 fills=fills)
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                                leverage=1.0, stop_px=2300.0, exchange_oid="1",
                                origin="adopted", exchange_opened_at=None)

    results = bot.backfill_exchange_opened_at(exchange)

    from datetime import datetime as _dt
    from datetime import timezone as _tz
    expected = _dt.fromtimestamp(3_000_000 / 1000, tz=_tz.utc).isoformat(timespec="seconds")
    assert results == [{"symbol": "ETH", "updated": True, "exchange_opened_at": expected}]
    assert bot.open_positions()[0]["exchange_opened_at"] == expected


def test_backfill_exchange_opened_at_skips_positions_that_already_have_it(tmp_db):
    """Nie einen bereits bekannten echten Wert ueberschreiben."""
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"ETH": 2600.0},
                                 fills=[{"coin": "ETH", "dir": "Open Long", "time": 9_999_000}])
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="ETH", side="long", size=0.02, entry_px=2500.0,
                                leverage=1.0, stop_px=2300.0, exchange_oid="1",
                                origin="adopted", exchange_opened_at="2026-01-01T00:00:00")

    results = bot.backfill_exchange_opened_at(exchange)

    assert results == []
    assert bot.open_positions()[0]["exchange_opened_at"] == "2026-01-01T00:00:00"


def test_backfill_exchange_opened_at_skips_bot_opened_positions(tmp_db):
    """Nur 'adopted' braucht den Nachtrag - eine vom Bot selbst eroeffnete
    Position hat opened_at bereits korrekt gesetzt."""
    exchange, _ = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)

    results = bot.backfill_exchange_opened_at(exchange)

    assert results == []


# --- Verzoegerte Positions-Sicht nach einem Fill (P2) ---

class _DelayedVisibilityExchange(PaperExchange):
    """Simuliert eine Boerse, deren Kontostand die frisch gefuellte Position
    erst nach ein paar Abfragen zeigt - eine leicht verzoegerte Lese-API darf
    einen tatsaechlichen Fill nicht als "keine Position" melden."""

    def __init__(self, *args, hide_calls=0, **kwargs):
        super().__init__(*args, **kwargs)
        self._hide_calls = hide_calls
        self._calls = 0

    def account_state(self):
        state = super().account_state()
        self._calls += 1
        if self._calls <= self._hide_calls:
            from data.hyperliquid import AccountState
            return AccountState(equity_usd=state.equity_usd,
                               withdrawable_usd=state.withdrawable_usd, positions=[])
        return state


def test_open_position_retries_before_declaring_not_found(tmp_db, monkeypatch):
    exchange = _DelayedVisibilityExchange(starting_equity_usd=250.0,
                                          price_fn=lambda s: {"BTC": 80000.0}.get(s),
                                          hide_calls=2)
    monkeypatch.setattr(bot, "_POSITION_LOOKUP_DELAY", 0.0)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 50.0, 1.0, 12.5)

    assert result["status"] == "filled"
    assert bot.open_positions()[0]["symbol"] == "BTC"


def test_open_position_reconstructs_from_order_after_exhausting_retries(tmp_db, monkeypatch):
    """Ein bestaetigter Fill (result.status == "filled") darf NIE spurlos
    verloren gehen, nur weil account_state() ihn noch nicht zeigt - eine
    fruehere Fassung gab hier auf ("status": "error") OHNE je eine
    bot_positions-Zeile zu schreiben: die Position existierte real auf der
    Boerse, war in der DB aber komplett unverwaltet (kein Trailing, keine
    Ausstiegspruefung) bis zum naechsten reconcile_with_exchange()-Takt. Seit
    der Behebung wird die Position aus der Order-Antwort selbst (Groesse/
    Einstand/Hebel) rekonstruiert und sofort persistiert, mit einer
    Diagnose-Zeile (`position_reconstructed_from_order`), die den
    Naeherungscharakter sichtbar macht."""
    exchange = _DelayedVisibilityExchange(starting_equity_usd=250.0,
                                          price_fn=lambda s: {"BTC": 80000.0}.get(s),
                                          hide_calls=99)
    monkeypatch.setattr(bot, "_POSITION_LOOKUP_DELAY", 0.0)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 50.0, 1.0, 12.5)

    assert result["status"] == "filled"
    positions = bot.open_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTC"
    assert positions[0]["side"] == "long"
    assert positions[0]["size"] > 0

    decisions = db.list_bot_decisions(limit=20)
    assert any(d["action"] == "position_reconstructed_from_order" for d in decisions)


# --- Phantom-Schluss: echter Fill statt geschaetztem mid_price ---

def test_phantom_close_uses_real_fill_price_when_available(tmp_db):
    """DER Kernfall aus dem Review: ein Boersen-Stop feuerte zwischen zwei
    Takten zu einem Kurs, der vom AKTUELLEN mid_price abweicht. Die
    Reconciliation muss den ECHTEN Fill verwenden (inkl. Gebuehr und
    Hyperliquids eigenem closedPnl), nicht den Kurs, der gerade zufaellig
    beim naechsten Takt herrscht."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="111",
                                opened_at="2026-01-01T00:00:00")
    fills = [{"coin": "BTC", "dir": "Close Long", "px": "78050.0", "sz": "0.01",
             "closedPnl": "-19.5", "fee": "0.35", "time": 1735776000000}]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[],  # BTC nicht mehr auf der Boerse
                                 prices={"BTC": 82000.0},          # WEIT weg vom echten Fill
                                 fills=fills)
    bot.initialize(exchange)

    result = bot.reconcile_with_exchange(exchange)

    assert result["phantom_closed"] == ["BTC"]
    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    assert closed["close_px"] == pytest.approx(78050.0)          # aus dem Fill, nicht 82000
    assert closed["realized_pnl_usd"] == pytest.approx(-19.5 - 0.35)   # closedPnl - fee
    decisions = db.list_bot_decisions()
    reconciled = [d for d in decisions if d["action"] == "reconciled_close"][0]
    assert "echter Fill-Kurs" in reconciled["reason"]


def test_phantom_close_uses_the_real_fill_timestamp_not_the_reconciliation_time(tmp_db):
    """Bugfix-Regressionstest: `closed_at` speicherte bislang den Zeitpunkt
    des Abgleich-Takts (jetzt), nicht den echten Boersen-Fill-Zeitpunkt,
    obwohl dieser aus den Fills selbst (Feld `time`) laengst bekannt war. Bei
    einem 15-Minuten-Takt kann das bis zu einer Kerze auseinanderliegen und
    hat live sogar einmal die Reihenfolge zweier Schluesse vertauscht -
    Haltedauer, Cooldown-Start und die Reihenfolge einer Verlustserie
    beziehen sich alle auf `closed_at`."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="111",
                                opened_at="2026-01-01T00:00:00")
    fill_time_ms = 1735776000000   # 2025-01-01T16:00:00+00:00 - deutlich VOR "jetzt"
    fills = [{"coin": "BTC", "dir": "Close Long", "px": "78050.0", "sz": "0.01",
             "closedPnl": "-19.5", "fee": "0.35", "time": fill_time_ms}]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[],
                                 prices={"BTC": 82000.0}, fills=fills)
    bot.initialize(exchange)

    bot.reconcile_with_exchange(exchange)

    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    expected = clock.iso_utc(datetime.fromtimestamp(fill_time_ms / 1000, tz=timezone.utc))
    assert closed["closed_at"] == expected
    # Nicht "jetzt" (der Test laeuft weit nach 2025) - die Kernaussage des Fixes.
    assert clock.parse_utc(closed["closed_at"]) < clock.now_utc() - timedelta(days=300)


def test_phantom_close_nets_the_opening_fee_too_not_only_the_closing_fee(tmp_db):
    """Review-Punkt 4 (Runde 3): die Reconciliation zog bislang nur die
    Schliessungsgebuehr aus den Boersen-Fills ab, nicht die beim Eroeffnen
    bereits gezahlte (in bot_orders fuer dieselbe position_id festgehaltene)
    Gebuehr - wie core.bot.close_position() es fuer den reguerren Schluss
    schon immer tut (db.bot_position_fees_usd summiert ueber die GANZE
    Position). Sonst ist ausgerechnet der praezise Fill-Pfad zu optimistisch."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="111",
                                opened_at="2026-01-01T00:00:00")
    position_id = db.list_open_bot_positions()[0]["id"]
    db.add_bot_order(symbol="BTC", intent="open_long", side="long", size=0.01,
                     order_type="market", requested_px=80000.0, status="filled",
                     fill_px=80000.0, fee_usd=0.72, position_id=position_id)
    fills = [{"coin": "BTC", "dir": "Close Long", "px": "78050.0", "sz": "0.01",
             "closedPnl": "-19.5", "fee": "0.35", "time": 1735776000000}]
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0},
                                 fills=fills)
    bot.initialize(exchange)

    bot.reconcile_with_exchange(exchange)

    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    # closedPnl - Schliessungsgebuehr - Eroeffnungsgebuehr, NICHT nur
    # closedPnl - Schliessungsgebuehr (das waere um die 0.72 zu optimistisch).
    assert closed["realized_pnl_usd"] == pytest.approx(-19.5 - 0.35 - 0.72)


def test_phantom_close_falls_back_to_estimate_without_fills(tmp_db):
    """Ohne passenden Fill (Paper, oder kein Treffer im Zeitfenster) bleibt
    die bisherige mid_price-Schaetzung als Rueckfall erhalten."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="111",
                                opened_at="2026-01-01T00:00:00")
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0},
                                 fills=[])   # keine passenden Fills
    bot.initialize(exchange)

    result = bot.reconcile_with_exchange(exchange)

    assert result["phantom_closed"] == ["BTC"]
    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    assert closed["close_px"] == pytest.approx(82000.0)
    reconciled = [d for d in db.list_bot_decisions() if d["action"] == "reconciled_close"][0]
    assert "geschätzter PnL" in reconciled["reason"]


def test_phantom_close_estimate_still_nets_a_known_opening_fee(tmp_db):
    """Auch der Schaetz-Zweig (kein Fill gefunden) zog vorher GAR KEINE
    Gebuehr ab - nicht einmal die beim Eroeffnen bereits bekannte. Jetzt
    laeuft beide Zweige durch dieselbe db.bot_position_fees_usd()-Nettung."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="111",
                                opened_at="2026-01-01T00:00:00")
    position_id = db.list_open_bot_positions()[0]["id"]
    db.add_bot_order(symbol="BTC", intent="open_long", side="long", size=0.01,
                     order_type="market", requested_px=80000.0, status="filled",
                     fill_px=80000.0, fee_usd=0.72, position_id=position_id)
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0},
                                 fills=[])
    bot.initialize(exchange)

    bot.reconcile_with_exchange(exchange)

    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    expected_gross = (82000.0 - 80000.0) * 0.01
    assert closed["realized_pnl_usd"] == pytest.approx(expected_gross - 0.72)


def test_phantom_close_cancels_the_orphaned_exchange_stop(tmp_db):
    """Ein Phantom-Schluss (Liquidation/manueller Schluss auf der Boerse)
    liess den zuvor gesetzten reduce-only-Stop-Trigger vorher unangetastet
    stehen - anders als core.bot.close_position(), das seinen Stop immer
    storniert. Ein verwaister Trigger koennte spaeter eine voellig neue,
    unbeteiligte Position im selben Symbol ausloesen."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="stop-111",
                                opened_at="2026-01-01T00:00:00")
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0},
                                 fills=[])
    bot.initialize(exchange)

    result = bot.reconcile_with_exchange(exchange)

    assert result["phantom_closed"] == ["BTC"]
    assert ("BTC", "stop-111") in exchange.cancelled


def test_phantom_close_logs_when_stop_cancel_fails(tmp_db, monkeypatch):
    """Ein fehlgeschlagener Storno darf den bereits vollzogenen Phantom-
    Schluss nicht rueckgaengig machen (best effort), muss aber sichtbar
    bleiben - sonst verschwindet ein verwaister Stop kommentarlos."""
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.01, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="stop-111",
                                opened_at="2026-01-01T00:00:00")
    exchange = _FakeLiveExchange(equity_usd=250.0, positions=[], prices={"BTC": 82000.0},
                                 fills=[])
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "_cancel_stop_if_supported", lambda ex, sym, oid: False)

    bot.reconcile_with_exchange(exchange)

    decisions = db.list_bot_decisions()
    assert any(d["action"] == "stop_cancel_failed" for d in decisions)


# --- Teilschliessung: die Boerse fuellt nur einen Teil der Schliess-Order ---

class _PartialFillExchange(_FakeLiveExchange):
    """close_position() liefert absichtlich weniger filled_size als die
    Positionsgroesse - simuliert eine Boerse, die eine Schliess-Order nur
    teilweise fuellt (Liquiditaet/Margin)."""

    def __init__(self, *args, filled_fraction=0.5, **kwargs):
        super().__init__(*args, **kwargs)
        self._filled_fraction = filled_fraction

    def close_position(self, symbol):
        self.close_calls.append(symbol)
        remaining_pos = next(p for p in self.positions if p.symbol == symbol)
        filled = remaining_pos.size * self._filled_fraction
        price = self.prices.get(symbol, 0.0)
        # Wie eine echte Boerse: die Position schrumpft um die gefuellte
        # Menge - ein zweiter close_position()-Aufruf (voller Schluss der
        # Restgroesse) muss die tatsaechliche Restgroesse sehen, nicht
        # wieder die urspruengliche volle Groesse.
        remainder = remaining_pos.size - filled
        if remainder > 1e-12:
            self.positions = [HLPosition(symbol=p.symbol, side=p.side, size=remainder,
                                        entry_px=p.entry_px, leverage=p.leverage,
                                        unrealized_pnl_usd=p.unrealized_pnl_usd)
                              if p.symbol == symbol else p for p in self.positions]
        else:
            self.positions = [p for p in self.positions if p.symbol != symbol]
        return OrderResult(status="filled", fill_px=price, filled_size=filled, fee_usd=0.01)


def test_close_position_keeps_remainder_open_on_partial_fill(tmp_db):
    """Eine fruehere Fassung buchte JEDE gefuellte Schliess-Order als
    Vollschluss - eine reale Restposition lief auf der Boerse weiter, wurde
    aber in der DB als geschlossen gefuehrt und beim naechsten Reconcile-Takt
    faelschlich als "unbekannt" mit frischem Stop und frischem opened_at neu
    uebernommen (max_hold_hours faelschlich zurueckgesetzt)."""
    # Groesse bewusst klein gehalten: _max_reasonable_exit_notional deckelt
    # Schliessungen auf equity * max_leverage * 5 (Plausibilitaetsguard), bei
    # Standard-Equity 250 $/1x-Hebel also 1250 $ - eine zu grosse Testposition
    # wuerde schon an diesem Guard scheitern, bevor es um die Teilfuellung geht.
    position = HLPosition(symbol="BTC", side="long", size=0.002, entry_px=80000.0,
                        leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _PartialFillExchange(equity_usd=250.0, positions=[position],
                                    prices={"BTC": 82000.0}, filled_fraction=0.5)
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.002, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="stop-111",
                                opened_at="2026-01-01T00:00:00")

    result = bot.close_position(exchange, "BTC", reason="test")

    assert result["status"] == "partial"
    open_positions = db.list_open_bot_positions()
    assert len(open_positions) == 1
    assert open_positions[0]["size"] == pytest.approx(0.001)
    assert open_positions[0]["stop_px"] == 78000.0  # unveraendert, Position ist weiter geschuetzt
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "close_partial_fill" for d in decisions)


def test_close_position_partial_fill_realized_pnl_carries_to_final_close(tmp_db):
    """Der Bruttogewinn/-verlust der ersten Teilschliessung darf beim
    spaeteren Schluss der Restgroesse nicht verschwinden - er wird in
    partial_realized_pnl_usd akkumuliert und beim finalen Schluss
    hinzuaddiert (core.db.close_bot_position)."""
    position = HLPosition(symbol="BTC", side="long", size=0.002, entry_px=80000.0,
                        leverage=1.0, unrealized_pnl_usd=0.0)
    exchange = _PartialFillExchange(equity_usd=250.0, positions=[position],
                                    prices={"BTC": 82000.0}, filled_fraction=0.5)
    bot.initialize(exchange)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.002, entry_px=80000.0,
                                leverage=1.0, stop_px=78000.0, exchange_oid="stop-111",
                                opened_at="2026-01-01T00:00:00")

    first = bot.close_position(exchange, "BTC", reason="test")
    assert first["status"] == "partial"
    partial_gross = (82000.0 - 80000.0) * 0.001  # erste Haelfte, zum selben Kurs gefuellt

    # Restgroesse (0.001) jetzt vollstaendig schliessen.
    exchange._filled_fraction = 1.0
    second = bot.close_position(exchange, "BTC", reason="test")
    assert second["status"] == "filled"

    closed = [p for p in db.list_bot_positions() if p["status"] == "closed"][0]
    final_gross = (82000.0 - 80000.0) * 0.001  # zweite Haelfte
    fees = db.bot_position_fees_usd(closed["id"])
    expected = partial_gross + final_gross - fees
    assert closed["realized_pnl_usd"] == pytest.approx(expected)


# --- trades_today() zaehlt nur echte Handels-Intents ---

def test_trades_today_ignores_reconciliation_orders(tmp_db):
    """Eine fruehere Fassung zaehlte JEDE gefuellte bot_orders-Zeile gegen
    MAX_TRADES_PER_DAY, auch exchange_adopt/exchange_reconciled_close -
    reine Buchhaltung ohne eigene Handelsentscheidung. Wenige Uebernahmen/
    Phantom-Schluesse konnten so das Tagesbudget ohne einen einzigen echten
    Trade aufbrauchen."""
    db.add_bot_order(symbol="BTC", intent="open_long", side="long", size=0.01,
                     order_type="market", requested_px=80000.0, status="filled",
                     fill_px=80000.0, fee_usd=0.1)
    db.add_bot_order(symbol="ETH", intent="exchange_adopt", side="long", size=0.1,
                     order_type=None, requested_px=None, status="filled",
                     fill_px=2500.0, fee_usd=0.0)
    db.add_bot_order(symbol="SOL", intent="exchange_reconciled_close", side="long", size=1.0,
                     order_type=None, requested_px=None, status="filled",
                     fill_px=100.0, fee_usd=0.0)
    db.add_bot_order(symbol="ADA", intent="close", side="long", size=1.0,
                     order_type="market", requested_px=1.0, status="filled",
                     fill_px=1.0, fee_usd=0.01)

    assert bot.trades_today() == 2


# --- Bestehende Positionen gegen aktuelle Limits (Warnung, kein Zwang) ---

def test_existing_position_limit_warnings_flags_leverage_above_current_limit(tmp_db):
    """Realer Ablauf: die Position war beim Eroeffnen gueltig (Stufe 10,
    max_leverage 2x), eine SPAETERE Verschaerfung des Reglers (Stufe 1,
    max_leverage 1x) macht sie nachtraeglich unzulaessig - genau der im
    Review beobachtete Fall (2x Bestandspositionen, Regler inzwischen auf
    1,9x)."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0},
                           funding={"BTC": -0.001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)
    assert _open_ok(exchange, symbol="BTC", notional=30.0, leverage=2.0,
                    stop_px=95.0)["status"] == "filled"

    bot_config.save_risk_level(1)   # max_leverage jetzt 1.0x
    warnings = bot.existing_position_limit_warnings(exchange, bot_config.bot_limits())

    assert any("Hebel" in w and "BTC" in w for w in warnings)


def test_existing_position_limit_warnings_flags_too_many_positions(tmp_db):
    exchange, _ = _exchange(prices={"BTC": 100.0, "ETH": 100.0})
    bot.initialize(exchange)
    for symbol in ("BTC", "ETH"):
        assert _open_ok(exchange, symbol=symbol, notional=10.0,
                        stop_px=95.0)["status"] == "filled"
    limits = dict(bot_config.bot_limits())
    limits["max_positions"] = 1   # nachtraeglich verschaerft

    warnings = bot.existing_position_limit_warnings(exchange, limits)

    assert any("offene Positionen" in w for w in warnings)


def test_existing_position_limit_warnings_flags_total_exposure(tmp_db):
    exchange, _ = _exchange(equity=100.0, prices={"BTC": 100.0})
    bot.initialize(exchange)
    assert _open_ok(exchange, symbol="BTC", notional=20.0,
                    stop_px=95.0)["status"] == "filled"
    limits = dict(bot_config.bot_limits())
    limits["max_total_exposure_pct"] = 10.0   # nachtraeglich weit unter die 20 % gesenkt

    warnings = bot.existing_position_limit_warnings(exchange, limits)

    assert any("Exposure" in w for w in warnings)


def test_existing_position_limit_warnings_empty_when_within_limits(tmp_db):
    exchange, _ = _exchange(prices={"BTC": 100.0})
    bot.initialize(exchange)
    assert _open_ok(exchange, symbol="BTC", notional=10.0,
                    stop_px=95.0)["status"] == "filled"

    assert bot.existing_position_limit_warnings(exchange, bot_config.bot_limits()) == []


def test_existing_position_limit_warnings_never_closes_anything(tmp_db):
    """Der ausdrueckliche Punkt aus dem Review: warnen, NICHT schliessen."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0}, funding={"BTC": -0.001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)
    assert _open_ok(exchange, symbol="BTC", notional=30.0, leverage=2.0,
                    stop_px=95.0)["status"] == "filled"
    bot_config.save_risk_level(1)

    bot.existing_position_limit_warnings(exchange, bot_config.bot_limits())

    assert len(bot.open_positions()) == 1


def test_run_signal_cycle_blocks_new_entries_while_a_position_exceeds_leverage_limit(
        tmp_db, monkeypatch):
    """Review-Punkt ('Aktueller Zustand', Runde 3): anders als eine Exposure-
    Verletzung blockiert eine reine Hebel-Verletzung KEINE neuen Einstiege
    von selbst (check_leverage prueft nur den Hebel DER NEUEN Order). Der
    Review verlangt ausdruecklich: bestehende Position NICHT automatisch
    schliessen, aber ALLE neuen Einstiege sperren, bis das Depot wieder
    regelkonform ist."""
    exchange, _ = _exchange(equity=250.0, prices={"BTC": 100.0, "ETH": 100.0},
                           funding={"BTC": -0.001, "ETH": -0.001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)
    assert _open_ok(exchange, symbol="BTC", notional=30.0, leverage=2.0,
                    stop_px=95.0)["status"] == "filled"
    bot_config.save_risk_level(1)   # max_leverage jetzt 1.0x - BTC liegt darueber
    monkeypatch.setattr(bot, "candidate_symbols", lambda: ["ETH"])

    result = bot.run_signal_cycle(exchange, candles_fn=lambda s: _uptrend_candles())

    assert result["entry"] is None
    assert "Hebel-Limit" in (result["skipped"] or "")
    assert bot.open_positions() == [p for p in bot.open_positions() if p["symbol"] == "BTC"]


def test_deterministic_cycle_logs_existing_limit_violation_as_decision(tmp_db):
    exchange, market = _exchange(equity=250.0, prices={"BTC": 100.0}, funding={"BTC": -0.001})
    bot.initialize(exchange)
    bot_config.save_risk_level(10)
    assert _open_ok(exchange, symbol="BTC", notional=30.0, leverage=2.0,
                    stop_px=95.0)["status"] == "filled"
    bot_config.save_risk_level(1)   # max_leverage jetzt 1.0x - die Position liegt darueber

    result = bot.run_deterministic_cycle(exchange)

    assert result["existing_position_limit_warnings"]
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "existing_position_limits_exceeded" for d in decisions)
    assert len(bot.open_positions()) == 1   # weiterhin offen, nicht zwangsgeschlossen


# --- Manuelles Stop-Nachziehen (Button je Position) ---

def _open_btc_long(stop_px=90.0):
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                                leverage=1.0, stop_px=stop_px, exchange_stop_px=stop_px,
                                exchange_oid="old-stop")


def test_manual_stop_replaces_exchange_order_and_updates_db(tmp_db):
    exchange = _FakeLiveExchange(prices={"BTC": 150.0}, stop_orders_supported=True)
    _open_btc_long()

    result = bot.set_manual_stop(exchange, "btc", 130.0)

    assert result["status"] == "ok" and result["exchange_updated"] is True
    assert exchange.stop_orders_placed == [("BTC", "long", 1.0, 130.0, "old-stop")]
    pos = db.get_bot_position("BTC")
    assert pos["stop_px"] == pytest.approx(130.0)
    assert pos["exchange_stop_px"] == pytest.approx(130.0)
    assert pos["exchange_oid"] == "stop-BTC"


def test_manual_stop_exchange_failure_leaves_db_untouched(tmp_db):
    """Die UI darf keinen Stop anzeigen, den die Boerse nicht hat."""
    exchange = _FakeLiveExchange(prices={"BTC": 150.0}, stop_orders_supported=True,
                                 stop_fails=True)
    _open_btc_long()

    result = bot.set_manual_stop(exchange, "BTC", 130.0)

    assert result["status"] == "error"
    pos = db.get_bot_position("BTC")
    assert pos["stop_px"] == pytest.approx(90.0)
    assert pos["exchange_stop_px"] == pytest.approx(90.0)
    assert pos["exchange_oid"] == "old-stop"


@pytest.mark.parametrize("side,stop,price,new_stop,fragment", [
    ("long", 90.0, 150.0, 85.0, "nachgezogen"),      # lockern
    ("long", 90.0, 150.0, 149.9, "mindestens"),      # zu nah am Kurs
    ("long", 90.0, 150.0, 160.0, "mindestens"),      # ueber dem Kurs
    ("short", 110.0, 50.0, 120.0, "nachgezogen"),    # Short lockern
    ("short", 110.0, 50.0, 49.0, "mindestens"),      # Short unter dem Kurs
])
def test_validate_manual_stop_rejects(side, stop, price, new_stop, fragment):
    pos = {"side": side, "stop_px": stop, "entry_px": 100.0}
    assert fragment in bot.validate_manual_stop(pos, new_stop, price)


def test_validate_manual_stop_accepts_short_tightening():
    pos = {"side": "short", "stop_px": 110.0, "entry_px": 100.0}
    assert bot.validate_manual_stop(pos, 60.0, 50.0) is None


def test_manual_stop_paper_moves_both_stops_without_replace(tmp_db):
    exchange = _FakeLiveExchange(prices={"BTC": 150.0})   # kein replace_stop_order
    _open_btc_long()

    result = bot.set_manual_stop(exchange, "BTC", 120.0)

    assert result["status"] == "ok" and result["exchange_updated"] is False
    pos = db.get_bot_position("BTC")
    assert pos["stop_px"] == pytest.approx(120.0)
    assert pos["exchange_stop_px"] == pytest.approx(120.0)


def test_manual_stop_request_processed_once_and_result_kept(tmp_db):
    exchange = _FakeLiveExchange(prices={"BTC": 150.0}, stop_orders_supported=True)
    _open_btc_long()
    bot.request_manual_stop("BTC", 130.0)
    bot.request_manual_stop("ETH", 10.0)   # keine offene Position

    results = {r["symbol"]: r for r in bot.process_manual_stop_requests(exchange)}

    assert results["BTC"]["status"] == "ok"
    assert results["ETH"]["status"] == "error"
    assert bot.manual_stop_requests() == {}
    assert bot.manual_stop_results()["BTC"]["status"] == "ok"
    assert bot.process_manual_stop_requests(exchange) == []
    assert len(exchange.stop_orders_placed) == 1


def test_trailing_does_not_undo_manual_stop(tmp_db, monkeypatch):
    """Ein manuell engerer Stop darf vom naechsten Automatik-Takt nicht
    zurueckgesetzt oder erneut an der Boerse ersetzt werden."""
    exchange = _FakeLiveExchange(prices={"BTC": 150.0}, stop_orders_supported=True)
    _open_btc_long()
    bot.set_manual_stop(exchange, "BTC", 130.0)

    monkeypatch.setattr(bot.bot_signals, "trailing_stop_px",
                        lambda *args, **kwargs: None)
    bot.update_trailing_stops(exchange, candles_fn=lambda symbol: object())
    pos = db.get_bot_position("BTC")
    assert pos["stop_px"] == pytest.approx(130.0)
    assert len(exchange.stop_orders_placed) == 1


# --- Manuelles Schliessen einer einzelnen Position (Button je Position) ---

def test_process_close_position_requests_closes_only_requested_symbol(tmp_db):
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    _open_ok(exchange, symbol="ETH", notional=30.0, stop_px=2000.0)
    bot.request_close_position("btc")

    results = bot.process_close_position_requests(exchange)

    assert len(results) == 1 and results[0]["symbol"] == "BTC"
    assert results[0]["status"] == "filled"
    assert [p["symbol"] for p in bot.open_positions()] == ["ETH"]
    assert bot.close_position_requests() == {}
    assert bot.close_position_results()["BTC"]["status"] == "filled"
    decisions = db.list_bot_decisions()
    assert any(d["action"] == "manual_close_single" for d in decisions)


def test_process_close_position_requests_keeps_request_on_failure(tmp_db):
    """Wie close_all_positions()/_maybe_close_all(): ein Fehlschlag darf die
    Anfrage nicht stillschweigend verwerfen - der naechste Takt versucht es
    automatisch erneut, weil Schliessen risikomindernd ist."""
    exchange, market = _exchange()
    bot.initialize(exchange)
    _open_ok(exchange, symbol="BTC", notional=30.0, stop_px=70000.0)
    del market.prices["BTC"]   # PaperExchange.close_position scheitert ohne Preis
    bot.request_close_position("BTC")

    results = bot.process_close_position_requests(exchange)

    assert results[0]["status"] == "error"
    assert "BTC" in bot.close_position_requests()   # Anfrage bleibt stehen
    assert db.get_bot_position("BTC")["status"] == "open"
    assert bot.close_position_results()["BTC"]["status"] == "error"


def test_process_close_position_requests_without_open_position_is_noop(tmp_db):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    bot.request_close_position("BTC")   # keine offene Position

    results = bot.process_close_position_requests(exchange)

    assert results[0]["status"] == "error"
    assert bot.close_position_requests() == {}   # "still_open" ist False -> geraeumt


def test_cancel_close_position_request(tmp_db):
    bot.request_close_position("BTC")
    assert "BTC" in bot.close_position_requests()
    bot.cancel_close_position_request("btc")
    assert bot.close_position_requests() == {}


# --- Externe Pruefung 23.09.2026: Notausstieg, Teilgewinn, Stop-Retry ---

class _FlakyAfterFillExchange(_NakedStopExchange):
    """Wie _NakedStopExchange, aber account_state() faellt NACH dem Fill aus
    (API-Ruckler) - solange die Position offen ist."""

    def account_state(self):
        if self._open:
            raise RuntimeError("API nicht erreichbar")
        return super().account_state()


def test_emergency_exit_runs_even_if_account_state_fails_after_fill(tmp_db):
    exchange = _FlakyAfterFillExchange(close_succeeds=True)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert exchange.close_calls == 1, "Notausstieg wurde nie versucht"
    assert result["status"] == "error"
    assert bot.open_positions() == []


def test_failed_emergency_exit_keeps_db_row_retry_flag_and_kill_switch(tmp_db):
    exchange = _FlakyAfterFillExchange(close_succeeds=False)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert "NOTAUSSTIEG FEHLGESCHLAGEN" in result["reason"]
    assert exchange.close_calls >= 1
    assert len(bot.open_positions()) == 1, "offene Position fehlt in der DB"
    assert db.get_meta("bot_stop_retry:BTC") == "1"
    assert bot_config.is_killed() is True


def test_close_position_works_when_account_state_fails(tmp_db):
    exchange = _FlakyAfterFillExchange(close_succeeds=True)
    exchange._open = True
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid=None)

    result = bot.close_position(exchange, "BTC", reason="test")

    assert result["status"] == "filled"
    assert bot.open_positions() == []


class _FillsExchange(_FakeLiveExchange):
    def recent_fills(self, since_ms):
        return [f for f in self._fills if int(f["time"]) >= since_ms]


def test_partial_close_then_exchange_stop_does_not_double_count_pnl(tmp_db):
    """Bot schliesst 1 von 2 Einheiten (+10), die Boerse den Rest (+20).
    Korrekt: 30. Frueher wurde der Teilgewinn erneut addiert (40)."""
    now = clock.now_utc()
    ms = lambda dt: int(dt.timestamp() * 1000)
    opened = clock.iso_utc(now - timedelta(hours=1))
    pid = db.upsert_open_bot_position(symbol="BTC", side="long", size=2.0, entry_px=100.0,
                                      leverage=1.0, stop_px=90.0, exchange_oid=None,
                                      opened_at=opened)
    fills = [
        {"coin": "BTC", "dir": "Close Long", "sz": "1", "px": "110", "closedPnl": "10",
         "fee": "0", "time": ms(now - timedelta(seconds=30))},
        {"coin": "BTC", "dir": "Close Long", "sz": "1", "px": "120", "closedPnl": "20",
         "fee": "0", "time": ms(now + timedelta(seconds=30))},
    ]
    exchange = _FillsExchange(prices={"BTC": 120.0}, fills=fills)
    db.add_bot_order(symbol="BTC", intent="close", side="long", size=1.0, order_type="market",
                     requested_px=110.0, status="filled", fill_px=110.0, fee_usd=0.0,
                     position_id=pid)
    db.reduce_bot_position_size("BTC", new_size=1.0, add_realized_pnl_usd=10.0)

    bot.reconcile_with_exchange(exchange)

    closed = [p for p in db.list_bot_positions() if p["symbol"] == "BTC"][0]
    assert closed["status"] == "closed"
    assert closed["realized_pnl_usd"] == pytest.approx(30.0)


def _open_with_stops(exchange_stop, local_stop, **kw):
    exchange = _FakeLiveExchange(prices={"BTC": 150.0}, stop_orders_supported=True, **kw)
    db.upsert_open_bot_position(symbol="BTC", side="long", size=1.0, entry_px=100.0,
                                leverage=1.0, stop_px=local_stop,
                                exchange_stop_px=exchange_stop, exchange_oid="old-stop")
    return exchange


def test_retry_unconfirmed_exchange_stops_repeats_failed_replace(tmp_db):
    exchange = _open_with_stops(exchange_stop=90.0, local_stop=99.0)

    updates = bot.retry_unconfirmed_exchange_stops(exchange)

    assert exchange.stop_orders_placed == [("BTC", "long", 1.0, 99.0, "old-stop")]
    assert updates[0]["exchange_updated"] is True
    pos = db.get_bot_position("BTC")
    assert pos["exchange_stop_px"] == pytest.approx(99.0)
    assert pos["exchange_oid"] == "stop-BTC"


def test_retry_unconfirmed_exchange_stops_ignores_sub_threshold_steps(tmp_db):
    exchange = _open_with_stops(exchange_stop=90.0, local_stop=90.3)   # < 0,5 %

    assert bot.retry_unconfirmed_exchange_stops(exchange) == []
    assert exchange.stop_orders_placed == []


def test_safety_cycle_retries_stops_when_no_decision_window_is_due(tmp_db, monkeypatch):
    exchange, _ = _exchange()
    bot.initialize(exchange)
    monkeypatch.setattr(bot, "_due_for_decision", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(bot, "retry_unconfirmed_exchange_stops",
                        lambda ex: calls.append(ex) or [])

    bot.run_deterministic_cycle(exchange)

    assert calls == [exchange]


# --- Zweite externe Pruefung 23.09.2026: DB-/Kursausfall im Notausstieg, Fill-Zuordnung ---

class _DbDiesAfterFillExchange(_NakedStopExchange):
    """Nach dem Fill ist die GESAMTE Datenbank nicht mehr erreichbar."""

    def __init__(self, close_succeeds, monkeypatch):
        super().__init__(close_succeeds)
        self._mp = monkeypatch

    def open_long(self, *a, **k):
        result = super().open_long(*a, **k)

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        self._mp.setattr(db, "_connect", _boom)
        return result


class _PriceAndStateDownExchange(_NakedStopExchange):
    def mid_price(self, symbol):
        if self._open:
            raise RuntimeError("Kurs-API nicht erreichbar")
        return 80000.0

    def account_state(self):
        if self._open:
            raise RuntimeError("API nicht erreichbar")
        return super().account_state()


def test_emergency_exit_runs_even_if_whole_db_is_down_after_fill(tmp_db, monkeypatch):
    exchange = _DbDiesAfterFillExchange(close_succeeds=True, monkeypatch=monkeypatch)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert exchange.close_calls == 1, "Notausstieg wurde nie versucht"
    assert "sofort geschlossen" in result["reason"]
    assert "BUCHUNG FEHLGESCHLAGEN" in result["reason"]


def test_failed_emergency_exit_with_db_down_does_not_raise(tmp_db, monkeypatch):
    exchange = _DbDiesAfterFillExchange(close_succeeds=False, monkeypatch=monkeypatch)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert exchange.close_calls >= 1
    assert "NOTAUSSTIEG FEHLGESCHLAGEN" in result["reason"]


def test_emergency_exit_runs_when_only_order_booking_fails(tmp_db, monkeypatch):
    exchange = _NakedStopExchange(close_succeeds=True)
    bot.initialize(exchange)
    monkeypatch.setattr(db, "add_bot_order", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert exchange.close_calls == 1
    assert result["status"] == "error"


def test_emergency_exit_runs_when_price_and_account_state_are_down(tmp_db):
    exchange = _PriceAndStateDownExchange(close_succeeds=True)
    bot.initialize(exchange)

    bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert exchange.close_calls == 1


def test_close_position_attempts_close_even_if_price_lookup_raises(tmp_db):
    exchange = _PriceAndStateDownExchange(close_succeeds=True)
    exchange._open = True
    db.upsert_open_bot_position(symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                                leverage=1.0, stop_px=70000.0, exchange_oid=None)

    result = bot.close_position(exchange, "BTC", reason="test")

    assert exchange.close_calls == 1
    assert result["status"] == "filled"


def _partial_then_rest_setup(rest_fill_time_offset_s, partial_fill_offset_s, partial_fee="0.5",
                             rest_fee="0.7", order_fee=0.5):
    now = clock.now_utc()
    ms = lambda dt: int(dt.timestamp() * 1000)
    opened = clock.iso_utc(now - timedelta(hours=1))
    pid = db.upsert_open_bot_position(symbol="BTC", side="long", size=2.0, entry_px=100.0,
                                      leverage=1.0, stop_px=90.0, exchange_oid=None,
                                      opened_at=opened)
    db.add_bot_order(symbol="BTC", intent="open_long", side="long", size=2.0,
                     order_type="market", requested_px=100.0, status="filled",
                     fill_px=100.0, fee_usd=0.2, position_id=pid)
    fills = [
        {"coin": "BTC", "dir": "Close Long", "sz": "1", "px": "110", "closedPnl": "10",
         "fee": partial_fee, "time": ms(now + timedelta(seconds=partial_fill_offset_s))},
        {"coin": "BTC", "dir": "Close Long", "sz": "1", "px": "120", "closedPnl": "20",
         "fee": rest_fee, "time": ms(now + timedelta(seconds=rest_fill_time_offset_s))},
    ]
    db.add_bot_order(symbol="BTC", intent="close", side="long", size=1.0, order_type="market",
                     requested_px=110.0, status="filled", fill_px=110.0, fee_usd=order_fee,
                     position_id=pid)
    db.reduce_bot_position_size("BTC", new_size=1.0, add_realized_pnl_usd=10.0)
    return _FillsExchange(prices={"BTC": 120.0}, fills=fills)


def _closed_btc():
    return [p for p in db.list_bot_positions() if p["symbol"] == "BTC"][0]


def test_partial_and_rest_fill_within_same_second_are_not_double_counted(tmp_db):
    # Bot-Teil-Fill 100 ms VOR der lokalen Buchung, Rest-Fill 300 ms danach:
    # beide innerhalb derselben Sekunde wie der (auf Sekunden gekuerzte)
    # Buchungszeitpunkt.
    exchange = _partial_then_rest_setup(rest_fill_time_offset_s=0.3, partial_fill_offset_s=-0.1)

    bot.reconcile_with_exchange(exchange)

    # 10 (Teil) + 20 (Rest) - Gebuehren einmal: 0.2 + 0.5 (Teil-Order) + 0.7 (Rest)
    assert _closed_btc()["realized_pnl_usd"] == pytest.approx(28.6)


def test_rest_fill_before_local_booking_is_not_dropped(tmp_db):
    # Verzoegerte Buchung: der Boersen-Stop fuellte BEVOR die lokale Order-Zeile
    # des Teilschlusses geschrieben wurde - ein Zeitfilter liesse ihn fallen.
    exchange = _partial_then_rest_setup(rest_fill_time_offset_s=-10, partial_fill_offset_s=-60)

    bot.reconcile_with_exchange(exchange)

    assert _closed_btc()["realized_pnl_usd"] == pytest.approx(28.6)


def test_fewer_close_fills_than_remaining_size_uses_all_of_them(tmp_db):
    now = clock.now_utc()
    db.upsert_open_bot_position(symbol="BTC", side="long", size=2.0, entry_px=100.0,
                                leverage=1.0, stop_px=90.0, exchange_oid=None,
                                opened_at=clock.iso_utc(now - timedelta(hours=1)))
    fills = [{"coin": "BTC", "dir": "Close Long", "sz": "1", "px": "120", "closedPnl": "20",
              "fee": "0", "time": int(now.timestamp() * 1000)}]
    exchange = _FillsExchange(prices={"BTC": 120.0}, fills=fills)

    bot.reconcile_with_exchange(exchange)

    assert _closed_btc()["realized_pnl_usd"] == pytest.approx(20.0)


class _PartialEmergencyCloseExchange(_NakedStopExchange):
    """Fill 0,12; der Notausstieg fuellt nur 0,06 (Liquiditaet/IOC)."""

    def __init__(self, on_fill=None):
        super().__init__(close_succeeds=True)
        self._on_fill = on_fill

    def open_long(self, *a, **k):
        self._open = True
        if self._on_fill:
            self._on_fill()
        return OrderResult(status="filled", fill_px=80000.0, filled_size=0.12, fee_usd=0.036,
                           error="EROEFFNET, aber Stop-Order fehlgeschlagen: ambiguous")

    def close_position(self, symbol):
        self.close_calls += 1
        return OrderResult(status="filled", fill_px=80000.0, filled_size=0.06, fee_usd=0.02)


def test_partial_emergency_close_is_not_reported_as_closed(tmp_db):
    exchange = _PartialEmergencyCloseExchange()
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert "sofort geschlossen" not in result["reason"]
    assert "NOTAUSSTIEG FEHLGESCHLAGEN" in result["reason"]
    assert "teilweise" in result["reason"]
    remaining = bot.open_positions()
    assert len(remaining) == 1 and remaining[0]["size"] == pytest.approx(0.06)
    assert db.get_meta("bot_stop_retry:BTC") == "1"
    assert bot_config.is_killed() is True


def test_partial_emergency_close_is_detected_even_if_db_is_down(tmp_db, monkeypatch):
    def _kill_db():
        monkeypatch.setattr(db, "_connect", lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))
    exchange = _PartialEmergencyCloseExchange(on_fill=_kill_db)
    bot.initialize(exchange)

    result = bot.open_position(exchange, "BTC", "long", 30.0, 1.0, 12.5)

    assert "sofort geschlossen" not in result["reason"]
    assert "NOTAUSSTIEG FEHLGESCHLAGEN" in result["reason"]
