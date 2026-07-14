"""Portfolio Pro - Einstiegspunkt (streamlit run app.py)."""
import streamlit as st

from core import config, db
from ui import components

st.set_page_config(page_title="Portfolio Pro", page_icon="💼", layout="wide")

# DB anlegen + einmalige Migration der V1-Daten
db.init_db()

# --- Globale Session-Defaults ---
st.session_state.setdefault("specialist_model", config.DEFAULT_SPECIALIST_MODEL)
st.session_state.setdefault("senior_model", config.DEFAULT_SENIOR_MODEL)
st.session_state.setdefault("chat_model", config.DEFAULT_CHAT_MODEL)
st.session_state.setdefault("session_cost", 0.0)
st.session_state.setdefault("ui_theme", db.get_meta("ui_theme") or "dark")
components.apply_theme(st.session_state["ui_theme"])

# --- Ersteinrichtung beim allerersten Start (leere DB) ---
if not db.get_meta("onboarded"):
    from views import onboarding
    onboarding.render()
    st.stop()

# --- Sidebar: kompakte Orientierung ---
with st.sidebar:
    st.markdown("## Portfolio Pro")
    st.caption("Dein klarer Blick auf Vermögen, Risiko und Chancen.")
    st.divider()
    dark_mode = st.toggle("Dunkler Modus", value=st.session_state["ui_theme"] == "dark",
                          help="Deine Auswahl wird auf diesem Gerät gespeichert.")
    new_theme = "dark" if dark_mode else "light"
    if new_theme != st.session_state["ui_theme"]:
        st.session_state["ui_theme"] = new_theme
        db.set_meta("ui_theme", new_theme)
        st.rerun()
    st.divider()
    if not config.anthropic_api_key():
        st.warning("KI ist erst nach Eintrag des ANTHROPIC_API_KEY verfügbar.")
    else:
        st.success("KI-Analyse bereit")
    st.caption("Modelle, Zielallokation und Kosten findest du unter Einstellungen.")

    # Dezenter Hinweis auf ein verfügbares Update (still, wenn kein Kanal/kein Update).
    try:
        from core import updater
        _upd = updater.check_for_update()
        if _upd:
            st.divider()
            st.info(f"⬆️ Update verfügbar: v{_upd['version']}\n\n"
                    "Einstellungen → Updates")
    except Exception:
        pass

# --- Navigation ---
from views import ai_desk, ai_portfolio, asset_detail, backtest, cash, crypto, dashboard, exports, settings, stocks, transactions  # noqa: E402

pages = {
    "Übersicht": [
        st.Page(dashboard.render, title="Dashboard", icon="📊", default=True),
    ],
    "Depots": [
        st.Page(stocks.render, title="Aktien", icon="📈", url_path="aktien"),
        st.Page(crypto.render, title="Krypto", icon="🪙", url_path="krypto"),
        st.Page(cash.render, title="Cash", icon="💶", url_path="cash"),
        st.Page(transactions.render, title="Buchungen", icon="🧾", url_path="buchungen"),
    ],
    "Analysen": [
        st.Page(asset_detail.render, title="Einzelwert", icon="🔍", url_path="analyse"),
        st.Page(backtest.render, title="Backtest", icon="⏮️", url_path="backtest"),
        st.Page(ai_desk.render, title="AI Desk", icon="🧠", url_path="ai-desk"),
        st.Page(ai_portfolio.render, title="KI-Portfolios", icon="🤖", url_path="ki-portfolio"),
    ],
    "Verwaltung": [
        st.Page(settings.render, title="Einstellungen", icon="⚙️", url_path="einstellungen"),
        st.Page(exports.render, title="Exports", icon="📄", url_path="exports"),
    ],
}
st.navigation(pages).run()
