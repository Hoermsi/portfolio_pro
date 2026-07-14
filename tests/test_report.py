"""Tests für den PDF-Monatsreport (offline, leere/gefüllte DB)."""
from datetime import date, timedelta

from core import report


def test_build_monthly_pdf_empty(tmp_db):
    pdf = report.build_monthly_pdf(date.today())
    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf[:4]) == b"%PDF"
    assert len(pdf) > 500


def test_build_monthly_pdf_with_snapshots(tmp_db):
    db = tmp_db
    month_start = date.today().replace(day=1)
    db.save_snapshot("stock", 1000.0, snap_date=month_start.isoformat())
    later = month_start + timedelta(days=20)
    db.save_snapshot("stock", 1100.0, snap_date=later.isoformat())

    pdf = report.build_monthly_pdf(month_start)
    assert bytes(pdf[:4]) == b"%PDF"
