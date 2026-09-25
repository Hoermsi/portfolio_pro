"""Netto-Performance und 90-Tage-Abbruchkriterium ohne Netz-/UI-Abhängigkeit."""
from datetime import datetime

import pandas as pd
import pytest

from core import bot, db


def _initialize(tmp_db, start=250.0):
    db.set_meta("bot_start", '{"date": "2026-01-01", "equity_start_usd": %s}' % start)


def test_performance_summary_keeps_costs_separate_from_net_equity(tmp_db):
    _initialize(tmp_db)
    db.add_bot_equity_point(245, 230, 200, fees_cum_usd=2, funding_cum_usd=3,
                            ts="2026-04-01T12:00:00")

    summary = bot.performance_summary(now=datetime(2026, 4, 1))

    assert summary["net_pnl_usd"] == -5
    assert summary["costs_usd"] == 5
    assert summary["gross_profit_usd"] == 0
    assert summary["evaluation_due"] is True
    assert summary["costs_over_double_gross_profit"] is True


def test_performance_summary_does_not_treat_funding_income_as_negative_cost(tmp_db):
    _initialize(tmp_db)
    db.add_bot_equity_point(255, 240, 200, fees_cum_usd=2, funding_cum_usd=-4,
                            ts="2026-01-02T12:00:00")

    summary = bot.performance_summary(now=datetime(2026, 1, 2))

    assert summary["costs_usd"] == 2
    assert summary["gross_profit_usd"] == 7


def test_abort_reasons_apply_only_after_the_90_day_window(tmp_db):
    _initialize(tmp_db)
    db.add_bot_equity_point(190, 180, 180, fees_cum_usd=1, funding_cum_usd=0,
                            ts="2026-01-02T12:00:00")

    before_due = bot.performance_summary(now=datetime(2026, 3, 31))
    after_due = bot.performance_summary(now=datetime(2026, 4, 1))

    assert bot.abort_reasons(before_due) == []
    assert "Netto-Equity unter 80 % des Startkapitals" in bot.abort_reasons(after_due)[0]


def test_performance_index_includes_net_bot_eur_and_btc_hold(tmp_db):
    db.add_bot_equity_point(250, 200, 200, 0, 0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(260, 220, 200, 1, 0, ts="2026-01-02T12:00:00")
    btc = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))

    result = bot.performance_index_df(btc)

    assert list(result.columns) == ["Bot netto", "EUR halten", "BTC Buy & Hold"]
    assert result["Bot netto"].iloc[-1] == pytest.approx(110)
    assert result["EUR halten"].iloc[-1] == pytest.approx(100)
    assert result["BTC Buy & Hold"].iloc[-1] == pytest.approx(110)


def test_performance_index_gracefully_omits_unaligned_btc_history(tmp_db):
    db.add_bot_equity_point(250, 200, 200, 0, 0, ts="2026-01-01T12:00:00")
    btc = pd.Series([100.0], index=pd.to_datetime(["2026-02-01"]))

    result = bot.performance_index_df(btc)

    assert list(result.columns) == ["Bot netto", "EUR halten"]


