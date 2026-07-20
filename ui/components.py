"""Wiederverwendbare Streamlit-Bausteine: Gauge, Charts, Agenten-Reports."""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from agents.specialists import SPECIALISTS


def _set_native_theme(dark: bool) -> bool:
    """Streamlits eingebautes Theme pro Sitzung mitschalten.

    Nur so werden Canvas-Tabellen (per CSS nicht erreichbar), Plotly-Charts,
    Eingabefelder, Tabs, Alerts usw. wirklich dunkel/hell. Nutzt eine interne
    Streamlit-API - Option gilt prozessweit, was bei dieser lokalen
    Single-User-App unkritisch ist; bei Fehlern greift weiter das CSS-Overlay.
    Gibt True zurück, wenn sich etwas geändert hat (dann ist ein Rerun nötig,
    weil der Browser die Theme-Config erst beim nächsten Seitenaufbau erhält).
    """
    opts = {
        "theme.base": "dark" if dark else "light",
        "theme.backgroundColor": "#0b1120" if dark else "#f7f9fc",
        "theme.secondaryBackgroundColor": "#111827" if dark else "#ffffff",
        "theme.textColor": "#e5edf9" if dark else "#182235",
    }
    changed = False
    try:
        for key, value in opts.items():
            if st._config.get_option(key) != value:
                st._config.set_option(key, value)
                changed = True
    except Exception:
        return False
    return changed


def _text_color() -> str:
    """Primäre Textfarbe passend zum aktiven Theme (für Plotly-Elemente)."""
    return "#e5edf9" if st.session_state.get("ui_theme", "dark") == "dark" else "#182235"


def apply_theme(theme: str = "dark"):
    """Lokale Hell-/Dunkel-Gestaltung: natives Streamlit-Theme + CSS-Overlay."""
    dark = theme == "dark"
    if _set_native_theme(dark):
        st.rerun()   # einmalig: Browser bekommt die Theme-Config erst beim Neuaufbau
    palette = """
      [data-testid="stAppViewContainer"], [data-testid="stHeader"] {background: #0b1120; color: #e5edf9;}
      [data-testid="stSidebar"] {background: #101827; color: #e5edf9;}
      [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {color: #e5edf9;}
      [data-testid="stMetric"] {background: linear-gradient(145deg, #172033, #111827); border-color: #26344d;}
      [data-testid="stMetricValue"] {color: #f8fafc;}
      [data-testid="stMetricLabel"] {color: #aab7cc;}
      .pp-card {background: #111827; border-color: #26344d;}
      .pp-subtle {color: #aab7cc;}
      [data-testid="stDataFrame"], [data-testid="stExpander"] {border-color: #26344d;}
      [data-testid="stExpander"] summary, [data-testid="stExpander"] details {background: #111827; color: #e5edf9;}
      [data-testid="stForm"] {border-color: #26344d;}
      [data-testid="stCaptionContainer"], [data-testid="stWidgetLabel"] p {color: #aab7cc;}
      hr {border-color: #26344d;}
      button[kind="primary"] {background-color: #e5edf9; color: #0b1120; border-color: #e5edf9;}
      button[kind="primary"]:hover {background-color: #c7d2e3; border-color: #c7d2e3; color: #0b1120;}
      button[kind="primary"]:focus-visible {outline: 2px solid #6ee7b7; outline-offset: 2px;}
      button[kind="primary"]:disabled {background-color: #26344d; border-color: #26344d; color: #aab7cc; opacity: .6;}
    """ if dark else """
      [data-testid="stAppViewContainer"], [data-testid="stHeader"] {background: #f7f9fc; color: #182235;}
      [data-testid="stSidebar"] {background: #ffffff; color: #182235;}
      [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {color: #182235;}
      [data-testid="stMetric"] {background: linear-gradient(145deg, #ffffff, #f5f8fc); border-color: #dbe4f0;}
      [data-testid="stMetricValue"] {color: #182235;}
      [data-testid="stMetricLabel"] {color: #64748b;}
      .pp-card {background: #ffffff; border-color: #dbe4f0;}
      .pp-subtle {color: #64748b;}
      [data-testid="stDataFrame"], [data-testid="stExpander"] {border-color: #dbe4f0;}
      [data-testid="stExpander"] summary, [data-testid="stExpander"] details {background: #ffffff; color: #182235;}
      [data-testid="stForm"] {border-color: #dbe4f0;}
      [data-testid="stCaptionContainer"], [data-testid="stWidgetLabel"] p {color: #64748b;}
      hr {border-color: #dbe4f0;}
      button[kind="primary"] {background-color: #182235; color: #ffffff; border-color: #182235;}
      button[kind="primary"]:hover {background-color: #2b3a52; border-color: #2b3a52; color: #ffffff;}
      button[kind="primary"]:focus-visible {outline: 2px solid #6ee7b7; outline-offset: 2px;}
      button[kind="primary"]:disabled {background-color: #dbe4f0; border-color: #dbe4f0; color: #64748b; opacity: .6;}
    """
    st.markdown("""
    <style>
      /* padding-top muss den fixierten stHeader (60px) überragen, sonst wird das
         allererste Element der Seite (.pp-eyebrow) darunter verdeckt/abgeschnitten */
      .block-container {max-width: 1420px; padding-top: 5rem; padding-bottom: 3rem;}
      [data-testid="stMetric"] {border: 1px solid; border-radius: 14px; padding: 1rem 1.1rem;}
      [data-testid="stMetricLabel"] {font-size: .88rem;}
      [data-testid="stMetricValue"] {font-weight: 650;}
      div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
      .pp-eyebrow {color: #6ee7b7; font-size: .78rem; font-weight: 700;
        letter-spacing: .08em; text-transform: uppercase; margin-bottom: .25rem;}
      .pp-card {border: 1px solid; border-radius: 14px;
        padding: 1rem 1.1rem; height: 100%;}
      .pp-positive {color: #4ade80; font-weight: 650;}
      .pp-negative {color: #fb7185; font-weight: 650;}
      [data-testid="stSidebar"] {border-right: 1px solid;}
    """ + palette + """
    </style>
    """, unsafe_allow_html=True)


