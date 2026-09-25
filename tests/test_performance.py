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


def test_value_n_days_ago_returns_previous_row_for_one_day(tmp_db):
    db = tmp_db
    db.save_snapshot("stock", 1000.0, "2026-01-01")
    db.save_snapshot("stock", 1100.0, "2026-01-02")
    hist = performance.history_df()
    assert performance.value_n_days_ago(hist, "Aktien", 1) == 1000.0


def test_value_n_days_ago_uses_nearest_earlier_snapshot_for_longer_periods(tmp_db):
    """7 Tage vor dem letzten Datenpunkt (01-10) faellt auf 01-03 - dafuer
    gibt es keinen Snapshot, der naechstliegende FRUEHERE ist 01-01."""
    db = tmp_db
    db.save_snapshot("stock", 900.0, "2026-01-01")
    db.save_snapshot("stock", 950.0, "2026-01-05")
    db.save_snapshot("stock", 1000.0, "2026-01-10")
    hist = performance.history_df()
    assert performance.value_n_days_ago(hist, "Aktien", 7) == 900.0


def test_value_n_days_ago_none_when_history_too_short(tmp_db):
    db = tmp_db
    db.save_snapshot("stock", 1000.0, "2026-01-10")
    hist = performance.history_df()
    assert performance.value_n_days_ago(hist, "Aktien", 30) is None


def test_value_n_days_ago_none_when_period_not_yet_reached(tmp_db):
    """Aeltester Snapshot liegt selbst noch innerhalb der angefragten
    Periode - keine falsche Basis erfinden, lieber 'n/a'."""
    db = tmp_db
    db.save_snapshot("stock", 1000.0, "2026-01-08")
    db.save_snapshot("stock", 1050.0, "2026-01-10")
    hist = performance.history_df()
    assert performance.value_n_days_ago(hist, "Aktien", 30) is None


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


def test_sentiment_series_empty_without_rows(tmp_db):
    result = performance.sentiment_series("crypto:overall")
    assert result.empty


def test_sentiment_series_returns_sorted_values(tmp_db):
    db = tmp_db
    db.save_sentiment("crypto:overall", 40.0, "2026-01-02")
    db.save_sentiment("crypto:overall", 30.0, "2026-01-01")
    # anderer Indikator am selben Tag darf nicht mit hineinrutschen.
    db.save_sentiment("stock:overall", 99.0, "2026-01-01")

    result = performance.sentiment_series("crypto:overall")
    assert list(result.values) == [30.0, 40.0]
    assert result.index[0] < result.index[1]


def test_sentiment_series_days_filter(tmp_db):
    from datetime import date, timedelta
    db = tmp_db
    old_date = (date.today() - timedelta(days=100)).isoformat()
    recent_date = (date.today() - timedelta(days=1)).isoformat()
    db.save_sentiment("crypto:overall", 20.0, old_date)
    db.save_sentiment("crypto:overall", 50.0, recent_date)

    result = performance.sentiment_series("crypto:overall", days=10)
    assert list(result.values) == [50.0]