def test_performance_index_handles_tz_aware_equity_with_tz_naive_btc_history(tmp_db):
    """core/clock.py (Phase 1) schreibt bot_equity.ts seither TZ-AWARE UTC
    (z.B. '...+00:00'), waehrend data.crypto_history-Reihen wie im Rest
    dieser Datei TZ-NAIV bleiben (reine Kalendertage aus einer unabhaengigen
    Quelle) - ohne Angleichung in core.bot.performance_index_df() wirft
    pandas 'Cannot compare tz-naive and tz-aware timestamps'. Blieb
    unbemerkt, weil bot_equity seit dem V2-Reset (Phase 0) bis zu diesem Bug
    leer war - dieser Test reproduziert die ECHTE Kombination."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, ts="2026-01-01T12:00:00+00:00")
    db.add_bot_equity_point(260, 220, 200, 1, 0, ts="2026-01-02T12:00:00+00:00")
    btc = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))

    result = bot.performance_index_df(btc)

    assert list(result.columns) == ["Bot netto", "EUR halten", "BTC Buy & Hold"]
    assert result["BTC Buy & Hold"].iloc[-1] == pytest.approx(110)


def test_performance_index_handles_multiple_intraday_points_on_same_day(tmp_db):
    """Der Bot schreibt intraday; der BTC-Tageslookup darf daran nicht scheitern."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, ts="2026-01-01T10:00:00")
    db.add_bot_equity_point(252, 202, 200, 0, 0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(260, 220, 200, 0, 0, ts="2026-01-02T12:00:00")
    # Auch externe Historien können doppelte Tageslabels liefern.
    btc = pd.Series(
        [100.0, 101.0, 110.0],
        index=pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02"]),
    )

    result = bot.performance_index_df(btc)

    assert result is not None
    assert len(result) == 3
    assert result["BTC Buy & Hold"].tolist() == pytest.approx(
        [100.0, 100.0, 110.0 / 101.0 * 100.0]
    )


def test_performance_index_does_not_show_a_deposit_as_a_return_spike(tmp_db):
    """Review-Punkt (Runde 3, 'Einschaetzung zur Rentabilitaet'): performance_summary()
    ist laengst um Kapitalfluesse bereinigt (net_flows_usd, siehe dort) - der
    EUR-indizierte Chart hier rechnete aber weiterhin mit der rohen equity_eur.
    Kernfall: 100 $ Einzahlung bei UNVERAENDERTEM Handelsergebnis darf den
    Index-Chart nicht nach oben springen lassen."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, net_flows_usd=0, ts="2026-01-01T12:00:00")
    # Einzahlung von 100 $ = 80 EUR bei einem 0,8-EUR/USD-Kurs (aus equity_eur/equity_usd
    # dieser Zeile ableitbar) - equity springt von 200 auf 280 EUR, OHNE dass sich am
    # Handelsergebnis etwas geaendert hat.
    db.add_bot_equity_point(350, 280, 200, 0, 0, net_flows_usd=100, ts="2026-01-02T12:00:00")

    result = bot.performance_index_df()

    assert result is not None
    # Ohne Bereinigung waere das 280/200*100 = 140 - der Test beweist, dass die
    # Einzahlung herausgerechnet wird und der Index bei ~100 (unveraendert) bleibt.
    assert result["Bot netto"].iloc[-1] == pytest.approx(100.0)


def test_performance_index_still_shows_real_trading_gains_after_a_deposit(tmp_db):
    """Gegenprobe zum Deposit-Test: ein ECHTER Handelsgewinn NACH einer
    Einzahlung darf nicht mit-herausgerechnet werden. Dieselbe einfache
    Differenzrechnung wie performance_summary().pnl_usd (bewusst keine
    zeitgewichtete Rendite, siehe dortiger Kommentar): 385 $ - 250 $ Start -
    100 $ Fluss = 35 $ echter Gewinn auf 250 $ Basis -> Index 114."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, net_flows_usd=0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(350, 280, 200, 0, 0, net_flows_usd=100, ts="2026-01-02T12:00:00")
    # +35 $ echter Gewinn zusaetzlich zur bereits erfassten Einzahlung.
    db.add_bot_equity_point(385, 308, 200, 0, 0, net_flows_usd=100, ts="2026-01-03T12:00:00")

    result = bot.performance_index_df()

    assert result["Bot netto"].iloc[-1] == pytest.approx(114.0)


def test_performance_index_collapses_identical_timestamps_for_chart(tmp_db):
    db.add_bot_equity_point(250, 200, 200, 0, 0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(255, 205, 200, 0, 0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(260, 210, 200, 0, 0, ts="2026-01-01T13:00:00")

    result = bot.performance_index_df()

    assert result is not None
    assert result.index.is_unique
    assert len(result) == 2
    assert result["Bot netto"].iloc[0] == pytest.approx(100.0)


def test_chart_start_rebases_all_series_at_the_cutoff(tmp_db):
    """Ab dem Chart-Beginn starten alle Reihen neu: Index 100, € ab dem
    Kontostand dort - frühere Punkte und die Einzahlung davor zählen nicht mit."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, net_flows_usd=0, ts="2026-01-01T12:00:00+00:00")
    db.add_bot_equity_point(350, 280, 200, 0, 0, net_flows_usd=100, ts="2026-01-02T12:00:00+00:00")
    db.add_bot_equity_point(385, 308, 200, 0, 0, net_flows_usd=100, ts="2026-01-03T12:00:00+00:00")
    db.set_meta(bot.CHART_START_META_KEY, "2026-01-02T10:00:00+00:00")
    since = bot.chart_start()

    index = bot.performance_index_df(since=since)
    eur = bot.performance_eur_df(since=since)

    assert len(index) == 2
    assert index["Bot netto"].tolist() == pytest.approx([100.0, 110.0])
    assert eur["Bot netto"].tolist() == pytest.approx([280.0, 308.0])
    assert eur["Kontostand"].tolist() == pytest.approx([280.0, 308.0])
    assert eur["EUR halten"].iloc[-1] == pytest.approx(280.0)


def test_performance_eur_scales_index_to_start_value_and_adds_real_balance(tmp_db):
    """€-Ansicht: bereinigte Reihen = Index × Startwert, Kontostand = rohe
    Equity inkl. Einzahlung (Szenario wie im Deposit-Test oben: +35 $ Gewinn,
    +100 $ Einzahlung, Start 200 €)."""
    db.add_bot_equity_point(250, 200, 200, 0, 0, net_flows_usd=0, ts="2026-01-01T12:00:00")
    db.add_bot_equity_point(350, 280, 200, 0, 0, net_flows_usd=100, ts="2026-01-02T12:00:00")
    db.add_bot_equity_point(385, 308, 200, 0, 0, net_flows_usd=100, ts="2026-01-03T12:00:00")
    btc = pd.Series([100.0, 120.0], index=pd.to_datetime(["2026-01-01", "2026-01-03"]))

    result = bot.performance_eur_df(btc)

    assert result["Bot netto"].iloc[-1] == pytest.approx(228.0)
    assert result["EUR halten"].iloc[-1] == pytest.approx(200.0)
    assert result["BTC Buy & Hold"].iloc[-1] == pytest.approx(240.0)
    assert result["Kontostand"].tolist() == pytest.approx([200.0, 280.0, 308.0])


def test_performance_eur_uses_one_equity_snapshot(monkeypatch):
    """Ein neuer Runner-Punkt während des Renderns darf die Reihen nicht verschieben."""
    rows = [
        {"ts": "2026-01-01T12:00:00+00:00", "equity_usd": 250,
         "equity_eur": 200, "net_flows_usd": 0},
        {"ts": "2026-01-02T12:00:00+00:00", "equity_usd": 260,
         "equity_eur": 208, "net_flows_usd": 0},
    ]
    calls = 0

    def changing_history():
        nonlocal calls
        calls += 1
        if calls == 1:
            return rows
        return rows + [{"ts": "2026-01-03T12:00:00+00:00", "equity_usd": 270,
                        "equity_eur": 216, "net_flows_usd": 0}]

    monkeypatch.setattr(db, "list_bot_equity", changing_history)
    result = bot.performance_eur_df()

    assert calls == 1
    assert len(result) == 2
    assert result["Bot netto"].tolist() == pytest.approx([200.0, 208.0])
    assert result["Kontostand"].tolist() == pytest.approx([200.0, 208.0])


# --- Kapitalfluss-Bereinigung (Ein-/Auszahlungen sind kein Handelsergebnis) ---

def test_deposit_does_not_inflate_gross_profit(tmp_db):
    """Eine Einzahlung darf nicht wie Handelsgewinn aussehen.

    gross_profit_usd ist NICHT nur Anzeige: es speist
    costs_over_double_gross_profit, eines der beiden 90-Tage-Abbruch-
    kriterien. Unbereinigt entschaerfte eine Einzahlung damit genau die
    Regel, die eine unrentable Kostenstruktur aufdecken soll."""
    _initialize(tmp_db, start=250.0)
    # 128,31 $ eingezahlt, Handel selbst hat 10 $ verloren, 5 $ Kosten.
    db.add_bot_equity_point(368.31, 340, 300, fees_cum_usd=5, funding_cum_usd=0,
                            net_flows_usd=128.31, ts="2026-04-01T12:00:00")

    summary = bot.performance_summary(now=datetime(2026, 4, 1))

    assert summary["pnl_usd"] == pytest.approx(-10.0)
    assert summary["gross_profit_usd"] == pytest.approx(-5.0)   # -10 + 5 Kosten
    assert summary["net_pnl_usd"] == pytest.approx(118.31)      # bleibt Kontostand-Delta
    # Kosten (5) > 2 x max(gross, 0) = 0 -> Kriterium greift trotz Einzahlung
    assert summary["costs_over_double_gross_profit"] is True


def test_deposit_does_not_inflate_return_pct(tmp_db):
    _initialize(tmp_db, start=250.0)
    db.add_bot_equity_point(378.31, 350, 300, fees_cum_usd=0, funding_cum_usd=0,
                            net_flows_usd=128.31, ts="2026-04-01T12:00:00")

    summary = bot.performance_summary(now=datetime(2026, 4, 1))

    # Weder Gewinn noch Verlust gehandelt -> bereinigte Rendite ist 0 %.
    assert summary["pnl_usd"] == pytest.approx(0.0)
    assert summary["return_pct"] == pytest.approx(0.0)
    # Die unbereinigte Zahl bleibt erhalten, aber sie ist eben nicht 0.
    assert summary["net_return_pct"] > 50


def test_withdrawal_does_not_fake_a_trading_loss(tmp_db):
    _initialize(tmp_db, start=250.0)
    db.add_bot_equity_point(150.0, 140, 150, fees_cum_usd=0, funding_cum_usd=0,
                            net_flows_usd=-100.0, ts="2026-04-01T12:00:00")

    summary = bot.performance_summary(now=datetime(2026, 4, 1))

    assert summary["pnl_usd"] == pytest.approx(0.0)
    assert summary["return_pct"] == pytest.approx(0.0)
    # Equity-Boden rechnet gegen den bereinigten Start (150), nicht gegen 250.
    assert summary["equity_below_80_pct"] is False
