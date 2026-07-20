"""KI-Portfolios: zwei getrennte, KI-gemanagte Schatten-Depots (Krypto / Aktien)
im Wettkampf gegen die jeweilige Klasse des echten Portfolios.
"""
import pandas as pd
import plotly.express as px
import streamlit as st

from agents import strategist
from core import config, shadow
from core.portfolio import total_value, valued_positions
from ui import components

_AKTION_ICON = {"kaufen": "🟢 Kauf", "verkaufen": "🔴 Verkauf",
                "umschichten": "🔄 Umschichtung", "halten": "⚪ Halten"}


def render():
    components.page_header("Analysen", "KI-Portfolios", "Zwei virtuelle Depots im Vergleich mit deinen echten Beständen.")
    st.caption("Die KI verwaltet je ein getrenntes Krypto- und Aktien-Experiment. So wird sichtbar, "
               "ob ihre Strategie dein jeweiliges echtes Depot schlägt.")

    tab_crypto, tab_stock = st.tabs(["🪙 Krypto-KI", "📈 Aktien-KI"])
    with tab_crypto:
        _render_scope("crypto")
    with tab_stock:
        _render_scope("stock")


def _render_scope(scope: str):
    if not shadow.exists(scope):
        _render_setup(scope)
    else:
        _render_dashboard(scope)


def _render_setup(scope: str):
    name = shadow.SCOPES[scope]
    klasse = "Krypto-Positionen" if scope == "crypto" else "Aktien-Positionen"
    st.info(
        f"Noch kein **{name}**-Depot angelegt. Beim Start wird eine **exakte Kopie "
        f"deiner aktuellen {klasse}** erstellt. Ab dann handelt die KI virtuell "
        "(mit 0,25 % Trade-Kosten je Trade), und beide Depots werden verglichen."
    )
    with st.spinner("Ermittle aktuellen Wert ..."):
        start_value = total_value(valued_positions(scope))
    st.metric(f"Aktueller Wert deiner {klasse} (Startkapital)", f"{start_value:,.2f} €")
    if start_value <= 0:
        st.warning(f"Keine {klasse} vorhanden oder keine Kurse verfügbar.")
        return
    if st.button(f"🚀 {name}-Depot jetzt anlegen", type="primary", key=f"init_{scope}"):
        info = shadow.init_from_real(scope)
        st.success(f"{name}-Depot angelegt mit {info['shadow_start']:,.2f} € Startkapital.")
        st.rerun()


