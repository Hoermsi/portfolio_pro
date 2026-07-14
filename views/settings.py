"""Lokale Einstellungen für Zielallokation, Risikoprofil und KI-Modelle."""
import json
from datetime import date

import streamlit as st

from core import config, db
from core.profile import max_target_return, risk_profile, save_risk_profile
from ui import components

_DEFAULT_TARGETS = {"stock": 60, "crypto": 20, "cash": 20}


def target_allocation() -> dict[str, float]:
    """Gespeicherte Zielallokation mit sicheren Standardwerten laden."""
    raw = db.get_meta("target_allocation")
    try:
        values = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        values = {}
    return {key: float(values.get(key, default)) for key, default in _DEFAULT_TARGETS.items()}


def render():
    components.page_header("Verwaltung", "Einstellungen", "Persönliche Leitplanken für deine lokale App.")

    tab_targets, tab_keys, tab_ai, tab_updates = st.tabs(
        ["Zielallokation", "API-Keys", "KI & Kosten", "Updates"])
    with tab_targets:
        st.markdown("### Strategische Zielallokation")
        st.caption("Die Zielwerte dienen dem Dashboard als Orientierung. Sie lösen keine automatischen Trades aus.")
        targets = target_allocation()
        with st.form("target_allocation_form"):
            c1, c2, c3 = st.columns(3)
            stock = c1.number_input("Aktien (%)", 0, 100, int(targets["stock"]), step=5)
            crypto = c2.number_input("Krypto (%)", 0, 100, int(targets["crypto"]), step=5)
            cash = c3.number_input("Cash (%)", 0, 100, int(targets["cash"]), step=5)
            total = stock + crypto + cash
            st.caption(f"Summe: {total} %")
            save = st.form_submit_button("Zielallokation speichern", type="primary")
        if save:
            if total != 100:
                st.error("Die drei Zielwerte müssen zusammen genau 100 % ergeben.")
            else:
                db.set_meta("target_allocation", json.dumps({"stock": stock, "crypto": crypto, "cash": cash}))
                st.success("Zielallokation gespeichert.")

        st.divider()
        _render_risk_profile()

    with tab_keys:
        _render_api_keys()

    with tab_ai:
        st.markdown("### KI-Modelle")
        _render_ai_tab()

    with tab_updates:
        _render_updates()


def _render_api_keys():
    st.markdown("### API-Keys")
    st.caption("Werden lokal auf diesem Gerät gespeichert (unverschlüsselt in der portfolio.db, "
               "dieselbe Vertrauensstufe wie eine .env). Der gespeicherte Wert wird nie angezeigt.")

    _api_key_row("Anthropic API Key", "anthropic_api_key", "ANTHROPIC_API_KEY",
                 "Für die KI-Analysen (Console-Key von console.anthropic.com).")
    st.divider()
    _api_key_row("Kraken API Key", "kraken_api_key", "KRAKEN_API_KEY",
                 "Nur Leserechte (Query Funds + Query Closed Orders & Trades).")
    _api_key_row("Kraken API Secret", "kraken_api_secret", "KRAKEN_API_SECRET", None)


def _save_api_key_callback(meta_key: str, label: str):
    """Läuft vor dem Rerun - hier darf der Widget-State noch verändert werden."""
    val = st.session_state.get(f"apikey_{meta_key}", "")
    config.save_api_key(meta_key, val)
    st.session_state[f"apikey_{meta_key}"] = ""
    st.session_state[f"_apikey_msg_{meta_key}"] = f"{label} gespeichert."


def _delete_api_key_callback(meta_key: str, label: str, from_env: bool):
    config.save_api_key(meta_key, None)
    msg = f"{label} entfernt." + (" .env-Wert wird wieder verwendet." if from_env else "")
    st.session_state[f"_apikey_msg_{meta_key}"] = msg


def _api_key_row(label: str, meta_key: str, env_key: str, help_text: str | None):
    import os
    from_db = bool(db.get_meta(meta_key))
    from_env = bool(os.getenv(env_key))
    status = "✓ in der App gespeichert" if from_db else ("✓ aus .env" if from_env else "— nicht gesetzt")
    st.markdown(f"**{label}** · {status}")
    st.text_input(label, type="password", key=f"apikey_{meta_key}",
                 label_visibility="collapsed", placeholder="Neuen Wert eingeben …",
                 help=help_text)
    new_val = st.session_state.get(f"apikey_{meta_key}", "")
    c1, c2 = st.columns([1, 1])
    c1.button("Speichern", key=f"save_{meta_key}", disabled=not new_val,
             on_click=_save_api_key_callback, args=(meta_key, label))
    c2.button("Entfernen", key=f"del_{meta_key}", disabled=not from_db,
             on_click=_delete_api_key_callback, args=(meta_key, label, from_env))
    msg = st.session_state.pop(f"_apikey_msg_{meta_key}", None)
    if msg:
        st.success(msg)


