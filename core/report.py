"""Monatsreport als PDF (reportlab).

Fasst den aktuellen Vermögensstand, die Allokation gegenüber der Zielallokation,
die Monats-Performance (aus der Snapshot-Historie) und die größten Positionen in
einem druckbaren PDF zusammen. Gibt reine Bytes zurück (UI hängt sie an einen
st.download_button). Datenquellen sind die vorhandenen Module - kein eigener
Kursabruf-Code.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from io import BytesIO

import pandas as pd

from analysis import performance
from core import db
from core.portfolio import total_value, valued_positions

_MONTHS_DE = ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]


def _month_performance(month: date) -> tuple[float | None, float | None]:
    """Gesamtwert am Monatsanfang und -ende aus der Snapshot-Historie (asof)."""
    hist = performance.history_df()
    if hist is None or "Gesamt" not in hist:
        return None, None
    series = hist["Gesamt"].sort_index()
    first = month.replace(day=1)
    last_day = calendar.monthrange(month.year, month.month)[1]
    last = month.replace(day=last_day)
    start = series.asof(pd.Timestamp(first))
    end = series.asof(pd.Timestamp(last))
    start = float(start) if start is not None and not pd.isna(start) else None
    end = float(end) if end is not None and not pd.isna(end) else None
    return start, end


def build_monthly_pdf(month: date) -> bytes:
    """Monatsreport für den Monat von `month` als PDF-Bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    stock_vals = valued_positions("stock")
    crypto_vals = valued_positions("crypto")
    stock_total = total_value(stock_vals)
    crypto_total = total_value(crypto_vals)
    cash = db.latest_cash_balance() or 0.0
    total = stock_total + crypto_total + cash

    from views.settings import target_allocation
    targets = target_allocation()

    styles = getSampleStyleSheet()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"Portfolio Pro Monatsreport {month:%Y-%m}")
    flow = []

    flow.append(Paragraph("Portfolio Pro – Monatsreport", styles["Title"]))
    flow.append(Paragraph(f"{_MONTHS_DE[month.month]} {month.year}", styles["Heading2"]))
    flow.append(Paragraph(f"Erstellt am {datetime.now():%d.%m.%Y %H:%M}", styles["Normal"]))
    flow.append(Spacer(1, 8 * mm))

    def _pct(v: float) -> str:
        return f"{v / total * 100:.1f} %" if total else "—"

    # --- Vermögensübersicht ---
    flow.append(Paragraph("Vermögensübersicht", styles["Heading3"]))
    wealth_data = [
        ["Anlageklasse", "Wert (€)", "Anteil"],
        ["Aktien", f"{stock_total:,.2f}", _pct(stock_total)],
        ["Krypto", f"{crypto_total:,.2f}", _pct(crypto_total)],
        ["Cash", f"{cash:,.2f}", _pct(cash)],
        ["Gesamt", f"{total:,.2f}", "100.0 %" if total else "—"],
    ]
    flow.append(_styled_table(wealth_data, colors, Table, TableStyle, mm, highlight_last=True))
    flow.append(Spacer(1, 6 * mm))

    # --- Allokation vs. Ziel ---
    flow.append(Paragraph("Allokation vs. Zielallokation", styles["Heading3"]))
    alloc_rows = [["Anlageklasse", "Ist", "Ziel", "Abweichung"]]
    for name, key, val in (("Aktien", "stock", stock_total), ("Krypto", "crypto", crypto_total),
                           ("Cash", "cash", cash)):
        ist = val / total * 100 if total else 0.0
        ziel = targets[key]
        alloc_rows.append([name, f"{ist:.1f} %", f"{ziel:.1f} %", f"{ist - ziel:+.1f} %-Pkt."])
    flow.append(_styled_table(alloc_rows, colors, Table, TableStyle, mm))
    flow.append(Spacer(1, 6 * mm))

    # --- Monats-Performance ---
    flow.append(Paragraph("Monats-Performance", styles["Heading3"]))
    start_val, end_val = _month_performance(month)
    if start_val and end_val and start_val > 0:
        change = end_val - start_val
        pct = change / start_val * 100
        perf_text = (f"Gesamtwert am Monatsanfang: {start_val:,.2f} € · "
                     f"am Monatsende: {end_val:,.2f} € · "
                     f"Veränderung: {change:+,.2f} € ({pct:+.1f} %).")
    else:
        perf_text = ("Für diesen Monat liegen noch nicht genug Verlaufsdaten vor "
                     "(Snapshots entstehen mit jedem Öffnen des Dashboards).")
    flow.append(Paragraph(perf_text, styles["Normal"]))
    flow.append(Spacer(1, 6 * mm))

    # --- Größte Positionen ---
    flow.append(Paragraph("Größte Positionen", styles["Heading3"]))
    valued = [v for v in (stock_vals + crypto_vals) if v.value_eur]
    top = sorted(valued, key=lambda v: v.value_eur or 0, reverse=True)[:5]
    if top:
        top_rows = [["Position", "Symbol", "Wert (€)", "Anteil"]]
        for v in top:
            top_rows.append([
                (v.position.name or v.position.symbol)[:28],
                v.position.symbol,
                f"{v.value_eur:,.2f}",
                _pct(v.value_eur or 0),
            ])
        flow.append(_styled_table(top_rows, colors, Table, TableStyle, mm))
    else:
        flow.append(Paragraph("Keine bewertbaren Positionen.", styles["Normal"]))

    flow.append(Spacer(1, 10 * mm))
    flow.append(Paragraph("Keine Anlageberatung. Kurse und Werte sind Näherungen zum "
                          "Erstellungszeitpunkt.", styles["Italic"]))

    doc.build(flow)
    return buf.getvalue()


def _styled_table(data, colors, Table, TableStyle, mm, highlight_last: bool = False):
    """Einheitlich formatierte Tabelle (Kopfzeile dunkel, Zahlen rechtsbündig)."""
    table = Table(data, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b1120")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#26344d")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5fa")]),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if highlight_last:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e6ebf3")))
    table.setStyle(TableStyle(style))
    return table
