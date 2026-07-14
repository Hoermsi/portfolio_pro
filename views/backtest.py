"""Backtest: hypothetische Rendite der heutigen Bestände ab einem Kaufdatum."""
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from analysis import backtest
from ui import components

_SCOPES = {"Gesamt": "total", "Aktien": "stock", "Krypto": "crypto"}
_GREEN = "color: #23c55e; font-weight: 600"
_RED = "color: #ff4b4b; font-weight: 600"


def _gv_style(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    return _GREEN if v > 0 else _RED


def render():
    components.page_header("Analysen", "Backtest",
                           "Was wäre deine Rendite, wenn du deine heutigen Bestände am Tag X gekauft hättest?")

    c1, c2 = st.columns([1, 1])
    scope_label = c1.radio("Umfang", list(_SCOPES.keys()), horizontal=True, key="bt_scope")
    scope = _SCOPES[scope_label]
    default_start = date.today() - timedelta(days=365)
    start_date = c2.date_input("Kaufdatum", value=default_start,
                               min_value=date.today() - timedelta(days=3650),
                               max_value=date.today() - timedelta(days=1),
                               key="bt_date")

    st.caption("Annahme: deine **heutigen** Stückzahlen zum Kurs am Kaufdatum gekauft. "
               "Große Coins reichen mehrere Jahre zurück; sehr neue oder kleine Coins "
               "evtl. kürzer – ohne Kurs am Kaufdatum werden sie übersprungen.")

    if not st.button("▶️ Backtest ausführen", type="primary"):
        return

    with st.spinner("Lade historische Kurse …"):
        res = backtest.run_backtest(scope, start_date)

    if not res["rows"]:
        st.warning("Keine bewertbaren Positionen für diesen Zeitraum/Umfang gefunden. "
                   "Bei Krypto ggf. ein späteres Kaufdatum wählen.")
        if res["skipped"]:
            st.caption("Ohne Kurs am Kaufdatum (übersprungen): " + ", ".join(res["skipped"]))
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Investiert (Tag X)", f"{res['total_invest']:,.0f} €")
    m2.metric("Wert heute", f"{res['total_value']:,.0f} €")
    m3.metric("Gewinn/Verlust", f"{res['gain_eur']:+,.0f} €")
    m4.metric("Rendite", f"{res['total_return_pct']:+.1f} %" if res["total_return_pct"] is not None else "—")

    rows = sorted(res["rows"], key=lambda r: r["Rendite (%)"], reverse=True)
    df = pd.DataFrame(rows)
    styled = (df.style
              .map(_gv_style, subset=["Rendite (%)"])
              .format({"Kauf-Kurs (€)": "{:,.4f}", "Kurs heute (€)": "{:,.4f}",
                       "Investiert (€)": "{:,.2f}", "Wert heute (€)": "{:,.2f}",
                       "Rendite (%)": "{:+.1f}"}, na_rep="—"))
    st.dataframe(styled, use_container_width=True, hide_index=True)

    curve = res.get("curve")
    if curve is not None and len(curve) >= 2:
        st.markdown("#### Wertverlauf seit Kaufdatum")
        fig = px.line(x=curve.index, y=curve.values,
                      labels={"x": "Datum", "y": "Depotwert (€)"})
        fig.update_traces(line_color="#23c55e")
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                          xaxis_title=None, yaxis_title="Wert (€)")
        st.plotly_chart(fig, use_container_width=True, key=f"bt_curve_{scope}")

    if res["skipped"]:
        st.caption("Ohne Kurs am Kaufdatum (übersprungen): " + ", ".join(res["skipped"]))
