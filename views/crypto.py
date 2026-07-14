"""Krypto-Portfolio: Kraken-Sync + manuelle Positionen."""
import streamlit as st

from core import config
from data import kraken
from views import positions
from ui import components


def render():
    components.page_header("Depots", "Krypto", "Kraken-Bestand, manuelle Positionen und langfristiger Verlauf.")

    key, secret = config.kraken_keys()
    disabled = not (key and secret)
    c1, c2, c3 = st.columns([1, 1, 1], vertical_alignment="center")
    with c1:
        sync_clicked = st.button("🔄 Kraken synchronisieren",
                                 disabled=disabled, use_container_width=True)
    with c2:
        reconstruct_clicked = st.button("📈 Wertverlauf aus Kraken-Historie",
                                        disabled=disabled, use_container_width=True,
                                        help="Rekonstruiert den echten historischen Wert deines "
                                             "Krypto-Depots aus der kompletten Kraken-Ledger-Historie "
                                             "(tatsächliche Mengen pro Tag × historische Kurse). "
                                             "Bei neuen Trades erneut ausführen.")
    with c3:
        with_cost = st.checkbox("Einstandskurse aus Handelshistorie", value=True,
                                disabled=disabled,
                                help="Berechnet Durchschnitts-Kaufkurse (inkl. Gebühren) aus "
                                     "deinen Kraken-Trades. Benötigt die API-Berechtigung "
                                     "'Query Closed Orders & Trades'. USD-Käufe werden "
                                     "näherungsweise mit dem aktuellen USD/EUR-Kurs umgerechnet.")
    if disabled:
        st.info("Für den automatischen Abgleich `KRAKEN_API_KEY` und "
                "`KRAKEN_API_SECRET` in die `.env` eintragen "
                "(Berechtigungen: **Query Funds** + **Query Closed Orders & Trades**).")

    if sync_clicked:
        try:
            with st.spinner("Hole Bestände und Handelshistorie von Kraken ..."):
                count, symbols, warning = kraken.sync_to_db(with_cost_basis=with_cost)
            if count:
                st.success(f"{count} Positionen von Kraken übernommen: {', '.join(symbols)}")
                if warning:
                    st.warning(warning)
                elif with_cost:
                    st.caption("Einstandskurse aus der Handelshistorie berechnet. Bestände aus "
                               "Transfers/Staking haben keine Kaufhistorie - dort ggf. unten "
                               "unter *Position bearbeiten* manuell nachtragen.")
            else:
                st.warning("Keine Krypto-Bestände auf Kraken gefunden.")
        except kraken.KrakenError as e:
            st.error(f"Kraken-Fehler: {e}")
        except Exception as e:
            st.error(f"Unerwarteter Fehler: {e}")

    if reconstruct_clicked:
        try:
            with st.spinner("Lese Kraken-Ledger und historische Kurse (kann etwas dauern) ..."):
                res = kraken.reconstruct_value_history()
            if res["tage"]:
                st.success(f"Wertverlauf rekonstruiert: {res['tage']} Tage ab {res['ab_datum']}. "
                           f"Der Krypto-Verlaufschart unten zeigt jetzt den echten Verlauf.")
                if res["ohne_kurs"]:
                    st.warning("Ohne verfügbare Kurshistorie (nicht im Verlauf): "
                               + ", ".join(res["ohne_kurs"]))
            else:
                st.warning("Keine Ledger-Historie gefunden.")
        except kraken.KrakenError as e:
            st.error(f"Kraken-Fehler: {e}")
        except Exception as e:
            st.error(f"Unerwarteter Fehler: {e}")

    st.divider()
    positions.render_positions_table("crypto")
    st.divider()
    positions.render_add_form(
        "crypto",
        symbol_help="Krypto-Symbol, z.B. BTC, ETH, SOL (Kurse via CoinGecko in EUR)",
    )
