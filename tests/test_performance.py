"""Tests für analysis/performance.py (Snapshot-Aggregation, ohne Netzwerkzugriff)."""
from analysis import performance


def test_history_df_forward_fills_missing_class_snapshot(tmp_db):
    """Fehlt an einem Tag der Snapshot einer Anlageklasse (z.B. Kursausfall),
    darf die 'Gesamt'-Reihe nicht künstlich einbrechen - der letzte bekannte
    Wert dieser Klasse muss fortgeschrieben werden statt als 0 zu zählen."""
    db = tmp_db
    db.save_snapshot("stock", 1000.0, "2026-01-01")
    db.save_snapshot("crypto", 500.0, "2026-01-01")
    # Tag 2: Aktien-Snapshot fehlt (Kursausfall), Krypto steigt.
    db.save_snapshot("crypto", 600.0, "2026-01-02")

    hist = performance.history_df()
    assert hist is not None
    day2 = hist.loc["2026-01-02"]
    assert day2["Aktien"] == 1000.0   # fortgeschrieben, nicht 0
    assert day2["Krypto"] == 600.0
    assert day2["Gesamt"] == 1600.0


def test_history_df_zero_before_first_snapshot_of_a_class(tmp_db):
    """Vor dem allerersten Snapshot einer Klasse gibt es keinen 'letzten
    bekannten Wert' - dort ist 0 weiterhin korrekt (kein Rückwärts-Fill)."""
    db = tmp_db
    db.save_snapshot("crypto", 500.0, "2026-01-01")
    db.save_snapshot("stock", 1000.0, "2026-01-02")

    hist = performance.history_df()
    day1 = hist.loc["2026-01-01"]
    assert day1["Aktien"] == 0.0
    assert day1["Gesamt"] == 500.0


def test_history_df_none_when_no_snapshots(tmp_db):
    assert performance.history_df() is None


def test_month_return_pct_excludes_deposit(tmp_db):
    """Eine Einzahlung in der Mitte des Monats darf nicht als Performance
    erscheinen - month_return_pct() muss deutlich unter der naiven
    (Endwert-Startwert)/Startwert-Rechnung liegen."""
    db = tmp_db
    db.save_snapshot("stock", 1000.0, "2026-02-01")
    db.save_snapshot("stock", 1000.0, "2026-02-14")
    db.add_cashflow(500.0, flow_date="2026-02-15")   # Einzahlung
    db.save_snapshot("stock", 1500.0, "2026-02-15")  # Sprung nur wegen der Einzahlung
    db.save_snapshot("stock", 1500.0, "2026-02-28")

    from datetime import date
    naive_pct = (1500.0 - 1000.0) / 1000.0 * 100
    pct = performance.month_return_pct(date(2026, 2, 1))
    assert pct is not None
    assert abs(pct) < abs(naive_pct)


def test_month_return_pct_none_for_missing_history(tmp_db):
    from datetime import date
    assert performance.month_return_pct(date(2026, 2, 1)) is None
