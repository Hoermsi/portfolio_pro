"""Tests für den Cash-/Allokations-Prompt-Block (dossier.cash_allocation_prompt)."""
import json

from agents import dossier


def _summary(stock, crypto, cash):
    total = stock + crypto + cash
    d = {"aktien_summe_eur": stock, "krypto_summe_eur": crypto, "cash_eur": cash,
         "gesamt_eur": total, "aktien": [], "krypto": []}
    if total > 0:
        d["allokation"] = {"aktien_pct": round(stock / total * 100, 1),
                           "krypto_pct": round(crypto / total * 100, 1),
                           "cash_pct": round(cash / total * 100, 1)}
    return d


def test_cash_prompt_reports_free_cash(tmp_db, monkeypatch):
    tmp_db.set_meta("target_allocation", json.dumps({"stock": 60, "crypto": 20, "cash": 20}))
    # Gesamt 10.000 EUR, davon 5.000 Cash; Ziel-Cash 20% = 2.000 -> frei 3.000
    monkeypatch.setattr(dossier, "portfolio_summary", lambda: _summary(4000, 1000, 5000))
    text = dossier.cash_allocation_prompt()
    assert "BANKGUTHABEN" in text
    assert "5,000.00 EUR" in text          # Cash
    assert "3,000.00 EUR" in text          # frei investierbar (5000 - 2000)
    assert "ist 40.0% / ziel 60.0%" in text   # Aktien untergewichtet
    assert "KONKRETE" in text              # Handlungsaufforderung bei freiem Cash


def test_cash_prompt_no_free_cash(tmp_db, monkeypatch):
    tmp_db.set_meta("target_allocation", json.dumps({"stock": 60, "crypto": 20, "cash": 20}))
    # Cash 1.000 unter Ziel-Reserve (20% von 10.000 = 2.000) -> nichts frei
    monkeypatch.setattr(dossier, "portfolio_summary", lambda: _summary(6000, 3000, 1000))
    text = dossier.cash_allocation_prompt()
    assert "FREI INVESTIERBARES CASH: 0.00 EUR" in text
    assert "keine cash-finanzierten Zukaeufe" in text


def test_cash_prompt_empty_portfolio(tmp_db, monkeypatch):
    monkeypatch.setattr(dossier, "portfolio_summary", lambda: _summary(0, 0, 0))
    text = dossier.cash_allocation_prompt()
    assert "BANKGUTHABEN" in text          # kein Crash bei leerem Depot


def test_cash_prompt_respects_emergency_fund_over_target(tmp_db, monkeypatch):
    """Der Notgroschen darf nicht angetastet werden, auch wenn er über der
    prozentualen Ziel-Cash-Quote liegt (sonst würde die KI ihn zum Investieren
    vorschlagen - genau das, was er verhindern soll)."""
    from core.profile import save_emergency_fund_eur
    tmp_db.set_meta("target_allocation", json.dumps({"stock": 60, "crypto": 20, "cash": 20}))
    save_emergency_fund_eur(8000.0)
    # Gesamt 10.000 EUR, Ziel-Cash 20% = 2.000, aber Notgroschen 8.000 > Ziel
    monkeypatch.setattr(dossier, "portfolio_summary", lambda: _summary(4000, 1000, 5000))
    text = dossier.cash_allocation_prompt()
    assert "NOTGROSCHEN" in text
    assert "FREI INVESTIERBARES CASH: 0.00 EUR" in text  # 5000 Cash < 8000 Reserve


def test_cash_prompt_emergency_fund_below_target_no_mention(tmp_db, monkeypatch):
    """Liegt der Notgroschen unter der Ziel-Cash-Quote, bleibt die Ziel-Quote
    maßgeblich - kein irrelevanter Notgroschen-Hinweis im Prompt."""
    from core.profile import save_emergency_fund_eur
    tmp_db.set_meta("target_allocation", json.dumps({"stock": 60, "crypto": 20, "cash": 20}))
    save_emergency_fund_eur(500.0)  # weit unter Ziel-Cash (2000)
    monkeypatch.setattr(dossier, "portfolio_summary", lambda: _summary(4000, 1000, 5000))
    text = dossier.cash_allocation_prompt()
    assert "NOTGROSCHEN" not in text
    assert "FREI INVESTIERBARES CASH: 3,000.00 EUR" in text  # unverändert wie zuvor
