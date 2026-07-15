"""Portfolio Pro - Einstiegspunkt (streamlit run app.py)."""
import streamlit as st

from core import config, db
from ui import components

st.set_page_config(page_title="Portfolio Pro", page_icon="💼", layout="wide")

# Demo-Modus VOR init_db: schaltet auf die separate Demo-DB um (echte Daten
# bleiben unberührt). Default aus bei jedem Start (nicht persistiert).
demo_mode = st.session_state.setdefault("demo_mode", False)
config.set_demo_mode(demo_mode)

# DB anlegen + einmalige Migration der V1-Daten
db.init_db()

if demo_mode:
    from core import demo
    demo.ensure_seeded()

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

    demo_toggle = st.toggle("Demo-Modus", value=st.session_state["demo_mode"],
                            help="Zeigt ein fiktives Beispiel-Depot mit echten Live-Kursen. "
                                 "Deine echten Daten bleiben unberührt.")
    if demo_toggle != st.session_state["demo_mode"]:
        st.session_state["demo_mode"] = demo_toggle
        st.rerun()
    if st.session_state["demo_mode"]:
        st.warning("🎭 Demo-Modus aktiv – fiktive Daten")
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

    from core.version import APP_VERSION
    st.caption(f"Portfolio Pro v{APP_VERSION}")

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
