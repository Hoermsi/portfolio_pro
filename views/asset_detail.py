"""Einzelwert-Analyse: Chart, Kennzahlen, News und Agenten-Voll-Analyse."""
import streamlit as st

from agents import senior_manager
from analysis import risk as risk_analysis
from analysis import technical
from data import crypto as crypto_data
from data import news as news_data
from data import stocks as stock_data
from ui import components

_PERIODS = {"3 Monate": ("3mo", 90), "1 Jahr": ("1y", 365),
            "3 Jahre": ("3y", 1095), "5 Jahre": ("5y", 1825)}


def render():
    components.page_header("Analysen", "Einzelwert", "Technik, Fundamentaldaten, Risiko und KI-Einschätzung für einen Wert.")

    c1, c2, c3 = st.columns([2, 1, 1])
    symbol = c1.text_input("Symbol", value=st.session_state.get("detail_symbol", ""),
                           help="Aktien-Ticker (NVDA, SAP.DE) oder Krypto-Symbol (BTC)").strip().upper()
    asset_type = c2.radio("Typ", ["Aktie", "Krypto"], horizontal=True,
                          index=1 if st.session_state.get("detail_type") == "crypto" else 0)
    period_label = c3.selectbox("Zeitraum", list(_PERIODS.keys()), index=1)

    if not symbol:
        st.info("Symbol eingeben - der Wert muss nicht im Portfolio sein.")
        return
    st.session_state["detail_symbol"] = symbol
    at = "crypto" if asset_type == "Krypto" else "stock"
    st.session_state["detail_type"] = at

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
    risk = risk_analysis.asset_risk(df)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Kurs", f"{tech['kurs']:,.4g} {currency}")
    m2.metric("RSI (14)", f"{tech['rsi']:.0f}")
    m3.metric("Technik-Score", f"{tech['t_score']}/100")
    if risk:
        m4.metric("Volatilität p.a.", f"{risk['volatilitaet_pct']:.0f}%")
        m5.metric("Max Drawdown", f"{risk['max_drawdown_pct']:.0f}%")

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
