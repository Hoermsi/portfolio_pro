def test_cash_empty(tmp_db):
    assert tmp_db.latest_cash_balance() is None
    assert tmp_db.list_cash_entries() == []


def test_cash_entries_and_latest(tmp_db):
    tmp_db.add_cash_entry(1500.0)
    tmp_db.add_cash_entry(1200.50)
    tmp_db.add_cash_entry(2000.0)

    entries = tmp_db.list_cash_entries()
    assert len(entries) == 3
    # Reihenfolge = Einfüge-Reihenfolge, jeder Eintrag eigener Datenpunkt
    assert [e["balance_eur"] for e in entries] == [1500.0, 1200.50, 2000.0]
    assert all(e["created_at"] for e in entries)
    assert tmp_db.latest_cash_balance() == 2000.0


def test_cash_multiple_entries_same_day_kept(tmp_db):
    """Mehrere Aktualisierungen am selben Tag bleiben getrennte Datenpunkte."""
    for value in (100.0, 200.0, 300.0):
        tmp_db.add_cash_entry(value)
    assert len(tmp_db.list_cash_entries()) == 3


def test_delete_last_cash_entry(tmp_db):
    tmp_db.add_cash_entry(1000.0)
    tmp_db.add_cash_entry(9999.0)   # Fehleingabe
    tmp_db.delete_last_cash_entry()
    assert tmp_db.latest_cash_balance() == 1000.0
    # auf leerer Tabelle kein Fehler
    tmp_db.delete_last_cash_entry()
    tmp_db.delete_last_cash_entry()
    assert tmp_db.latest_cash_balance() is None


def test_portfolio_summary_includes_cash(tmp_db, monkeypatch):
    """Cash fließt ins Gesamtvermögen und die Allokation von portfolio_summary ein."""
    from core import portfolio

    # Positionen ausklammern (keine Netz-Calls), reine Cash-Logik prüfen
    monkeypatch.setattr(portfolio, "valued_positions", lambda asset_type: [])
    tmp_db.add_cash_entry(2500.0)

    summary = portfolio.portfolio_summary()
    assert summary["cash_eur"] == 2500.0
    assert summary["gesamt_eur"] == 2500.0  # 0 Aktien + 0 Krypto + Cash
    assert summary["allokation"]["cash_pct"] == 100.0


def test_portfolio_summary_cash_defaults_to_zero(tmp_db, monkeypatch):
    """Ohne Cash-Eintrag ist cash_eur 0.0 und bricht die Summenbildung nicht."""
    from core import portfolio

    monkeypatch.setattr(portfolio, "valued_positions", lambda asset_type: [])
    summary = portfolio.portfolio_summary()
    assert summary["cash_eur"] == 0.0
    assert summary["gesamt_eur"] == 0.0
    assert "allokation" not in summary  # gesamt_eur == 0 -> keine Allokation


def test_cashflows_crud(tmp_db):
    first = tmp_db.add_cashflow(500.0, "2026-07-01", "Sparrate")
    tmp_db.add_cashflow(-120.0, "2026-07-02", "Entnahme")
    flows = tmp_db.list_cashflows()
    assert [f["amount_eur"] for f in flows] == [-120.0, 500.0]
    assert flows[1]["note"] == "Sparrate"
    tmp_db.delete_cashflow(first)
    assert len(tmp_db.list_cashflows()) == 1