def _render_risk_profile():
    """Risikobereitschaft + Zielrendite (gekoppelt) + Pensionsjahr.

    Bewusst KEIN st.form: der Zielrendite-Bereich muss sofort auf den
    Risiko-Regler reagieren, Formulare rerendern aber erst beim Submit.
    """
    st.markdown("### Risikoprofil")
    st.caption("Fließt in die Empfehlungen des Portfolio-Strategen und den Portfolio-Review ein. "
               "Die Zielrendite ist an die Risikobereitschaft gekoppelt - mehr Rendite erfordert mehr Risiko.")
    profile = risk_profile()

    risk = st.slider("Risikobereitschaft", 1, 10, int(profile["risk"]), key="settings_risk",
                     help="Jünger oder langer Anlagehorizont = tendenziell mehr Risiko möglich. "
                          "Naher Hauskauf oder Pensionsantritt = weniger.")
    st.caption("1 = sehr konservativ · 5 = ausgewogen · 10 = sehr risikofreudig")

    cap = max_target_return(risk)
    # Streamlit wirft eine Exception, wenn der gemerkte Slider-Wert über dem neuen
    # max_value liegt (Risiko wurde gesenkt) - session_state vorab clampen.
    state_key = "settings_target_return"
    if state_key in st.session_state and st.session_state[state_key] > cap:
        st.session_state[state_key] = cap
        st.info(f"Zielrendite wurde auf {cap:.1f} % begrenzt - bei Risikostufe {risk} "
                f"ist maximal {cap:.1f} % p.a. wählbar.")
    target = st.slider("Zielrendite p.a. (%)", 0.0, cap,
                       min(float(profile["target_return_pct"]), cap), step=0.5, key=state_key)

    this_year = date.today().year
    year = st.number_input("Pensionsantritt (Jahr)", min_value=this_year, max_value=2100,
                           value=int(profile["retirement_year"] or 2060),
                           key="settings_retirement_year",
                           help="Zielhorizont für die Zinseszins-Projektion auf dem Dashboard.")

    monthly = st.number_input("Monatliche Sparrate (€)", min_value=0.0, max_value=100000.0,
                              value=float(profile["monthly_contribution"]), step=50.0,
                              key="settings_monthly",
                              help="Betrag, den du monatlich zusätzlich investierst. Fließt in die "
                                   "Zinseszins-Projektion auf dem Dashboard ein.")

    if st.button("Risikoprofil speichern", type="primary", key="save_risk_profile"):
        save_risk_profile(risk, target, int(year), monthly)
        st.success("Risikoprofil gespeichert.")


def _render_ai_tab():
    st.caption("Die Auswahl gilt für diese Browser-Sitzung. Preise sind Schätzwerte und können vom Anbieter abweichen.")
    labels = list(config.CLAUDE_MODELS.keys())
    ids = list(config.CLAUDE_MODELS.values())

    def _select(title: str, state_key: str):
        current = st.session_state[state_key]
        idx = ids.index(current) if current in ids else 0
        choice = st.selectbox(title, labels, index=idx, key=f"sel_{state_key}")
        st.session_state[state_key] = config.CLAUDE_MODELS[choice]

    _select("Spezialisten", "specialist_model")
    _select("Senior Asset Manager", "senior_model")
    _select("Chat", "chat_model")
    st.divider()
    c1, c2 = st.columns(2)
    c1.metric("KI-Kosten diese Sitzung", f"${st.session_state['session_cost']:.3f}")
    c2.metric("Analysen bisher", f"${db.total_agent_cost():.2f}")


def _render_updates():
    from core import updater
    from core.version import APP_VERSION

    st.markdown("### Programm-Updates")
    st.metric("Installierte Version", f"v{APP_VERSION}")

    if not updater.is_configured():
        st.info("Es ist noch kein Update-Kanal hinterlegt. Der Entwickler trägt dazu "
                "das GitHub-Repository in `core/updater.py` (`GITHUB_REPO`) ein.")
        return

    st.caption("Prüft das neueste Release auf GitHub. Deine Daten (Positionen, Cash, "
               "API-Keys) liegen getrennt vom Programm und bleiben bei einem Update erhalten.")

    if st.button("🔄 Nach Updates suchen"):
        updater.check_for_update.cache_clear()
        st.session_state["update_info"] = updater.check_for_update()
        st.session_state["update_checked"] = True

    if st.session_state.get("update_checked"):
        info = st.session_state.get("update_info")
        if not info:
            st.success("Du hast die aktuelle Version.")
            return
        st.warning(f"Neue Version verfügbar: **v{info['version']}**")
        if info.get("notes"):
            with st.expander("Änderungen"):
                st.markdown(info["notes"])
        st.caption("Beim Aktualisieren lädt die App die neue Version, schließt sich und "
                   "startet neu. Bitte danach kurz warten.")
        if st.button("⬇️ Jetzt aktualisieren", type="primary"):
            with st.spinner("Update wird geladen und vorbereitet …"):
                try:
                    updater.apply_update(info["asset_url"], info["version"])
                except Exception as e:  # noqa: BLE001 - dem Nutzer den Grund zeigen
                    st.error(f"Update fehlgeschlagen: {e}")
                    return
            st.info("Update vorbereitet. Die App wird jetzt beendet und aktualisiert sich. "
                    "Starte sie anschließend über die gewohnte Verknüpfung.")
            st.stop()