def _render_dashboard(scope: str):
    name = shadow.SCOPES[scope]
    info = shadow.start_info(scope)
    with st.spinner("Bewerte beide Depots ..."):
        real_total = total_value(valued_positions(scope))
        shadow_vals = shadow.valued_shadow(scope)
        shadow_total = shadow.total_value(scope, shadow_vals)
    comp = shadow.comparison_df(scope)

    # --- Kopfzeilen-Metriken ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💼 Echt", f"{real_total:,.2f} €",
              help="Wert der entsprechenden Klasse deines echten Portfolios.")
    c2.metric(f"🤖 {name}", f"{shadow_total:,.2f} €")
    real_idx = ki_idx = None
    if comp is not None and len(comp) >= 1:
        last = comp.iloc[-1]
        real_idx, ki_idx = last.get("Echt"), last.get("KI")
    if real_idx is not None and ki_idx is not None:
        c3.metric("📈 KI seit Start", f"{ki_idx - 100:+.1f} %",
                  f"Echt {real_idx - 100:+.1f} %")
        c4.metric("🏆 Outperformance", f"{ki_idx - real_idx:+.1f} Pkt",
                  help="Indexpunkte Vorsprung des KI-Depots (Start = 100).")
    st.caption(f"Experiment-Start: {info['date']} · Startkapital {info['shadow_start']:,.2f} € · "
               f"Trade-Kosten je virtuellem Trade: 0,25 %")
    if info.get("skipped"):
        st.warning(
            "Beim Start ohne Kurs und daher **nicht** ins KI-Depot aufgenommen: "
            + ", ".join(info["skipped"])
            + ". Wenn das Kernpositionen sind: Depot unten zurücksetzen und bei "
            "verfügbaren Kursen neu anlegen."
        )

    # --- Vergleichschart ---
    if comp is not None and len(comp) >= 2:
        long = comp.reset_index().melt(id_vars="Datum", var_name="Portfolio", value_name="Index")
        fig = px.line(long, x="Datum", y="Index", color="Portfolio",
                      color_discrete_map={"Echt": "#8894a3", "KI": "#23c55e"},
                      labels={"Index": "Index (Start = 100)"})
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          legend=dict(orientation="h", y=1.05), autosize=True)
        st.plotly_chart(fig, width="stretch", config={"responsive": True}, key=f"shadow_compare_{scope}")
    else:
        st.caption("📉 Der Vergleichschart entsteht ab dem zweiten Tag — die Verlaufsreihe "
                   "wächst mit jedem Öffnen des Dashboards.")

    st.divider()

    # --- Neue Anweisungen einholen ---
    strat_model = st.session_state.get("senior_model", config.DEFAULT_SENIOR_MODEL)
    est = strategist.estimate_cost(strat_model)
    col_run, col_info = st.columns([1, 2])
    with col_run:
        run_clicked = st.button("🧠 Neue KI-Anweisungen einholen", type="primary",
                                disabled=not config.anthropic_api_key(),
                                width="stretch", key=f"run_{scope}")
    with col_info:
        st.caption(f"Der **Portfolio-Stratege** ({config.model_label(strat_model)}) analysiert "
                   f"das {name}-Depot, die Marktlage und seine bisherigen Calls und handelt "
                   f"direkt. Geschätzte Kosten ≈ ${est:.3f}. (Modell in der Sidebar änderbar.)")
        if not config.anthropic_api_key():
            st.warning("ANTHROPIC_API_KEY fehlt in der .env.")

    if run_clicked:
        status = st.status("Portfolio-Stratege arbeitet ...", expanded=True)
        result = strategist.run_strategy(scope, strat_model,
                                         progress_cb=lambda m: status.write(m))
        status.update(label="Fertig", state="complete", expanded=False)
        st.session_state[f"last_strategy_{scope}"] = result
        st.session_state["session_cost"] = (
            st.session_state.get("session_cost", 0.0) + result.get("total_cost_usd", 0.0)
        )
        st.rerun()

    _render_last_strategy(scope)

    st.divider()

    # --- Positionen + Changelog ---
    col_pos, col_log = st.columns(2)
    with col_pos:
        st.markdown(f"#### 🤖 {name}: Positionen")
        _render_shadow_positions(scope, shadow_vals, shadow_total)
    with col_log:
        st.markdown("#### 📜 Changelog (KI-Änderungen)")
        _render_changelog(scope)

    st.divider()
    _render_recommendation_history(scope)
    _render_reset(scope)


def _render_last_strategy(scope: str):
    result = st.session_state.get(f"last_strategy_{scope}")
    if not result:
        return
    if result.get("error"):
        cost = result.get("total_cost_usd", 0.0)
        msg = result["error"]
        if cost > 0:
            msg += f"\n\n💸 Trotz Fehler bereits verbraucht: ${cost:.4f} (siehe Sitzungs-Kosten in der Sidebar)."
        st.error(msg)
        return
    if result.get("marktausblick"):
        st.markdown("##### 🌍 Marktausblick")
        st.info(result["marktausblick"])
    if result.get("gesamtkommentar"):
        st.markdown(f"_{result['gesamtkommentar']}_")
    if result.get("cash_hinweis"):
        st.markdown("##### 💶 Hinweis zu deinem echten Depot (Cash)")
        st.info(result["cash_hinweis"])
        st.caption("Bezieht sich auf dein echtes Bankguthaben – die KI-Trades unten "
                   "betreffen weiterhin nur das virtuelle Schatten-Depot.")

    empf = result.get("empfehlungen", [])
    outcomes = result.get("outcomes", [])
    st.markdown(f"##### 📋 Empfehlungen dieses Laufs ({len(empf)})")
    if not empf:
        st.caption("Der Stratege hat diesmal keine Trades empfohlen.")
    for i, e in enumerate(empf):
        label = _AKTION_ICON.get((e.get("aktion") or "").lower(), e.get("aktion", ""))
        target = f" → {e['ziel_symbol']}" if e.get("ziel_symbol") else ""
        anteil = f" · {e.get('anteil_pct', 0):.0f}%" if e.get("anteil_pct") is not None else ""
        outcome = outcomes[i]["notiz"] if i < len(outcomes) else ""
        with st.container(border=True):
            st.markdown(f"**{label}: {e.get('symbol', '')}{target}**{anteil}")
            st.caption(e.get("begruendung", ""))
            if outcome:
                st.caption(f"↳ _{outcome}_")
    st.caption(f"💰 Kosten dieses Laufs: ${result.get('total_cost_usd', 0):.4f}")


