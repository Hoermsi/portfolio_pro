"""data/hyperliquid.py: ausschliesslich gemockte SDK-Antworten - nie echte
Netzwerkcalls (Projektkonvention, siehe CLAUDE.md). Die tatsaechliche
Response-Form (response.data.statuses[...]) stammt aus der offiziellen
Hyperliquid-API-Doku, hier als feste Fixtures nachgebaut."""
import pytest

from core import config
from data import hyperliquid as hl

# Referenz auf die ECHTE candles_df() vor jedem Test-Zugriff: tests/conftest.py's
# autouse-Fixture _no_hyperliquid_network ersetzt hl.candles_df fuer JEDEN Test
# (auch in dieser Datei) durch ein blosses `lambda *a, **k: None` - Sicherheitsnetz
# gegen echte Netzwerkcalls aus Tests, die eigentlich nur core.bot pruefen wollen.
# Wer HIER gezielt candles_df() selbst testen will (Retry-Wiring unten), muss die
# echte Funktion je Test wieder einsetzen; diese Referenz wird VOR jedem
# Fixture-Lauf im Modul erfasst und bleibt deshalb unangetastet.
_REAL_CANDLES_DF = hl.candles_df

# Kleine, feste Test-Universe/Asset-Ctxs-Struktur - deckt die Form ab, die
# Info.meta_and_asset_ctxs() laut Doku liefert: [ {"universe": [...]}, [...] ],
# per Index verkettet.
_UNIVERSE = [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
    {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
]
_CTXS = [
    {"funding": "0.0000125", "markPx": "78900.0", "midPx": "78900.0"},
    {"funding": "-0.0000200", "markPx": "2450.0", "midPx": "2450.0"},
    {"funding": None, "markPx": "97.0", "midPx": "97.0"},
]
_MIDS = {"BTC": "78900.0", "ETH": "2450.0", "SOL": "97.0"}


@pytest.fixture(autouse=True)
def _mock_public_api(monkeypatch):
    """Fuer JEDEN Test in dieser Datei: die beiden einzigen Netzwerk-
    Einstiegspunkte (_mids, _meta_and_ctxs) durch feste Werte ersetzen -
    kein Test in dieser Datei darf tatsaechlich ins Netz gehen."""
    monkeypatch.setattr(hl, "_mids", lambda: dict(_MIDS))
    monkeypatch.setattr(hl, "_meta_and_ctxs", lambda: [{"universe": _UNIVERSE}, _CTXS])


# --- _with_retry(): Retry mit Backoff fuer lesende Info-Aufrufe ---

@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Backoff-Wartezeiten in JEDEM Test dieser Datei auf 0 setzen - sonst
    wuerde ein Test, der einen dauerhaften Fehlschlag simuliert, echte
    1,5 Sekunden (0,5s + 1s) Wanduhrzeit kosten."""
    monkeypatch.setattr(hl.time, "sleep", lambda seconds: None)


def test_with_retry_returns_immediately_on_first_success():
    calls = []

    def ok(x):
        calls.append(x)
        return x * 2

    assert hl._with_retry(ok, 21) == 42
    assert len(calls) == 1


def test_with_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("kurzer Ruckler")
        return "ok"

    assert hl._with_retry(flaky) == "ok"
    assert attempts["n"] == 3   # zwei Fehlschlaege, dritter Versuch erfolgreich


def test_with_retry_gives_up_after_max_attempts_and_reraises_last_exception():
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise TimeoutError(f"Versuch {attempts['n']}")

    with pytest.raises(TimeoutError, match="Versuch 3"):
        hl._with_retry(always_fails)
    assert attempts["n"] == hl._RETRY_ATTEMPTS


def test_with_retry_sleeps_with_increasing_backoff_between_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr(hl.time, "sleep", lambda seconds: slept.append(seconds))

    def always_fails():
        raise RuntimeError("nie erfolgreich")

    with pytest.raises(RuntimeError):
        hl._with_retry(always_fails)

    # Ein Sleep WENIGER als Versuche - nach dem letzten Fehlschlag wird nicht
    # mehr gewartet, da kein weiterer Versuch mehr folgt.
    assert len(slept) == hl._RETRY_ATTEMPTS - 1
    assert slept == sorted(slept)   # streng steigend (exponentiell)


def test_with_retry_passes_through_args_and_kwargs():
    def fn(a, b, c=None):
        return (a, b, c)

    assert hl._with_retry(fn, 1, 2, c=3) == (1, 2, 3)


def test_candles_df_retries_transient_failure_before_giving_up(monkeypatch):
    """Wiring-Test (nicht nur die reine _with_retry-Mechanik): candles_df()
    ruft _info_client().candles_snapshot() ueber _with_retry auf - ein
    einzelner transienter Fehlschlag darf nicht sofort None zurueckgeben.
    Reproduktion des real beobachteten Falls (ein Walk-Forward-Lauf und der
    15-Minuten-Runner-Takt trafen gleichzeitig auf dieselbe Hyperliquid-API;
    ein einzelner erneuter Versuch Sekunden spaeter war sofort erfolgreich).
    `_info_client` ist NICHT Teil des autouse-Fixtures oben (das ersetzt nur
    _mids/_meta_and_ctxs), daher hier frei mockbar."""
    calls = {"n": 0}
    candle = {"t": 1735689600000, "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10"}

    class _FakeInfo:
        def candles_snapshot(self, coin, interval, start, end):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionError("Rate-Limit")
            return [candle]

    monkeypatch.setattr(hl, "_info_client", lambda: _FakeInfo())
    # tests/conftest.py's autouse-Sicherheitsnetz ersetzt hl.candles_df sonst
    # durch ein No-Op-Lambda - fuer DIESEN Test die echte Funktion zurueck.
    monkeypatch.setattr(hl, "candles_df", _REAL_CANDLES_DF)
    _REAL_CANDLES_DF.cache_clear()   # @ttl_cache(300) - keine Reste aus anderen Laeufen

    df = hl.candles_df("BTC")

    assert df is not None and len(df) == 1
    assert calls["n"] == 2   # erster Versuch schlug fehl, zweiter griff


def test_candles_df_gives_up_and_returns_none_after_persistent_failure(monkeypatch):
    class _FakeInfo:
        def candles_snapshot(self, coin, interval, start, end):
            raise ConnectionError("dauerhaft nicht erreichbar")

    monkeypatch.setattr(hl, "_info_client", lambda: _FakeInfo())
    monkeypatch.setattr(hl, "candles_df", _REAL_CANDLES_DF)
    _REAL_CANDLES_DF.cache_clear()

    assert hl.candles_df("BTC") is None


# --- oeffentliche Lesefunktionen ---

def test_mid_price_known_symbol():
    assert hl.mid_price("btc") == 78900.0  # case-insensitiv


def test_mid_price_unknown_symbol_returns_none():
    assert hl.mid_price("DOGE") is None


def test_funding_rate_hourly_positive_and_negative():
    assert hl.funding_rate_hourly("BTC") == pytest.approx(0.0000125)
    assert hl.funding_rate_hourly("ETH") == pytest.approx(-0.0000200)


def test_funding_rate_hourly_missing_field_returns_none():
    assert hl.funding_rate_hourly("SOL") is None


def test_funding_rate_hourly_unknown_symbol_returns_none():
    assert hl.funding_rate_hourly("DOGE") is None


def test_supported_symbols():
    assert hl.supported_symbols() == {"BTC", "ETH", "SOL"}


def test_max_leverage_for():
    assert hl.max_leverage_for("BTC") == 40
    assert hl.max_leverage_for("SOL") == 20
    assert hl.max_leverage_for("DOGE") is None


def test_round_size_floors_never_rounds_up():
    # SOL: szDecimals=2 -> 1.239 wuerde auf 1.24 aufrunden, MUSS aber auf 1.23 abrunden
    assert hl._sz_decimals("SOL") == 2
    assert hl._round_size("SOL", 1.239) == 1.23


def test_round_size_unknown_symbol_raises():
    with pytest.raises(hl.HyperliquidError):
        hl._round_size("DOGE", 1.0)


def test_round_price_uses_hyperliquid_precision_rules():
    # BTC hat szDecimals=5 -> höchstens eine Nachkommastelle bei Perps.
    assert hl._round_price("BTC", 77460.07) == 77460.0
    assert hl._round_price("ETH", 2450.12345) == 2450.1


# --- OrderResult / GuardResult-artige Wahrheitswerte ---

def test_order_result_bool():
    assert bool(hl.OrderResult(status="filled")) is True
    assert bool(hl.OrderResult(status="error", error="x")) is False


# --- LiveExchange: Konstruktor-Guard ---

def test_live_exchange_requires_credentials(monkeypatch):
    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: (None, None))
    with pytest.raises(hl.HyperliquidError):
        hl.LiveExchange()


def test_live_account_state_reads_unified_usdc_balance(monkeypatch):
    """Unified accounts expose collateral via spotClearinghouseState."""
    class _FakeInfo:
        def user_state(self, address):
            return {
                "assetPositions": [],
                "marginSummary": {"accountValue": "0.0"},
                "withdrawable": "0.0",
            }

        def query_user_abstraction_state(self, address):
            return "unifiedAccount"

        def spot_user_state(self, address):
            return {"balances": [{"coin": "USDC", "total": "44.6", "hold": "0.0"}]}

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress"
    exchange._info = _FakeInfo()

    state = exchange.account_state()

    assert state.equity_usd == pytest.approx(44.6)
    assert state.withdrawable_usd == pytest.approx(44.6)
    assert state.equity_trusted is True


def test_live_account_state_uses_unified_balance_even_with_open_position(monkeypatch):
    """Regressionstest für einen live beobachteten Vorfall: bei einer offenen
    Position auf einem Unified-Margin-Konto lieferte `marginSummary.
    accountValue` nur die für die Position hinterlegte Margin (5.92 $) statt
    der tatsächlichen Gesamt-Equity (~44.5 $) - klein, aber NICHT <= 0. Die
    alte Bedingung `if equity_usd <= 0` griff deshalb nicht und meldete eine
    um >85% zu niedrige Equity, was den Equity-Boden-Kill-Switch fälschlich
    hätte auslösen können. Die Unified-Rechnung muss deshalb IMMER greifen,
    sobald der Kontomodus 'unifiedAccount'/'portfolioMargin' ist - nicht nur
    als Rückfall bei einem exakt nullwertigen classic accountValue.

    `usdc.total` (44.5) ist bei diesem Kontomodus bereits die vollständig
    bewertete Gesamt-Equity inklusive des offenen PnL der Position - NICHT
    zusätzlich `total + unrealized_pnl_usd` addieren (das zählte das offene
    PnL ein zweites Mal, live nachgerechnet gegen Hyperliquids eigene
    Portfolio-Historie bestätigt)."""
    class _FakeInfo:
        def user_state(self, address):
            return {
                "assetPositions": [{"position": {
                    "coin": "NEAR", "szi": "-6.5", "entryPx": "1.8211",
                    "leverage": {"value": 1.9}, "unrealizedPnl": "-0.05",
                }}],
                "marginSummary": {"accountValue": "5.92"},  # klein, aber > 0
                "withdrawable": "0.0",
            }

        def query_user_abstraction_state(self, address):
            return "unifiedAccount"

        def spot_user_state(self, address):
            return {"balances": [{"coin": "USDC", "total": "44.5", "hold": "0.0"}]}

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress"
    exchange._info = _FakeInfo()

    state = exchange.account_state()

    assert state.equity_usd == pytest.approx(44.5)
    assert len(state.positions) == 1
    assert state.equity_trusted is True


def test_live_account_state_unified_balance_not_doubled_with_profit(monkeypatch):
    """Spiegelbild-Fall zu oben: ein offener GEWINN darf die Unified-Equity
    ebenfalls nicht ein zweites Mal erhöhen. `usdc.total` (50.0) enthält den
    offenen Gewinn der Position bereits; `unrealized_pnl_usd` (+3.0) darf
    nicht nochmals addiert werden, sonst würden Positionsgröße und
    Heat-Budget auf Basis einer zu hohen, nicht gedeckten Equity bemessen."""
    class _FakeInfo:
        def user_state(self, address):
            return {
                "assetPositions": [{"position": {
                    "coin": "ETH", "szi": "0.02", "entryPx": "2400.0",
                    "leverage": {"value": 2.0}, "unrealizedPnl": "3.0",
                }}],
                "marginSummary": {"accountValue": "24.0"},
                "withdrawable": "0.0",
            }

        def query_user_abstraction_state(self, address):
            return "unifiedAccount"

        def spot_user_state(self, address):
            return {"balances": [{"coin": "USDC", "total": "50.0", "hold": "0.0"}]}

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress-profit"
    exchange._info = _FakeInfo()

    state = exchange.account_state()

    assert state.equity_usd == pytest.approx(50.0)
    assert state.equity_trusted is True


def test_live_account_state_falls_back_to_classic_when_abstraction_lookup_fails():
    """Ein fehlgeschlagener Zusatzaufruf darf die Basis-Antwort nicht kippen -
    equity_usd bleibt der klassische Wert, ABER equity_trusted wird False:
    ohne Kenntnis des Kontomodus ist NICHT sicher, dass dieser Wert stimmt
    (er war es live einmal nicht, siehe test_live_account_state_uses_unified_
    balance_even_with_open_position)."""
    class _FakeInfo:
        def user_state(self, address):
            return {"assetPositions": [], "marginSummary": {"accountValue": "12.0"},
                    "withdrawable": "12.0"}

        def query_user_abstraction_state(self, address):
            raise RuntimeError("timeout")

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress-fallback"
    exchange._info = _FakeInfo()

    state = exchange.account_state()
    assert state.equity_usd == pytest.approx(12.0)
    assert state.equity_trusted is False


def test_live_account_state_untrusted_when_unified_but_no_usdc_entry():
    """Kontomodus erkannt (unified), aber kein USDC-Eintrag im Spot-Saldo -
    equity_usd bleibt der (vermutlich falsche) classic-Wert, equity_trusted
    muss False sein statt den Fehlwert stillschweigend als Wahrheit zu
    behandeln."""
    class _FakeInfo:
        def user_state(self, address):
            return {"assetPositions": [], "marginSummary": {"accountValue": "5.92"},
                    "withdrawable": "0.0"}

        def query_user_abstraction_state(self, address):
            return "unifiedAccount"

        def spot_user_state(self, address):
            return {"balances": [{"coin": "ETH", "total": "1.0", "hold": "0.0"}]}

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress-no-usdc"
    exchange._info = _FakeInfo()

    state = exchange.account_state()
    assert state.equity_usd == pytest.approx(5.92)
    assert state.equity_trusted is False


def test_live_account_state_untrusted_when_spot_lookup_fails():
    class _FakeInfo:
        def user_state(self, address):
            return {"assetPositions": [], "marginSummary": {"accountValue": "5.92"},
                    "withdrawable": "0.0"}

        def query_user_abstraction_state(self, address):
            return "unifiedAccount"

        def spot_user_state(self, address):
            raise RuntimeError("timeout")

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress-spot-fails"
    exchange._info = _FakeInfo()

    state = exchange.account_state()
    assert state.equity_trusted is False


def test_live_account_state_ignores_unified_balance_for_classic_accounts():
    """Ein normales (nicht Unified-) Konto behält seinen klassischen Wert,
    selbst wenn irgendwo spot-USDC läge - kein falsches Aufaddieren."""
    class _FakeInfo:
        def user_state(self, address):
            return {"assetPositions": [], "marginSummary": {"accountValue": "9.0"},
                    "withdrawable": "9.0"}

        def query_user_abstraction_state(self, address):
            return "standard"

    exchange = object.__new__(hl.LiveExchange)
    exchange._address = "0xaddress-classic"
    exchange._info = _FakeInfo()

    state = exchange.account_state()
    assert state.equity_usd == pytest.approx(9.0)
    assert state.equity_trusted is True


# --- LiveExchange: reine Antwort-Parser (kein Netzwerk, keine Instanz noetig) ---

def test_parse_order_response_filled():
    resp = {"response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 77747314}}
    ]}}}
    result = hl.LiveExchange._parse_order_response(resp)
    assert result.status == "filled"
    assert result.fill_px == 1891.4
    assert result.filled_size == 0.02
    assert result.fee_usd == pytest.approx(0.02 * 1891.4 * hl.TAKER_FEE)


def test_parse_order_response_error():
    resp = {"response": {"data": {"statuses": [
        {"error": "Order must have minimum value of $10."}
    ]}}}
    result = hl.LiveExchange._parse_order_response(resp)
    assert result.status == "error"
    assert "minimum value" in result.error


def test_parse_order_response_resting_is_treated_as_error():
    """Eine IOC-Market-Order, die nicht sofort voll gefuellt wird, darf NIE
    stillschweigend als Erfolg durchgehen."""
    resp = {"response": {"data": {"statuses": [{"resting": {"oid": 77738308}}]}}}
    result = hl.LiveExchange._parse_order_response(resp)
    assert result.status == "error"
    assert "77738308" in result.error


def test_parse_order_response_malformed():
    result = hl.LiveExchange._parse_order_response({"unexpected": "shape"})
    assert result.status == "error"


def test_parse_order_response_empty_statuses():
    resp = {"response": {"data": {"statuses": []}}}
    result = hl.LiveExchange._parse_order_response(resp)
    assert result.status == "error"


def test_parse_stop_oid_from_resting_and_filled():
    resting = {"response": {"data": {"statuses": [{"resting": {"oid": 42}}]}}}
    filled = {"response": {"data": {"statuses": [{"filled": {"oid": 43, "totalSz": "1", "avgPx": "1"}}]}}}
    assert hl.LiveExchange._parse_stop_oid(resting) == "42"
    assert hl.LiveExchange._parse_stop_oid(filled) == "43"


def test_parse_stop_oid_malformed_returns_none():
    assert hl.LiveExchange._parse_stop_oid({}) is None
    assert hl.LiveExchange._parse_stop_oid(
        {"response": {"data": {"statuses": [{"resting": {"oid": None}}]}}}
    ) is None


def test_live_entry_stop_without_oid_is_not_accepted(monkeypatch):
    """Eine scheinbar erfolgreiche, aber nicht identifizierbare Stop-Antwort
    darf nie als abgesicherte Position gelten."""
    class _FakeExchange:
        def update_leverage(self, *args, **kwargs):
            return None

        def market_open(self, *args, **kwargs):
            return {"response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.02", "avgPx": "78900", "oid": 1}}
            ]}}}

        def order(self, *args, **kwargs):
            return {"response": {"data": {"statuses": [{"error": "ambiguous"}]}}}

        def frontend_open_orders(self, address):
            return []

    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: ("key", "0xaddress"))
    monkeypatch.setattr(hl, "supported_symbols", lambda: {"BTC"})
    monkeypatch.setattr(hl, "mid_price", lambda symbol: 78900.0)
    monkeypatch.setattr(hl, "max_leverage_for", lambda symbol: 40)
    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    exchange._address = "0xaddress"
    exchange._leverage_set = {}

    result = exchange.open_long("BTC", 12.0, 1.0, 70000.0)

    assert result.status == "filled"
    assert result.stop_oid is None
    assert "Stop-Order" in result.error


def test_live_entry_retains_stop_oid_when_frontend_verification_times_out(monkeypatch):
    """Auch bei verspäteter Sichtbarkeit bleibt die OID für den Notausstieg
    erhalten; sie darf nicht durch die Fehlerbehandlung verloren gehen."""
    class _FakeExchange:
        def update_leverage(self, *args, **kwargs):
            return None

        def market_open(self, *args, **kwargs):
            return {"response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.02", "avgPx": "78900", "oid": 1}}
            ]}}}

        def order(self, *args, **kwargs):
            return {"response": {"data": {"statuses": [
                {"resting": {"oid": 777}}
            ]}}}

        def frontend_open_orders(self, address):
            return []

    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: ("key", "0xaddress"))
    monkeypatch.setattr(hl, "supported_symbols", lambda: {"BTC"})
    monkeypatch.setattr(hl, "mid_price", lambda symbol: 78900.0)
    monkeypatch.setattr(hl, "max_leverage_for", lambda symbol: 40)
    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    exchange._address = "0xaddress"
    exchange._leverage_set = {}

    result = exchange.open_long("BTC", 12.0, 1.0, 70000.0)

    assert result.status == "filled"
    assert result.stop_oid == "777"
    assert "nicht als sichtbare" in result.error


# --- LiveExchange: cancel_order / recent_fills / funding_payments_usd ---
# Antwortformen gegen das echte, verknüpfte Konto verifiziert (read-only).

def test_cancel_order_success():
    class _FakeExchange:
        def cancel(self, symbol, oid):
            assert symbol == "BTC" and oid == 123
            return {"status": "ok"}

    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    assert exchange.cancel_order("BTC", "123") is True


def test_cancel_order_failure_returns_false_not_raises():
    class _FakeExchange:
        def cancel(self, symbol, oid):
            raise RuntimeError("boom")

    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    assert exchange.cancel_order("BTC", "123") is False


def test_cancel_order_non_ok_status_returns_false():
    class _FakeExchange:
        def cancel(self, symbol, oid):
            return {"status": "err", "response": "already canceled"}

    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    assert exchange.cancel_order("BTC", "123") is False


def test_recent_fills_filters_by_time_and_matches_real_response_shape():
    # Verifizierte Feldform gegen das echte Konto: coin, px, sz, side, dir,
    # closedPnl, fee, feeToken, time, oid, tid.
    fills = [
        {"coin": "BTC", "px": "78242.0", "sz": "0.00015", "fee": "0.005281",
         "feeToken": "USDC", "time": 1000, "oid": 1, "tid": 1},
        {"coin": "BTC", "px": "78228.0", "sz": "0.00015", "fee": "0.00528",
         "feeToken": "USDC", "time": 2000, "oid": 2, "tid": 2},
    ]

    class _FakeInfo:
        def user_fills(self, address):
            return fills

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"

    assert exchange.recent_fills(1500) == [fills[1]]
    assert exchange.recent_fills(0) == fills


def test_recent_fills_non_list_response_returns_empty():
    class _FakeInfo:
        def user_fills(self, address):
            return {"unexpected": "shape"}

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"
    assert exchange.recent_fills(0) == []


def test_funding_payments_usd_negates_raw_usdc_sign():
    """Rohes 'usdc'-Feld: negativ = Geld hat das Konto verlassen. Unsere
    Konvention (core.bot): positiv = Kosten. Beide muessen invertiert sein."""
    entries = [
        {"delta": {"type": "funding", "coin": "BTC", "usdc": "-3.625312"}, "time": 1000},
        {"delta": {"type": "funding", "coin": "ETH", "usdc": "1.5"}, "time": 2000},
        {"delta": {"type": "someOtherType", "usdc": "999"}, "time": 3000},  # muss ignoriert werden
    ]

    class _FakeInfo:
        def user_funding_history(self, user, startTime):
            assert user == "0xaddress"
            return entries

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"

    result = exchange.funding_payments_usd(0)
    assert result == pytest.approx(3.625312 - 1.5)


def test_funding_payments_usd_paginates_beyond_a_single_page(monkeypatch):
    """Bugfix-Regressionstest: `user_funding_history` liefert je Aufruf nur
    eine begrenzte Seite - beim Audit fehlte ohne Fortsetzung ein Eintrag
    aus 545. `_FUNDING_HISTORY_PAGE_SIZE` wird hier auf 2 heruntergesetzt,
    um die Fortsetzung mit einer kleinen, lesbaren Fixture zu erzwingen."""
    monkeypatch.setattr(hl, "_FUNDING_HISTORY_PAGE_SIZE", 2)
    pages = [
        [
            {"delta": {"type": "funding", "coin": "BTC", "usdc": "-1.0"}, "time": 1000},
            {"delta": {"type": "funding", "coin": "BTC", "usdc": "-2.0"}, "time": 2000},
        ],
        [
            {"delta": {"type": "funding", "coin": "ETH", "usdc": "0.5"}, "time": 3000},
        ],
    ]
    calls = []

    class _FakeInfo:
        def user_funding_history(self, user, startTime):
            calls.append(startTime)
            return pages[len(calls) - 1] if len(calls) <= len(pages) else []

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"

    result = exchange.funding_payments_usd(0)

    assert result == pytest.approx(1.0 + 2.0 - 0.5)   # -(-1)-(-2)-(0.5)
    # Zweite Seite ab newest(2000)+1; die zweite Antwort hat nur 1 Eintrag
    # (< Seitengroesse 2) -> Abbruch, kein dritter Aufruf noetig.
    assert calls == [0, 2001]


def test_funding_payments_usd_non_list_response_returns_zero():
    class _FakeInfo:
        def user_funding_history(self, user, startTime):
            return None

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"
    assert exchange.funding_payments_usd(0) == 0.0


def test_deposit_withdrawal_usd_handles_real_send_type_by_direction():
    """Gegen das echte, verknuepfte Konto verifiziert: eine externe
    Einzahlung erscheint NICHT als delta.type 'deposit', sondern als 'send'
    mit 'destination' == der eigenen Adresse und dem Betrag im Feld
    'usdcValue' (NICHT 'usdc', das Feld existiert bei 'send' gar nicht).
    Eine Auszahlung ist symmetrisch: 'send' mit 'user' (Absender) == der
    eigenen Adresse. Andere Ledger-Typen (Funding) werden ignoriert."""
    entries = [
        # echte Einzahlung: Geld kommt bei UNSERER Adresse an
        {"delta": {"type": "send", "user": "0xsomeone-else",
                   "destination": "0xADDRESS", "usdcValue": "128.31"}, "time": 1000},
        # echte Auszahlung: WIR sind der Absender
        {"delta": {"type": "send", "user": "0xADDRESS",
                   "destination": "0xsomeone-else", "usdcValue": "20.0"}, "time": 2000},
        {"delta": {"type": "funding", "usdc": "999.0"}, "time": 3000},  # muss ignoriert werden
    ]

    class _FakeInfo:
        def user_non_funding_ledger_updates(self, user, startTime):
            assert user == "0xaddress"
            return entries

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"  # Adressvergleich ist case-insensitive

    result = exchange.deposit_withdrawal_usd(0)
    assert result == pytest.approx(128.31 - 20.0)


def test_deposit_withdrawal_usd_supports_bridge_deposit_withdraw_types():
    """Die urspruenglich aus der SDK-Docstring angenommenen Typen
    'deposit'/'withdraw' (fuer den L1-Bridge-Pfad, nie live beobachtet)
    bleiben als Fallback unterstuetzt - Betrag wird als UNSIGNED Betrag
    behandelt, die Richtung steckt im type-Namen."""
    entries = [
        {"delta": {"type": "deposit", "usdc": "500.0"}, "time": 1000},
        {"delta": {"type": "withdraw", "usdc": "200.0"}, "time": 2000},
        {"delta": {"type": "accountClassTransfer", "usdc": "999.0"}, "time": 3000},  # unklar -> ignoriert
    ]

    class _FakeInfo:
        def user_non_funding_ledger_updates(self, user, startTime):
            return entries

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"

    result = exchange.deposit_withdrawal_usd(0)
    assert result == pytest.approx(300.0)


def test_deposit_withdrawal_usd_non_list_response_returns_zero():
    class _FakeInfo:
        def user_non_funding_ledger_updates(self, user, startTime):
            return None

    exchange = object.__new__(hl.LiveExchange)
    exchange._info = _FakeInfo()
    exchange._address = "0xaddress"
    assert exchange.deposit_withdrawal_usd(0) == 0.0


# --- PaperExchange ---

def test_paper_exchange_starts_with_full_cash_no_positions():
    ex = hl.PaperExchange(starting_equity_usd=250.0)
    state = ex.account_state()
    assert state.equity_usd == 250.0
    assert state.withdrawable_usd == 250.0
    assert state.positions == []


def test_paper_exchange_open_long_deducts_fee_and_creates_position():
    ex = hl.PaperExchange(starting_equity_usd=250.0)
    result = ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    fill = hl.slipped_price(78900.0, "long")
    assert result.status == "filled"
    assert result.fill_px == pytest.approx(fill)
    assert result.filled_size == pytest.approx(50.0 / fill)
    assert result.fee_usd == pytest.approx(50.0 * hl.TAKER_FEE)

    state = ex.account_state()
    assert len(state.positions) == 1
    pos = state.positions[0]
    assert pos.symbol == "BTC" and pos.side == "long"
    # Der Einstand liegt UEBER dem mid_price: ein Long kauft mit Slippage
    # teurer. Genau diese Differenz fehlte frueher komplett.
    assert pos.entry_px > 78900.0
    # Unrealisiert steht die frische Position um die Slippage im Minus - ein
    # Trade ist ab der ersten Sekunde ein Stueck im Rueckstand, nicht bei 0.
    assert pos.unrealized_pnl_usd == pytest.approx((78900.0 - fill) * pos.size)
    assert state.equity_usd == pytest.approx(
        250.0 - 50.0 * hl.TAKER_FEE + (78900.0 - fill) * pos.size)


def test_paper_exchange_unrealized_pnl_tracks_price_moves(monkeypatch):
    prices = {"BTC": 78900.0}
    ex = hl.PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: prices.get(s))
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)

    prices["BTC"] = 79900.0  # Long gewinnt
    state = ex.account_state()
    fill = hl.slipped_price(78900.0, "long")
    size = 50.0 / fill
    assert state.positions[0].unrealized_pnl_usd == pytest.approx((79900.0 - fill) * size)


def test_paper_exchange_short_pnl_direction():
    prices = {"BTC": 78900.0}
    ex = hl.PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: prices.get(s))
    ex.open_short("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)

    prices["BTC"] = 77900.0  # Kurs faellt -> Short gewinnt
    state = ex.account_state()
    fill = hl.slipped_price(78900.0, "short")   # Short verkauft billiger
    assert fill < 78900.0
    size = 50.0 / fill
    assert state.positions[0].unrealized_pnl_usd == pytest.approx((fill - 77900.0) * size)


def test_paper_exchange_nachkauf_averages_entry_price():
    prices = {"BTC": 78900.0}
    ex = hl.PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: prices.get(s))
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    fill1 = hl.slipped_price(78900.0, "long")
    size1 = 50.0 / fill1

    prices["BTC"] = 79900.0
    ex.open_long("BTC", notional_usd=30.0, leverage=1.0, stop_distance_pct=5.0)
    fill2 = hl.slipped_price(79900.0, "long")
    size2 = 30.0 / fill2

    pos = ex.account_state().positions[0]
    expected_entry = (fill1 * size1 + fill2 * size2) / (size1 + size2)
    assert pos.size == pytest.approx(size1 + size2)
    assert pos.entry_px == pytest.approx(expected_entry)


def test_paper_exchange_rejects_opposite_direction_without_closing():
    ex = hl.PaperExchange(starting_equity_usd=250.0)
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    result = ex.open_short("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    assert result.status == "error"
    assert "Gegenrichtung" in result.error
    # ursprüngliche Long-Position bleibt unangetastet
    assert ex.account_state().positions[0].side == "long"


def test_paper_exchange_close_position_realizes_pnl():
    prices = {"BTC": 78900.0}
    ex = hl.PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: prices.get(s))
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    fee_open = 50.0 * hl.TAKER_FEE
    entry = hl.slipped_price(78900.0, "long")
    size = 50.0 / entry

    prices["BTC"] = 79900.0
    result = ex.close_position("BTC")
    exit_px = hl.slipped_price(79900.0, "long", closing=True)

    assert result.status == "filled"
    assert result.fill_px == pytest.approx(exit_px)
    assert exit_px < 79900.0    # Long verkauft billiger als der mid_price
    fee_close = exit_px * size * hl.TAKER_FEE
    expected_pnl = (exit_px - entry) * size
    assert ex.account_state().equity_usd == pytest.approx(
        250.0 - fee_open - fee_close + expected_pnl
    )
    assert ex.account_state().positions == []


def test_paper_slippage_is_always_adverse():
    """Slippage darf nie zugunsten des Handelnden wirken - sonst waere sie
    eine Ertragsquelle statt einer Kostenposition, und das Papierergebnis
    wieder zu optimistisch."""
    px = 100.0
    assert hl.slipped_price(px, "long") > px                    # Long kauft teurer
    assert hl.slipped_price(px, "long", closing=True) < px      # und verkauft billiger
    assert hl.slipped_price(px, "short") < px                   # Short verkauft billiger
    assert hl.slipped_price(px, "short", closing=True) > px     # und kauft teurer


def test_paper_round_trip_without_price_move_loses_money():
    """Sofort wieder geschlossen und ohne jede Kursbewegung muss ein Trade
    Geld KOSTEN (Gebuehren + Slippage). Vorher fuellte das Papierdepot exakt
    zum mid_price, ein Nullsummen-Round-Trip kostete nur die Gebuehr."""
    ex = hl.PaperExchange(starting_equity_usd=250.0)
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    ex.close_position("BTC")
    equity = ex.account_state().equity_usd
    assert equity < 250.0 - 2 * 50.0 * hl.TAKER_FEE


def test_paper_exchange_close_without_position_errors():
    ex = hl.PaperExchange(starting_equity_usd=250.0)
    result = ex.close_position("BTC")
    assert result.status == "error"


def test_paper_exchange_rejects_order_when_fee_exceeds_cash():
    ex = hl.PaperExchange(starting_equity_usd=0.0001)
    result = ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)
    assert result.status == "error"


def test_paper_exchange_missing_price_falls_back_to_last_known():
    prices = {"BTC": 78900.0}
    ex = hl.PaperExchange(starting_equity_usd=250.0, price_fn=lambda s: prices.get(s))
    ex.open_long("BTC", notional_usd=50.0, leverage=1.0, stop_distance_pct=5.0)

    prices.pop("BTC")  # simuliert einen API-Ausfall
    state = ex.account_state()
    assert len(state.positions) == 1  # kein Absturz, Position bleibt sichtbar


def test_ensure_leverage_returns_the_floored_applied_value(monkeypatch):
    """Hyperliquid akzeptiert nur Ganzzahl-Hebel - der Rueckgabewert muss der
    TATSAECHLICH gesetzte (abgerundete) Hebel sein, nicht der angeforderte.
    core.bot.open_position() speichert genau diesen Wert in der DB."""
    calls = []

    class _FakeExchange:
        def update_leverage(self, leverage, symbol, is_cross=True):
            calls.append((leverage, symbol, is_cross))

    monkeypatch.setattr(hl, "max_leverage_for", lambda symbol: 40)
    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    exchange._leverage_set = {}

    applied = exchange._ensure_leverage("BTC", 1.9)

    assert applied == 1.0
    assert calls == [(1, "BTC", True)]


def test_ensure_leverage_skips_redundant_calls_but_still_returns_applied(monkeypatch):
    """Ein wiederholter Aufruf mit demselben angeforderten Wert darf keine
    zweite Boersenanfrage ausloesen, muss aber trotzdem den abgerundeten Wert
    zurueckgeben - sonst haette die fruehere Kurzschluss-Rueckgabe (None)
    core.bot dazu gebracht, den unveraenderten Rohwert zu speichern."""
    calls = []

    class _FakeExchange:
        def update_leverage(self, leverage, symbol, is_cross=True):
            calls.append(leverage)

    monkeypatch.setattr(hl, "max_leverage_for", lambda symbol: 40)
    exchange = object.__new__(hl.LiveExchange)
    exchange._exchange = _FakeExchange()
    exchange._leverage_set = {}

    first = exchange._ensure_leverage("BTC", 1.9)
    second = exchange._ensure_leverage("BTC", 1.9)

    assert first == second == 1.0
    assert calls == [1]      # nur EIN echter Boersenaufruf
