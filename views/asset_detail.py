"""Markt & Einzelwertanalyse: Markt-Temperatur (Krypto/Aktien) oben,
darunter Chart, Kennzahlen, News und Agenten-Voll-Analyse für einen Einzelwert."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from agents import senior_manager
from analysis import alerts, market_timing, risk as risk_analysis, technical
from core import db, profile
from data import crypto as crypto_data
from data import news as news_data
from data import sentiment
from data import stocks as stock_data
from ui import components

_PERIODS = {"3 Monate": ("3mo", 90), "1 Jahr": ("1y", 365),
            "3 Jahre": ("3y", 1095), "5 Jahre": ("5y", 1825)}


def render():
    components.page_header("Analysen", "Markt & Einzelwertanalyse",
                           "Markt-Stimmung und Technik, Fundamentaldaten, Risiko und KI-Einschätzung für einen Wert.")

    market_label = st.radio("Markt", ["Krypto", "Aktien"], horizontal=True, key="indicator_market")
    market = "crypto" if market_label == "Krypto" else "stock"
    at = market
    _render_market_temperature(market)
    st.divider()

    st.markdown("### 🔎 Einzelwertanalyse")
    c1, c2 = st.columns([3, 1])
    symbol = c1.text_input("Symbol", value=st.session_state.get("detail_symbol", ""),
                           help=("Krypto-Symbol (BTC)" if at == "crypto"
                                 else "Aktien-Ticker (NVDA, SAP.DE)")).strip().upper()
    period_label = c2.selectbox("Zeitraum", list(_PERIODS.keys()), index=1)

    if not symbol:
        st.info("Symbol eingeben - der Wert muss nicht im Portfolio sein.")
        return
    st.session_state["detail_symbol"] = symbol

    period_yf, period_days = _PERIODS[period_label]
    with st.spinner("Lade Kursdaten ..."):
        if at == "crypto":
            df = crypto_data.get_history(symbol, days=period_days)
            currency = "EUR"
        else:
            df = stock_data.get_history(symbol, period_yf)
            currency = stock_data.get_currency(symbol)
    if df is None or df.empty:
        st.error(f"Keine Kursdaten für '{symbol}' gefunden - Symbol prüfen.")
        return

    tech = technical.summarize(df)
    risk = risk_analysis.asset_risk(df, asset_type=at)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Kurs", f"{tech['kurs']:,.4g} {currency}")
    m2.metric("RSI (14)", f"{tech['rsi']:.0f}")
    m3.metric("Technik-Score", f"{tech['t_score']}/100")
    if risk:
        m4.metric("Volatilität p.a.", f"{risk['volatilitaet_pct']:.0f}%")
        m5.metric("Max Drawdown", f"{risk['max_drawdown_pct']:.0f}%")
    m6.metric("ATR% (Tagesspanne)",
             f"{tech['atr_pct']:.1f}%" if tech.get("atr_pct") is not None else "—",
             help="Durchschnittliche Tages-Schwankungsbreite (Average True Range) "
                  "relativ zum Kurs - Orientierung für Stop-/Positionsgrößen. "
                  "Für Krypto derzeit nicht verfügbar (keine Intraday-High/Low-Daten).")

    components.render_price_chart(tech["df"], tech["fibs"], key=f"price_{symbol}_{at}")

    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("#### 📊 Fundamentaldaten")
        fundamentals = (crypto_data.get_market_data(symbol) if at == "crypto"
                        else stock_data.get_fundamentals(symbol))
        fundamentals = {k: v for k, v in fundamentals.items() if v is not None}
        if fundamentals:
            for k, v in fundamentals.items():
                if isinstance(v, float):
                    v = f"{v:,.2f}"
                st.markdown(f"- **{k.replace('_', ' ').title()}:** {v}")
        else:
            st.caption("Keine Fundamentaldaten verfügbar.")
    with col_r:
        st.markdown("#### 📰 News")
        news = news_data.get_news(symbol, at)
        if news:
            for n in news:
                st.caption(f"**{n['source']}**: [{n['title']}]({n['link']})")
        else:
            st.caption("Keine aktuellen News gefunden.")

    st.divider()
    st.markdown("### 🤖 Agenten-Analyse (Senior Asset Manager)")
    spec_model = st.session_state["specialist_model"]
    senior_model = st.session_state["senior_model"]
    est = senior_manager.estimate_cost("asset", spec_model, senior_model)
    st.caption(f"4 Spezialisten + Senior · geschätzte Kosten ≈ ${est:.3f} "
               f"(Modelle in der Sidebar wählbar)")

    if st.button("🚀 Voll-Analyse starten", type="primary"):
        status = st.status("Analyse läuft ...", expanded=True)
        result = senior_manager.run_asset_analysis(
            symbol, at, spec_model, senior_model,
            progress_cb=lambda msg: status.write(msg),
        )
        status.update(label="Analyse abgeschlossen", state="complete", expanded=False)
        st.session_state[f"analysis_{symbol}_{at}"] = result
        st.session_state["session_cost"] = (
            st.session_state.get("session_cost", 0.0) + result.get("total_cost_usd", 0.0)
        )

    result = st.session_state.get(f"analysis_{symbol}_{at}")
    if result:
        components.render_analysis_result(result, key_prefix=f"asset_{symbol}_{at}")


def _fetch_crypto_readings() -> tuple[dict, int | None]:
    """Alle Krypto-Sentiment-Quellen parallel holen (Muster wie alerts.metrics_for)."""
    with ThreadPoolExecutor(max_workers=6) as pool:
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


def _fetch_stock_readings() -> dict:
    """Alle Aktien-Sentiment-Quellen sequentiell holen.

    Bewusst NICHT über ThreadPoolExecutor wie beim Krypto-Fetch: dort läuft
    yfinance nur EINMAL parallel mit (mayer_multiple, die übrigen Quellen sind
    andere APIs). Hier würden vier yf.download()-Aufrufe für verschiedene
    Ticker gleichzeitig laufen - reproduzierbar nicht thread-sicher (mal
    stille None-Rückgaben, mal harte TypeError/ValueError durch vertauschte
    Spalten). Sequentiell ist minimal langsamer, aber verlässlich; der
    bestehende ttl_cache(300) auf stock_data.get_history hält Folgeaufrufe schnell.
    """
    return {
        "vix": market_timing.vix_level(),
        "sp500_ma200": market_timing.sp500_ma200_ratio(),
        "breadth": market_timing.breadth_ratio(),
        "risk_appetite": market_timing.risk_appetite_ratio(),
    }


def _render_market_temperature(market: str):
    coinbase_rank = None
    with st.spinner("Lade Sentiment-Indikatoren …"):
        if market == "crypto":
            readings, coinbase_rank = _fetch_crypto_readings()
        else:
            readings = _fetch_stock_readings()
    temp = market_timing.market_temperature(readings, market=market)

    if temp["score"] is None:
        st.markdown("### 🌡️ Markt-Temperatur")
        st.info("Sentiment-Daten gerade nicht erreichbar.")
        return

    # Tageswert je Indikator festhalten, mit Markt-Praefix (Krypto und Aktien
    # duerfen sich am selben Tag nicht ueberschreiben - core.db._migrate_sentiment_prefix).
    for row in temp["breakdown"]:
        db.save_sentiment(f"{market}:{row['key']}", row["score"])
    db.save_sentiment(f"{market}:overall", temp["score"])

    color = components.gauge_color(temp["score"], invert=True)
    st.markdown(
        f"### 🌡️ Markt-Temperatur: "
        f"<span style='color:{color}'>{temp['classification']} — {temp['score']:.0f}/100</span>",
        unsafe_allow_html=True,
    )
    coverage = temp["coverage_pct"]
    if coverage < 100:
        st.caption(f"📊 {coverage:.0f}% der Gewichtung verfügbar "
                   f"({len(temp['breakdown'])} von {len(temp['breakdown']) + len(temp['unavailable'])} Indikatoren)."
                   + (" Bei geringer Abdeckung wird keine „Extreme“-Einordnung ausgegeben."
                      if coverage < 60 else ""))
    if market == "crypto":
        if coinbase_rank:
            st.warning(f"📱 Coinbase auf Platz {coinbase_rank} der Gratis-Charts "
                      f"(Apple US) — erhöhtes Retail-Interesse.")
        else:
            st.caption("Coinbase aktuell nicht in den Top-200 Gratis-Apps.")

    breakdown = temp["breakdown"]
    cols = st.columns([2] + [1] * len(breakdown), vertical_alignment="bottom")
    with cols[0]:
        components.render_gauge(temp["score"], "Markt-Temperatur",
                                key=f"temp_gauge_{market}", invert=True, height=200)
    for col, row in zip(cols[1:], breakdown):
        with col:
            short_label = row["label"].split(" (")[0]
            components.render_gauge(row["score"], short_label,
                                    key=f"sub_gauge_{market}_{row['key']}",
                                    invert=True, height=150)

    if temp["unavailable"]:
        labels = market_timing.labels_for(market)
        missing = ", ".join(labels.get(k, k) for k in temp["unavailable"])
        st.caption(f"Gerade nicht verfügbar (Gewichte auf die übrigen umverteilt): {missing}")
    extra = (" Dominanz- und Meme-Momentum-Trends werden ab jetzt selbst aufgezeichnet "
            "(CoinGecko liefert dafür nur Momentanwerte)." if market == "crypto" else "")
    st.caption("Gewichtung ist eine Einschätzung, kein Backtest-Ergebnis." + extra)

    st.divider()
    _render_cycle_ladder(market, temp, readings)

    _render_position_details(market)


def _render_cycle_ladder(market: str, temp: dict, readings: dict):
    """Zweites Barometer 'Zyklus-Position' + eigene, frei einstellbare Kauf-/
    Verkaufs-Stufen. Zeigt nur an, was DEINE Regel gerade sagt - die App
    verkauft/kauft nichts automatisch (wie das Notgroschen-Feature: rein
    informativ, keine automatischen Aktionen) und bewertet die Regel selbst
    nicht als Anlageempfehlung.

    Krypto: nur Fear&Greed + Mayer Multiple - die einzigen zwei Krypto-
    Indikatoren mit echter Mehrjahres-Historie (die übrigen 4 liefert
    CoinGecko nur als Momentanwert, siehe market_temperature()-Docstring).
    Aktien: entspricht 1:1 der Markt-Temperatur, da dort alle 4 Indikatoren
    ohnehin volle Kurs-Historie haben - keine Reduktion nötig oder möglich.
    """
    if market == "crypto":
        cycle = market_timing.market_temperature(
            {"fear_greed": readings.get("fear_greed"), "mayer": readings.get("mayer")},
            market="crypto",
        )
    else:
        cycle = temp
    if cycle["score"] is None:
        return

    cfg = profile.ladder_config(market)
    sell, buy = cfg["sell"], cfg["buy"]
    score = cycle["score"]
    sell_tier = market_timing.active_ladder_tier(score, sell, "sell")
    buy_tier = market_timing.active_ladder_tier(score, buy, "buy")

    st.markdown("### 🎯 Zyklus-Position")
    if market == "crypto":
        st.caption(f"Nur Fear&Greed + Mayer Multiple (~{cycle['coverage_pct']:.0f}% Gewichtung "
                   "der Markt-Temperatur) - die einzigen Krypto-Indikatoren mit echter "
                   "Mehrjahres-Historie. Die übrigen liefert CoinGecko nur als Momentanwert.")
    else:
        st.caption("Entspricht der Markt-Temperatur - alle 4 Aktien-Indikatoren haben "
                   "ohnehin volle Kurs-Historie.")

    col_gauge, col_status = st.columns([1, 2])
    with col_gauge:
        components.render_ladder_gauge(score, "Zyklus-Position", buy, sell,
                                       key=f"cycle_gauge_{market}", height=200)
    with col_status:
        if sell_tier:
            st.warning(f"🔴 Verkaufs-Stufe {sell_tier}/3 erreicht (Score {score:.0f}) — "
                      f"deine Regel: {profile.LADDER_FRACTIONS_PCT[sell_tier - 1]}% verkaufen.")
        elif buy_tier:
            st.success(f"🟢 Kauf-Stufe {buy_tier}/3 erreicht (Score {score:.0f}) — "
                      f"deine Regel: {profile.LADDER_FRACTIONS_PCT[buy_tier - 1]}% des "
                      "verfügbaren Cash einsetzen.")
        else:
            st.caption(f"Keine Stufe aktiv (Score {score:.0f}).")
        st.caption(
            "⚠️ Das ist **deine eigene, frei einstellbare Regel** — keine Anlageempfehlung "
            "der App. Feste Schwellen hätten in vergangenen Bullruns oft zu früh verkauft "
            "(z.B. Schwelle 80 im Nov. 2020 erreicht, BTC stieg danach noch +288%) oder "
            "Alt-Coin-Tops verpasst (die Hoch-Tage einzelner Coins liegen historisch über "
            "die gesamte Skala verteilt). Sinnvoller Einsatz eher als ein Signal unter "
            "mehreren, nicht als Automatismus."
        )

    with st.expander("⚙️ Eigene Stufen einstellen"):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Verkauf (rot)**")
            s1 = st.number_input("Stufe 1 (33%)", 0.0, 100.0, sell[0], step=1.0, key=f"ladder_sell1_{market}")
            s2 = st.number_input("Stufe 2 (66%)", 0.0, 100.0, sell[1], step=1.0, key=f"ladder_sell2_{market}")
            s3 = st.number_input("Stufe 3 (100%)", 0.0, 100.0, sell[2], step=1.0, key=f"ladder_sell3_{market}")
        with c2:
            st.markdown("**Kauf (grün)**")
            b1 = st.number_input("Stufe 1 (33%)", 0.0, 100.0, buy[0], step=1.0, key=f"ladder_buy1_{market}")
            b2 = st.number_input("Stufe 2 (66%)", 0.0, 100.0, buy[1], step=1.0, key=f"ladder_buy2_{market}")
            b3 = st.number_input("Stufe 3 (100%)", 0.0, 100.0, buy[2], step=1.0, key=f"ladder_buy3_{market}")
        if st.button("Stufen speichern", key=f"ladder_save_{market}"):
            profile.save_ladder_config(market, [s1, s2, s3], [b1, b2, b3])
            st.success("Eigene Stufen gespeichert.")
            st.rerun()


def _render_position_details(market: str):
    positions = db.list_positions(market)
    symbols = sorted({p.symbol for p in positions if p.symbol != "EUR" and p.quantity > 0})
    if not symbols:
        return
    label = "ATH-Abstand" if market == "crypto" else "52W-Hoch-Abstand"
    with st.expander(f"Deine Positionen im Detail ({label}, RSI)"):
        with st.spinner("Lade Kennzahlen …"):
            if market == "crypto":
                # ATH-Abstand etc. in EINEM Batch-Request holen (get_market_data_batch) -
                # ein get_market_data()-Aufruf PRO Position parallel sprengt reproduzierbar
                # das freie CoinGecko-Rate-Limit (429), wodurch der ATH-Abstand für die
                # meisten Coins leer bliebe.
                extra = crypto_data.get_market_data_batch(symbols)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    metric_futures = {s: pool.submit(alerts.asset_metrics, s, market) for s in symbols}
                    metrics = {s: f.result() for s, f in metric_futures.items()}
            else:
                # Sequentiell: yfinance ist bei gleichzeitigen yf.download()-Aufrufen
                # für unterschiedliche Ticker nicht thread-sicher (siehe
                # _fetch_stock_readings) - hat hier reproduzierbar Abstürze und
                # danach über den ttl_cache falsche Werte für's eigene Symbol
                # der Einzelwertanalyse verursacht.
                metrics = {s: alerts.asset_metrics(s, market) for s in symbols}
                extra = {s: stock_data.get_fundamentals(s) for s in symbols}

        rows = []
        for s in symbols:
            price = metrics.get(s, {}).get("price_eur")
            if market == "crypto":
                dist = extra.get(s, {}).get("ath_abstand_pct")
            else:
                high_52w = extra.get(s, {}).get("52w_hoch")
                dist = (price / high_52w - 1) * 100 if price and high_52w else None
            rows.append({
                "Symbol": s,
                "Kurs (€)": price,
                "RSI": metrics.get(s, {}).get("rsi"),
                label: dist,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                    column_config={
                        "Kurs (€)": st.column_config.NumberColumn(format="%.4f"),
                        "RSI": st.column_config.NumberColumn(format="%.0f"),
                        label: st.column_config.NumberColumn(format="%+.1f %%"),
                    })
