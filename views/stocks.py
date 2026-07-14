"""Aktien-Portfolio: Positionen, Flatex-Import."""
import streamlit as st
import pandas as pd

from core import db
from data import flatex
from views import positions
from ui import components


def render():
    components.page_header("Depots", "Aktien", "Positionen, Konten und Import an einem Ort.")

    tab_pos, tab_import, tab_watch = st.tabs(["Positionen", "Flatex-Import", "Watchlist"])

    with tab_pos:
        positions.render_positions_table("stock")
        st.divider()
        positions.render_add_form(
            "stock",
            symbol_help="yfinance-Ticker, z.B. NVDA, AAPL, SAP.DE, oder ISIN",
        )
        st.caption("💡 Detail-Analyse eines Werts: Seite **Einzelwert-Analyse**.")

    with tab_watch:
        positions.render_watchlist("stock")

    with tab_import:
        st.info("Ein erneuter Upload ersetzt ausschließlich den Bestand des gewählten Kontos. Prüfe die Auswahl vor dem Synchronisieren.")
        st.markdown("Flatex **'Depotbestand'**-CSV je Konto getrennt hochladen.")
        col_aktien, col_etf = st.columns(2)
        with col_aktien:
            _render_account_upload("📈 Aktien-Konto", "Flatex-Aktien", "aktien")
        with col_etf:
            _render_account_upload("📊 ETF-Konto", "Flatex-ETF", "etf")

        # Alte Sammelkategorie aus der V1-Migration -> würde sonst doppelt zählen
        if "Flatex-Import" in db.list_categories("stock"):
            st.divider()
            st.info("Es existiert noch die alte Sammelkategorie **Flatex-Import** "
                    "(Aktien und ETFs gemischt). Nach dem getrennten Upload kannst du "
                    "sie entfernen, damit Positionen nicht doppelt zählen.")
            if st.button("🗑️ Alte Kategorie 'Flatex-Import' entfernen"):
                n = db.delete_positions_by_category("stock", "Flatex-Import")
                st.success(f"{n} Positionen aus 'Flatex-Import' entfernt.")
                st.rerun()


def _render_account_upload(titel: str, category: str, key: str):
    st.markdown(f"##### {titel}")
    file = st.file_uploader("Flatex-CSV", type=["csv"], key=f"upload_{key}")
    preview = None
    if file:
        ok, rows, errors = flatex.preview_csv(file)
        if ok:
            preview = rows
            st.caption(f"Vorschau: {len(rows)} gültige Positionen werden in „{category}“ übernommen.")
            with st.expander("Importvorschau öffnen"):
                st.dataframe(pd.DataFrame(rows).rename(columns={"symbol": "Symbol", "quantity": "Menge",
                                                                "buy_price": "Einstand (€)", "name": "Name"}),
                             use_container_width=True, hide_index=True)
        else:
            st.error("; ".join(errors))
    if file and preview is not None and st.button("Konto synchronisieren", key=f"sync_{key}",
                                                   use_container_width=True):
        ok, count, unresolved = flatex.import_csv(file, category=category, replace=True)
        if ok:
            st.success(f"{count} Positionen in *{category}* importiert (Bestand ersetzt).")
            if unresolved:
                st.warning(
                    "Bei yfinance nicht auflösbar (als ISIN gespeichert, evtl. "
                    "ohne Kurs):\n\n- " + "\n- ".join(unresolved)
                )
        else:
            st.error("Fehler: " + "; ".join(unresolved))