def page_header(eyebrow: str, title: str, subtitle: str | None = None):
    """Konsistente Titelhierarchie für die Hauptseiten."""
    st.markdown(f'<div class="pp-eyebrow">{eyebrow}</div>', unsafe_allow_html=True)
    st.title(title)
    if subtitle:
        st.markdown(f'<div class="pp-subtle">{subtitle}</div>', unsafe_allow_html=True)


def render_gauge(score: float, title: str = "Gesamt-Rating", key: str | None = None):
    text = _text_color()
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"font": {"color": text}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": text,
                     "tickfont": {"color": text}},
            "steps": [
                {"range": [0, 40], "color": "#ff4b4b"},
                {"range": [40, 70], "color": "#ffa500"},
                {"range": [70, 100], "color": "#23c55e"},
            ],
            "bar": {"color": text},
        },
        title={"text": title, "font": {"color": text}},
    ))
    fig.update_layout(height=250, margin=dict(t=50, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True}, key=key)


def render_price_chart(df: pd.DataFrame, fibs: dict | None = None, height: int = 420,
                       key: str | None = None):
    """Kurs-Chart mit Bollinger, MA50/200 und optionalen Fibonacci-Linien."""
    fig = go.Figure()
    if "Upper_Band" in df:
        fig.add_trace(go.Scatter(x=df.index, y=df["Upper_Band"], name="Bollinger oben",
                                 line=dict(color="rgba(173,216,230,0.5)")))
        fig.add_trace(go.Scatter(x=df.index, y=df["Lower_Band"], name="Bollinger unten",
                                 line=dict(color="rgba(173,216,230,0.5)"), fill="tonexty"))
    fig.add_trace(go.Scatter(x=df.index, y=df["Close"], name="Kurs", line=dict(width=2)))
    if "MA50" in df:
        fig.add_trace(go.Scatter(x=df.index, y=df["MA50"], name="MA50",
                                 line=dict(dash="dot", color="deepskyblue")))
    if "MA200" in df:
        fig.add_trace(go.Scatter(x=df.index, y=df["MA200"], name="MA200",
                                 line=dict(dash="dot", color="orange")))
    if fibs:
        for name, value in fibs.items():
            fig.add_hline(y=value, line_dash="dash", line_color="gray",
                          opacity=0.3, annotation_text=name)
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=1, xanchor="right"),
                      autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True}, key=key)


def render_allocation_pie(items: list[dict], names: str, values: str, title: str,
                          key: str | None = None):
    df = pd.DataFrame(items)
    if df.empty or df[values].sum() <= 0:
        st.caption("Keine Daten für das Diagramm.")
        return
    fig = px.pie(df, names=names, values=values, title=title, hole=0.62,
                 color_discrete_sequence=["#34d399", "#60a5fa", "#a78bfa", "#fbbf24", "#fb7185", "#94a3b8"])
    fig.update_traces(textposition="inside", textinfo="percent", hovertemplate="%{label}<br>%{value:,.2f} €<br>%{percent}<extra></extra>")
    fig.update_layout(legend=dict(orientation="h", y=-0.12), height=330,
                      margin=dict(l=10, r=10, t=45, b=35), autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True}, key=key)


