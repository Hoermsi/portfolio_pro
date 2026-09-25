"""core/portfolio.py: Bewertung von Depot-Positionen inkl. der synthetischen
'Krypto Bot'-Zeile (core.portfolio.crypto_bot_valuation()) - bündelt den
Hyperliquid-Trading-Bot-Kontowert zu EINER Position fürs Krypto-Depot."""
import pytest

from core import bot, portfolio
from data.hyperliquid import PaperExchange


def test_crypto_bot_valuation_none_when_bot_inactive(tmp_db):
    assert portfolio.crypto_bot_valuation() is None


def test_crypto_bot_valuation_none_without_any_equity_measurement(tmp_db):
    """Aktiv (bot_start gesetzt), aber noch nie eine Equity gemessen -
    performance_summary() liefert None, also auch die Bewertung."""
    tmp_db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": 100.0}')
    assert bot.is_active() is True
    assert portfolio.crypto_bot_valuation() is None


def test_crypto_bot_valuation_reflects_current_equity_and_pnl(tmp_db, monkeypatch):
    from data import fx as fx_data
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda currency: 0.9)

    exchange = PaperExchange(starting_equity_usd=100.0)
    bot.initialize(exchange)
    exchange._cash_usd = 150.0  # +50 $ Gewinn, keine Ein-/Auszahlung
    bot.record_equity_snapshot(exchange)

    val = portfolio.crypto_bot_valuation()

    assert val is not None
    assert val.position.id == -1
    assert val.position.symbol == "BOT"
    assert val.position.name == "Krypto Bot"
    assert val.position.category == "Hyperliquid"
    assert val.value_eur == pytest.approx(150.0 * 0.9)
    # G/V (value_eur - cost_basis) muss exakt dem Ein-/Auszahlungs-
    # bereinigten PNL der Trading-Bot-Seite entsprechen - keine zweite,
    # abweichende Gewinn/Verlust-Zahl fuer denselben Bot.
    summary = bot.performance_summary()
    assert val.gain_abs == pytest.approx(summary["pnl_usd"] * 0.9)


def test_crypto_bot_valuation_cost_basis_accounts_for_deposit(tmp_db, monkeypatch):
    """Eine Einzahlung darf nicht als Gewinn in der 'Krypto Bot'-Zeile
    erscheinen - derselbe Grundsatz wie beim PNL auf der Trading-Bot-Seite."""
    from core import db
    from data import fx as fx_data
    monkeypatch.setattr(fx_data, "get_fx_to_eur", lambda currency: 1.0)

    exchange = PaperExchange(starting_equity_usd=100.0)
    bot.initialize(exchange)
    exchange._cash_usd = 200.0  # +100 $, davon 100 $ reine Einzahlung
    db.set_meta("bot_net_flows_usd", "100.0")
    bot.record_equity_snapshot(exchange)

    val = portfolio.crypto_bot_valuation()

    assert val.value_eur == pytest.approx(200.0)
    assert val.gain_abs == pytest.approx(0.0)  # kein Handelsgewinn, nur Einzahlung