def _render_shadow_positions(scope: str, vals: list[dict], total: float):
    if not vals:
        st.caption("Keine Positionen.")
        return
    rows = []
    for v in vals:
        weight = (v["value_eur"] or 0) / total * 100 if total else 0
        rows.append({
            "Symbol": v["symbol"],
            "Typ": v["asset_type"],
            "Menge": round(v["quantity"], 6),
            "Wert (€)": round(v["value_eur"], 2) if v["value_eur"] is not None else None,
            "Anteil (%)": round(weight, 1),
            "Status": v["error"] or "OK",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    items = [{"Symbol": v["symbol"], "Wert": v["value_eur"]}
             for v in vals if v["value_eur"]]
    components.render_allocation_pie(items, "Symbol", "Wert", "Verteilung",
                                     key=f"pie_shadow_{scope}")


def _render_changelog(scope: str):
    log = shadow.db.list_shadow_log(scope, 200)
    if not log:
        st.caption("Noch keine Änderungen — hole dir oben die ersten KI-Anweisungen.")
        return
    for entry in log:
        ts = entry["created_at"][:16].replace("T", " ")
        aktion = entry["aktion"]
        if aktion == "umschichten":
            head = (f"🔄 **{entry['von_symbol']} → {entry['nach_symbol']}** · "
                    f"{entry['wert_eur']:,.2f} €")
        elif aktion == "verkaufen":
            head = f"🔴 **{entry['von_symbol']} verkauft** · {entry['wert_eur']:,.2f} €"
        elif aktion == "kaufen":
            head = f"🟢 **{entry['nach_symbol']} gekauft** · {entry['wert_eur']:,.2f} €"
        else:
            head = f"⚪ **{entry['von_symbol']} gehalten**"
        st.markdown(head)
        st.caption(f"{ts} · {entry.get('notiz', '')}")


def _render_recommendation_history(scope: str):
    recs = shadow.db.list_recommendations(scope, 200)
    if not recs:
        return
    with st.expander(f"🗂️ Alle bisherigen Empfehlungen ({len(recs)})"):
        rows = []
        for r in recs:
            perf = ""
            if r["kurs_symbol_eur"] and r["symbol"]:
                now = shadow.price_eur(r["symbol"], r["asset_type"] or scope)
                if now:
                    perf = f"{(now / r['kurs_symbol_eur'] - 1) * 100:+.1f}%"
            rows.append({
                "Datum": r["created_at"][:10],
                "Aktion": r["aktion"],
                "Symbol": r["symbol"] or "",
                "Ziel": r["ziel_symbol"] or "",
                "Anteil %": round(r["anteil_pct"], 0) if r["anteil_pct"] is not None else "",
                "Angewendet": "✅" if r["angewendet"] else "—",
                "Seither": perf,
                "Begründung": (r["begruendung"] or "")[:80],
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_reset(scope: str):
    name = shadow.SCOPES[scope]
    with st.expander(f"⚠️ {name}-Depot zurücksetzen"):
        st.caption(f"Löscht NUR das {name}-Experiment (Positionen, Changelog, Empfehlungen, "
                   "Verlauf). Das andere KI-Depot bleibt unberührt.")
        confirm = st.checkbox(f"Ja, {name}-Depot unwiderruflich zurücksetzen",
                              key=f"reset_confirm_{scope}")
        if st.button("🗑️ Zurücksetzen", disabled=not confirm, key=f"reset_btn_{scope}"):
            shadow.reset(scope)
            st.session_state.pop(f"last_strategy_{scope}", None)
            st.success(f"{name}-Depot zurückgesetzt.")
            st.rerun()
