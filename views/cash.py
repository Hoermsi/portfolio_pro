"""Cash-Seite: Bankkonto-Stand pflegen mit Verlaufsdiagramm und Schnell-Buttons."""
import pandas as pd
import plotly.express as px
import streamlit as st

from core import db
from core.profile import emergency_fund_eur, emergency_fund_progress_pct
from ui import components

_QUICK_STEPS = (-1000, -500, -100, -10, 10, 100, 500, 1000)


def _adjust_amount(delta: float):
    """on_click-Callback der Schnell-Buttons: passt den Feldwert an (min. 0)."""
    current = float(st.session_state.get("cash_amount", 0.0) or 0.0)
    st.session_state["cash_amount"] = max(0.0, current + delta)


def render():
    components.page_header("Depots", "Liquidität", "Bank-Cash, externe Ein- und Auszahlungen sowie Verlauf.")
    tab_balance, tab_flows = st.tabs(["Kontostand", "Ein- & Auszahlungen"])
    with tab_balance:
        _render_balance()
    with tab_flows:
        _render_cashflows()


def _render_balance():
    st.caption("Jede Aktualisierung erzeugt einen Datenpunkt im Vermögensverlauf.")

    entries = db.list_cash_entries()
    latest = entries[-1]["balance_eur"] if entries else None
    previous = entries[-2]["balance_eur"] if len(entries) >= 2 else None

    # --- Aktueller Stand ---
    fund = emergency_fund_eur()
    cols = st.columns([1, 1, 2]) if fund > 0 else st.columns([1, 2])
    col_metric, col_info = cols[0], cols[-1]
    with col_metric:
        if latest is not None:
            delta = f"{latest - previous:+,.2f} €" if previous is not None else None
            st.metric("Aktueller Kontostand", f"{latest:,.2f} €", delta)
        else:
            st.metric("Aktueller Kontostand", "—")
    if fund > 0:
        with cols[1]:
            pct = emergency_fund_progress_pct(latest)
            st.metric("Notgroschen gefüllt", f"{pct:.0f} %" if pct is not None else "—",
                     f"Ziel: {fund:,.0f} €")
    with col_info:
        if entries:
            st.caption(f"Zuletzt aktualisiert: "
                       f"{entries[-1]['created_at'][:16].replace('T', ' ')} · "
                       f"{len(entries)} Einträge im Verlauf")

    st.divider()

    # --- Eingabe + Schnell-Buttons ---
    st.markdown("#### 💾 Kontostand aktualisieren")
    if "cash_amount" not in st.session_state:
        st.session_state["cash_amount"] = float(latest) if latest is not None else 0.0

    st.number_input("Neuer Kontostand (€)", min_value=0.0, step=50.0,
                    format="%.2f", key="cash_amount")

    btn_cols = st.columns(len(_QUICK_STEPS))
    for col, step in zip(btn_cols, _QUICK_STEPS):
        label = f"{step:+,} €".replace(",", ".")
        col.button(label, key=f"cash_step_{step}", width="stretch",
                   on_click=_adjust_amount, args=(float(step),))

    if st.button("💾 Stand aktualisieren", type="primary"):
        db.add_cash_entry(float(st.session_state["cash_amount"]))
        st.success(f"Kontostand {st.session_state['cash_amount']:,.2f} € gespeichert.")
        st.rerun()

    st.divider()

    # --- Verlauf ---
    st.markdown("#### 📈 Verlauf")
    if len(entries) < 1:
        st.caption("Noch keine Einträge - speichere oben deinen ersten Kontostand.")
        return

    df = pd.DataFrame(entries)
    df["Zeitpunkt"] = pd.to_datetime(df["created_at"])
    fig = px.line(df, x="Zeitpunkt", y="balance_eur", markers=True,
                  labels={"balance_eur": "Kontostand (€)"})
    fig.update_traces(line_color="#23c55e")
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch", key="cash_history_chart")

    with st.expander(f"🗂️ Alle Einträge ({len(entries)})"):
        table = pd.DataFrame({
            "Zeitpunkt": [e["created_at"][:16].replace("T", " ") for e in reversed(entries)],
            "Kontostand (€)": [round(e["balance_eur"], 2) for e in reversed(entries)],
        })
        st.dataframe(table, width="stretch", hide_index=True)
        if st.button("↩️ Letzten Eintrag löschen (Fehleingabe)"):
            db.delete_last_cash_entry()
            st.rerun()


def _render_cashflows():
    st.markdown("### Kapitalbewegungen")
    st.caption("Erfasse nur Geld, das von außerhalb in dein Gesamtvermögen fließt oder es verlässt. "
               "So wird der Renditeindex im Dashboard um diese Bewegungen bereinigt.")
    with st.form("cashflow_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 1, 2])
        kind = c1.radio("Art", ["Einzahlung", "Auszahlung"], horizontal=True)
        amount = c2.number_input("Betrag (€)", min_value=0.01, step=100.0, format="%.2f")
        note = c3.text_input("Notiz", placeholder="z. B. monatliche Sparrate")
        flow_date = st.date_input("Datum")
        save = st.form_submit_button("Kapitalbewegung erfassen", type="primary")
    if save:
        signed_amount = amount if kind == "Einzahlung" else -amount
        db.add_cashflow(signed_amount, flow_date.isoformat(), note)
        st.success("Kapitalbewegung erfasst.")
        st.rerun()

    flows = db.list_cashflows(100)
    if not flows:
        st.info("Noch keine externen Kapitalbewegungen erfasst.")
        return
    table = pd.DataFrame({
        "Datum": [f["flow_date"] for f in flows],
        "Art": ["Einzahlung" if f["amount_eur"] > 0 else "Auszahlung" for f in flows],
        "Betrag (€)": [round(f["amount_eur"], 2) for f in flows],
        "Notiz": [f["note"] for f in flows],
    })
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"Betrag (€)": st.column_config.NumberColumn(format="%+.2f €")})
    with st.expander("Buchung löschen"):
        options = {f"{f['flow_date']} · {f['amount_eur']:+,.2f} € · {f['note'] or 'ohne Notiz'}": f["id"]
                   for f in flows}
        chosen = st.selectbox("Kapitalbewegung", list(options))
        if st.button("Ausgewählte Buchung löschen"):
            db.delete_cashflow(options[chosen])
            st.rerun()