def render_allocation_bars(items: list[dict], title: str, key: str | None = None):
    """Horizontale Gewichtung für viele Positionen – lesbarer als eine große Torte."""
    df = pd.DataFrame(items)
    if df.empty or df["Wert"].sum() <= 0:
        st.caption("Keine Daten für die Gewichtung.")
        return
    df = df.sort_values("Wert", ascending=True).tail(8)
    fig = px.bar(df, x="Wert", y="Name", orientation="h", title=title,
                 text="Wert", color_discrete_sequence=["#60a5fa"])
    fig.update_traces(texttemplate="%{text:,.0f} €", textposition="outside",
                      hovertemplate="%{y}<br>%{x:,.2f} €<extra></extra>")
    fig.update_layout(height=330, margin=dict(l=0, r=30, t=45, b=10),
                      xaxis_title=None, yaxis_title=None, showlegend=False, autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True}, key=key)


def render_usage(usage: dict):
    if not usage:
        return
    st.caption(
        f"🔢 {usage.get('input', 0)} In / {usage.get('output', 0)} Out Token "
        f"(Cache: {usage.get('cache_read', 0)}) · "
        f"≈ {usage.get('cost_usd', 0) * 100:.2f} ct (${usage.get('cost_usd', 0):.4f})"
    )


def render_specialist_report(key: str, report: dict):
    spec = SPECIALISTS.get(key, {"name": key, "emoji": "🤖"})
    header = f"{spec['emoji']} {report.get('agent', spec['name'])}"
    if report.get("error"):
        with st.expander(f"{header} — ⚠️ Ausfall"):
            st.error(report["error"])
        return
    urteil_icon = {"positiv": "🟢", "neutral": "🟡", "negativ": "🔴"}.get(report.get("urteil", ""), "⚪")
    with st.expander(f"{header} — {report.get('score', '?')}/100 {urteil_icon}"):
        zusammenfassung = (report.get("zusammenfassung") or "").strip()
        punkte = [p for p in report.get("punkte", []) if str(p).strip()]
        if zusammenfassung:
            st.write(zusammenfassung)
        for p in punkte:
            st.markdown(f"- {p}")
        if not zusammenfassung and not punkte:
            st.warning("Dieser Agent hat einen Score, aber keinen Text geliefert "
                       "(vermutlich abgeschnittene Antwort). Analyse am besten erneut starten.")
        render_usage(report.get("usage", {}))


def render_senior_report(result: dict, key_prefix: str = ""):
    senior = result.get("senior")
    if result.get("senior_error") or not senior:
        st.error(result.get("senior_error") or "Senior-Analyse fehlgeschlagen.")
        return
    rec = senior.get("empfehlung", "?")
    rec_color = {"Kaufen": "🟢", "Aufstocken": "🟢", "Halten": "🟡",
                 "Reduzieren": "🟠", "Verkaufen": "🔴"}.get(rec, "⚪")
    col1, col2 = st.columns([1, 2])
    with col1:
        render_gauge(senior.get("gesamtscore", 50), "Senior-Rating",
                     key=f"gauge_{key_prefix}")
        st.markdown(f"### {rec_color} Empfehlung: **{rec}**")
    with col2:
        st.markdown("#### 🧑‍💼 Einschätzung des Senior Asset Managers")
        st.info(senior.get("begruendung", ""))
        if senior.get("allokationshinweis"):
            st.markdown(f"**Allokation:** {senior['allokationshinweis']}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Chancen**")
        for c in senior.get("chancen", []):
            st.markdown(f"- ✅ {c}")
    with c2:
        st.markdown("**Risiken**")
        for r in senior.get("risiken", []):
            st.markdown(f"- ⚠️ {r}")
    # Nur beim Portfolio-Review vorhanden (Einzelwert-Analyse liefert das Feld nicht).
    cash_vorschlaege = senior.get("cash_vorschlaege") or []
    if cash_vorschlaege:
        st.markdown("#### 💶 Cash einsetzen")
        st.caption("Vorschläge, freies Cash entlang deiner Zielallokation zu investieren "
                   "(keine Anlageberatung).")
        for v in cash_vorschlaege:
            betrag = v.get("betrag_eur")
            betrag_txt = f"{betrag:,.0f} €" if isinstance(betrag, (int, float)) else "—"
            st.markdown(f"- **{betrag_txt} → {v.get('symbol', '?')}**: {v.get('begruendung', '')}")
    render_usage(result.get("senior_usage", {}))


def render_analysis_result(result: dict, key_prefix: str = ""):
    """Kompletter Analyse-Report: Senior + alle Spezialisten.

    key_prefix macht die Plotly-Charts eindeutig, wenn mehrere Reports auf
    derselben Seite stehen (z.B. Live-Ergebnis + Historie).
    """
    if result.get("error"):
        st.error(result["error"])
        return
    prefix = key_prefix or f"{result.get('target', 'x')}_{result.get('mode', '')}"
    render_senior_report(result, key_prefix=prefix)
    st.divider()
    st.markdown("#### Berichte der Spezialisten")
    for key, report in result.get("specialists", {}).items():
        render_specialist_report(key, report)
    st.caption(f"💰 Gesamtkosten dieser Analyse: ${result.get('total_cost_usd', 0):.4f}")
