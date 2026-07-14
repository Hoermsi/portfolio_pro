"""Exports: Monatsreport als PDF herunterladen."""
from datetime import date

import streamlit as st

from core import report
from ui import components

_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]


def render():
    components.page_header("Verwaltung", "Exports", "Berichte und Auswertungen zum Herunterladen.")

    st.markdown("### 📄 Monatsreport (PDF)")
    st.caption("Vermögensübersicht, Allokation vs. Ziel, Monats-Performance und die "
               "größten Positionen als druckbares PDF.")

    today = date.today()
    c1, c2 = st.columns(2)
    year = c1.number_input("Jahr", min_value=2000, max_value=today.year,
                           value=today.year, step=1)
    month_name = c2.selectbox("Monat", _MONTHS_DE, index=today.month - 1)
    month = date(int(year), _MONTHS_DE.index(month_name) + 1, 1)

    if st.button("📄 Monatsreport erzeugen", type="primary"):
        with st.spinner("Erzeuge PDF …"):
            try:
                pdf = report.build_monthly_pdf(month)
            except Exception as e:  # noqa: BLE001 - dem Nutzer den Grund zeigen
                st.error(f"Report konnte nicht erstellt werden: {e}")
                return
        st.session_state["export_pdf"] = pdf
        st.session_state["export_pdf_name"] = f"PortfolioPro-Report-{month:%Y-%m}.pdf"
        st.success("Report erstellt – unten herunterladen.")

    if st.session_state.get("export_pdf"):
        st.download_button("⬇️ PDF herunterladen", data=st.session_state["export_pdf"],
                           file_name=st.session_state.get("export_pdf_name", "report.pdf"),
                           mime="application/pdf")
