"""Lokales Buchungsjournal für manuelle Wertpapier- und Kryptotransaktionen."""
import pandas as pd
import streamlit as st

from core import db
from ui import components


def render():
    components.page_header("Depots", "Buchungen", "Manuelle Käufe und Verkäufe mit direkter Aktualisierung der Positionen.")
    tab_book, tab_history = st.tabs(["Kauf oder Verkauf buchen", "Buchungsjournal"])
    with tab_book:
        _render_booking_form()
    with tab_history:
        _render_history()


def _render_booking_form():
    st.caption("Ein Kauf bildet den durchschnittlichen Einstand inklusive Gebühren neu. "
               "Ein Verkauf reduziert nur die Menge; der Einstand pro Stück bleibt erhalten.")
    with st.form("trade_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        side_label = c1.radio("Art", ["Kauf", "Verkauf"], horizontal=True)
        asset_label = c2.radio("Anlageklasse", ["Aktie", "Krypto"], horizontal=True)
        trade_date = c3.date_input("Buchungsdatum")
        c4, c5, c6 = st.columns(3)
        symbol = c4.text_input("Symbol", placeholder="z. B. NVDA oder BTC").strip().upper()
        quantity = c5.number_input("Menge", min_value=0.00000001, format="%.8f")
        price = c6.number_input("Preis (€ / Stück)", min_value=0.0, format="%.4f")
        asset_type = "stock" if asset_label == "Aktie" else "crypto"
        categories = db.list_categories(asset_type) or ["Standard"]
        c7, c8, c9 = st.columns(3)
        category = c7.selectbox("Konto", categories)
        fees = c8.number_input("Gebühren (€)", min_value=0.0, format="%.2f")
        note = c9.text_input("Notiz (optional)")
        submit = st.form_submit_button("Buchung speichern", type="primary")
    if submit:
        if not symbol:
            st.error("Bitte ein Symbol eingeben.")
            return
        try:
            db.record_trade(symbol, asset_type, "buy" if side_label == "Kauf" else "sell",
                            quantity, price, category, fees, trade_date.isoformat(), note)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("Buchung gespeichert und Position aktualisiert.")


def _render_history():
    transactions = db.list_transactions()
    if not transactions:
        st.info("Noch keine manuellen Käufe oder Verkäufe erfasst.")
        return
    rows = []
    for tx in transactions:
        gross = tx["quantity"] * tx["price_eur"]
        rows.append({"Datum": tx["trade_date"], "Art": "Kauf" if tx["side"] == "buy" else "Verkauf",
                     "Symbol": tx["symbol"], "Anlageklasse": "Aktie" if tx["asset_type"] == "stock" else "Krypto",
                     "Konto": tx["category"], "Menge": tx["quantity"], "Preis (€)": tx["price_eur"],
                     "Gebühr (€)": tx["fees_eur"], "Volumen (€)": gross, "Notiz": tx["note"]})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={
                     "Preis (€)": st.column_config.NumberColumn(format="%.4f €"),
                     "Gebühr (€)": st.column_config.NumberColumn(format="%.2f €"),
                     "Volumen (€)": st.column_config.NumberColumn(format="%.2f €"),
                 })
    st.caption("Direkte Änderungen an einer Position oder ein Depotimport erscheinen nicht rückwirkend im Buchungsjournal.")
