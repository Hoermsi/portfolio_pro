"""Krypto-Portfolio: Kraken-Sync + manuelle Positionen + Markt-Temperatur."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from analysis import alerts, market_timing
from core import config, db
from data import crypto as crypto_data
from data import kraken, sentiment
from views import positions
from ui import components

_SENTIMENT_LABELS = {
    "fear_greed": "Fear & Greed", "mayer": "Mayer Multiple",
    "breadth": "Altcoin-Breite", "btc_dominance": "BTC-Dominanz",
    "meme": "Meme-Momentum", "stablecoin_dominance": "Stablecoin-Dominanz",
}


def render():
    components.page_header("Depots", "Krypto", "Kraken-Bestand, manuelle Positionen und langfristiger Verlauf.")

    tab_pos, tab_watch = st.tabs(["Positionen", "Watchlist"])
    with tab_pos:
        _render_positions_tab()
    with tab_watch:
        positions.render_watchlist("crypto")


def _render_positions_tab():
    key, secret = config.kraken_keys()
    disabled = not (key and secret)
    c1, c2, c3 = st.columns([1, 1, 1], vertical_alignment="center")
    with c1:
        sync_clicked = st.button("🔄 Kraken synchronisieren",
                                 disabled=disabled, width="stretch")
    with c2:
        reconstruct_clicked = st.button("📈 Wertverlauf aus Kraken-Historie",
                                        disabled=disabled, width="stretch",
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
    _render_market_temperature()
    st.divider()
    positions.render_positions_table("crypto")
    st.divider()
    positions.render_add_form(
        "crypto",
        symbol_help="Krypto-Symbol, z.B. BTC, ETH, SOL (Kurse via CoinGecko in EUR)",
    )


def _fetch_sentiment_readings() -> tuple[dict, int | None]:
    """Alle Sentiment-Quellen parallel holen (Muster wie analysis.alerts.metrics_for)."""
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            "fear_greed": pool.submit(sentiment.fear_greed),
            "mayer": pool.submit(market_timing.mayer_multiple),
            "breadth": pool.submit(sentiment.altcoin_breadth_30d),
            "global": pool.submit(sentiment.global_metrics),
            "meme": pool.submit(sentiment.meme_market),
            "coinbase_rank": pool.submit(sentiment.coinbase_app_rank),
        }
        results = {k: f.result() for k, f in futures.items()}
    # global_metrics() liefert ein Dict mit sowohl btc_dominance als auch
    # stablecoin_dominance - beide Scorer greifen sich daraus nur ihr eigenes Feld.
    readings = {
        "fear_greed": results["fear_greed"],
        "mayer": results["mayer"],
        "breadth": results["breadth"],
        "btc_dominance": results["global"],
        "meme": results["meme"],
        "stablecoin_dominance": results["global"],
    }
    return readings, results["coinbase_rank"]


def _render_market_temperature():
    st.markdown("### 🌡️ Markt-Temperatur")
    with st.spinner("Lade Sentiment-Indikatoren …"):
        readings, coinbase_rank = _fetch_sentiment_readings()
    temp = market_timing.market_temperature(readings)

    if temp["score"] is None:
        st.info("Sentiment-Daten gerade nicht erreichbar.")
        return

    # Tageswert je Indikator festhalten - CoinGecko liefert Dominanz/Meme nur
    # als Momentanwert; der eigene Verlauf entsteht dadurch erst mit der Zeit.
    for row in temp["breakdown"]:
        db.save_sentiment(row["key"], row["score"])
    db.save_sentiment("overall", temp["score"])

    col_gauge, col_info = st.columns([1, 2])
    with col_gauge:
        components.render_gauge(temp["score"], "Markt-Temperatur",
                                key="crypto_temp_gauge", invert=True)
    with col_info:
        st.markdown(f"**{temp['classification']}** — {temp['score']:.0f}/100")
        if coinbase_rank:
            st.warning(f"📱 Coinbase auf Platz {coinbase_rank} der Gratis-Charts "
                      f"(Apple US) — erhöhtes Retail-Interesse.")
        else:
            st.caption("Coinbase aktuell nicht in den Top-200 Gratis-Apps.")

    with st.expander("Wie kommt dieser Wert zustande?"):
        if temp["breakdown"]:
            rows = pd.DataFrame(temp["breakdown"])[["label", "text", "score", "weight_pct"]]
            rows.columns = ["Indikator", "Wert", "Teil-Score", "Gewicht (%)"]
            st.dataframe(rows, hide_index=True, width="stretch",
                        column_config={
                            "Teil-Score": st.column_config.NumberColumn(format="%.0f"),
                            "Gewicht (%)": st.column_config.NumberColumn(format="%.1f %%"),
                        })
        if temp["unavailable"]:
            missing = ", ".join(_SENTIMENT_LABELS.get(k, k) for k in temp["unavailable"])
            st.caption(f"Gerade nicht verfügbar (Gewichte auf die übrigen umverteilt): {missing}")
        st.caption("Gewichtung ist eine Einschätzung, kein Backtest-Ergebnis. Dominanz- und "
                  "Meme-Momentum-Trends werden ab jetzt selbst aufgezeichnet (CoinGecko "
                  "liefert dafür nur Momentanwerte).")

    _render_coin_details()


def _render_coin_details():
    symbols = sorted({p.symbol for p in db.list_positions("crypto")
                      if p.symbol != "EUR" and p.quantity > 0})
    if not symbols:
        return
    with st.expander("Deine Coins im Detail (ATH-Abstand, RSI)"):
        with st.spinner("Lade Kennzahlen …"):
            with ThreadPoolExecutor(max_workers=6) as pool:
                metric_futures = {s: pool.submit(alerts.asset_metrics, s, "crypto") for s in symbols}
                market_futures = {s: pool.submit(crypto_data.get_market_data, s) for s in symbols}
                metrics = {s: f.result() for s, f in metric_futures.items()}
                market = {s: f.result() for s, f in market_futures.items()}
        rows = [{
            "Symbol": s,
            "Kurs (€)": metrics.get(s, {}).get("price_eur"),
            "RSI": metrics.get(s, {}).get("rsi"),
            "Abstand ATH (%)": market.get(s, {}).get("ath_abstand_pct"),
        } for s in symbols]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                    column_config={
                        "Kurs (€)": st.column_config.NumberColumn(format="%.4f"),
                        "RSI": st.column_config.NumberColumn(format="%.0f"),
                        "Abstand ATH (%)": st.column_config.NumberColumn(format="%+.1f %%"),
                    })
