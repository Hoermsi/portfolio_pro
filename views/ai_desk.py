"""AI Desk: Portfolio-Review durch das Agenten-Team + Claude-Chat mit Tool-Zugriff."""
import json

import streamlit as st

from agents import chat as chat_mod
from agents import senior_manager
from core import config, db
from ui import components


def render():
    components.page_header("Analysen", "AI Desk", "Portfolio-Review, Rückfragen und vergangene Einschätzungen an einem Ort.")

    tab_review, tab_chat, tab_history = st.tabs(
        ["Portfolio-Review", "💬 Chat mit Claude", "Analyse-Historie"]
    )

    spec_model = st.session_state["specialist_model"]
    senior_model = st.session_state["senior_model"]

    # --- PORTFOLIO-REVIEW ---
    with tab_review:
        st.markdown(
            "Der **Risiko-Manager** prüft Diversifikation, Klumpenrisiken und "
            "Korrelationen, der **Senior Asset Manager** erstellt das Gesamturteil "
            "zu deiner Allokation."
        )
        scope_labels = {
            "🌐 Gesamtportfolio": "all",
            "📈 Nur Aktien": "stock",
            "🪙 Nur Krypto": "crypto",
        }
        scope_choice = st.radio("Was soll analysiert werden?",
                                list(scope_labels.keys()), horizontal=True)
        scope = scope_labels[scope_choice]

        est = senior_manager.estimate_cost("portfolio", spec_model, senior_model)
        st.caption(f"Geschätzte Kosten ≈ ${est:.3f}")
        if st.button("🚀 Review starten", type="primary"):
            status = st.status("Review läuft ...", expanded=True)
            result = senior_manager.run_portfolio_review(
                scope, spec_model, senior_model, progress_cb=lambda m: status.write(m)
            )
            status.update(label="Review abgeschlossen", state="complete", expanded=False)
            st.session_state[f"portfolio_review_{scope}"] = result
            st.session_state["session_cost"] = (
                st.session_state.get("session_cost", 0.0) + result.get("total_cost_usd", 0.0)
            )
        result = st.session_state.get(f"portfolio_review_{scope}")
        if result:
            st.markdown(f"### Ergebnis: {result.get('name', '')}")
            components.render_analysis_result(result, key_prefix=f"review_{scope}")

    # --- CHAT ---
    with tab_chat:
        chat_model = st.session_state["chat_model"]
        st.caption(
            f"Claude ({config.model_label(chat_model)}) hat Tool-Zugriff auf dein "
            "Portfolio und Marktdaten. Beispiele: *\"Wie steht mein Portfolio da?\"*, "
            "*\"Analysiere NVDA\"*, *\"Wie hoch ist mein Krypto-Anteil?\"*"
        )
        if st.button("🗑️ Chat leeren"):
            st.session_state["chat_api_messages"] = []
            st.session_state["chat_display"] = []
            st.rerun()

        st.session_state.setdefault("chat_api_messages", [])
        st.session_state.setdefault("chat_display", [])

        for msg in st.session_state["chat_display"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        user_input = st.chat_input("Frage zum Portfolio oder zu einem Wert ...")
        if user_input:
            st.session_state["chat_display"].append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)
            st.session_state["chat_api_messages"].append(
                {"role": "user", "content": user_input}
            )
            with st.chat_message("assistant"):
                with st.spinner("Claude denkt nach und holt Daten ..."):
                    messages, text, cost, err = chat_mod.chat_turn(
                        st.session_state["chat_api_messages"], chat_model
                    )
                st.session_state["chat_api_messages"] = messages
                st.session_state["session_cost"] = (
                    st.session_state.get("session_cost", 0.0) + cost
                )
                if err:
                    st.error(err)
                    st.session_state["chat_display"].append(
                        {"role": "assistant", "content": f"⚠️ {err}"}
                    )
                else:
                    st.markdown(text)
                    st.caption(f"≈ {cost * 100:.2f} ct")
                    st.session_state["chat_display"].append(
                        {"role": "assistant", "content": text}
                    )

    # --- HISTORIE ---
    with tab_history:
        runs = db.list_agent_runs(50)
        if not runs:
            st.caption("Noch keine Analysen durchgeführt.")
        else:
            st.caption(f"Kumulierte Analyse-Kosten gesamt: ${db.total_agent_cost():.3f}")
            for run in runs:
                label = (f"{run['created_at']} · {run['target']} · "
                         f"{run['recommendation'] or '-'} · "
                         f"Score {run['total_score'] if run['total_score'] is not None else '-'} · "
                         f"${run['cost_usd']:.4f}")
                with st.expander(label):
                    try:
                        report = json.loads(run["report_json"])
                        components.render_analysis_result(report, key_prefix=f"hist_{run['id']}")
                    except (json.JSONDecodeError, TypeError):
                        st.write(run["report_json"])
