"""Phase-F-Readiness: nur Read-only-Prüfung, niemals eine Order."""
from dataclasses import dataclass

from core import bot_config, bot_live, config
from data.hyperliquid import Position as HLPosition


def test_preflight_stops_before_exchange_without_credentials(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: (None, None))

    result = bot_live.read_only_preflight()

    assert result["ok"] is False
    assert result["equity_usd"] is None
    assert any(not check["ok"] for check in result["checks"])


def test_preflight_reads_account_but_never_exposes_credentials(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: ("secret", "0xpublic"))

    @dataclass
    class _State:
        equity_usd: float = 250.0
        positions: list = None

        def __post_init__(self):
            self.positions = self.positions or []

    class _Exchange:
        def __init__(self):
            self.reads = 0

        def account_state(self):
            self.reads += 1
            return _State()

        def frontend_open_orders(self):
            return []

    exchange = _Exchange()
    monkeypatch.setattr("data.hyperliquid.LiveExchange", lambda: exchange)
    monkeypatch.setattr("data.hyperliquid.supported_symbols", lambda: {"BTC"})

    result = bot_live.read_only_preflight("BTC")

    assert result["ok"] is True
    assert result["equity_usd"] == 250.0
    assert exchange.reads == 1
    assert "secret" not in str(result)
    # Voraussetzung fuer den Live-Schalter (core.bot_config.preflight_is_fresh):
    # ein Erfolg muss sofort als "frisch" gelten.
    assert bot_config.preflight_is_fresh() is True


def test_preflight_failure_does_not_record_timestamp(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: (None, None))
    bot_live.read_only_preflight()
    assert bot_config.last_preflight_ok_at() is None


def test_preflight_requires_flat_account(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "hyperliquid_credentials", lambda: ("secret", "0xpublic"))

    @dataclass
    class _State:
        equity_usd: float = 250.0
        positions: list = None

        def __post_init__(self):
            self.positions = self.positions or [HLPosition(
                symbol="BTC", side="long", size=0.001, entry_px=80000.0,
                leverage=1.0, unrealized_pnl_usd=0.0,
            )]

    class _Exchange:
        def account_state(self):
            return _State()

        def frontend_open_orders(self):
            return []

    monkeypatch.setattr("data.hyperliquid.LiveExchange", lambda: _Exchange())
    monkeypatch.setattr("data.hyperliquid.supported_symbols", lambda: {"BTC"})

    result = bot_live.read_only_preflight("BTC")

    assert result["ok"] is False
    assert result["positions"] == ["BTC"]
    assert any("Keine offenen Hyperliquid-Positionen" in c["label"] and not c["ok"]
               for c in result["checks"])
    assert bot_config.last_preflight_ok_at() is None
