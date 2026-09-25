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
      /* primaryFormSubmit (st.form_submit_button(type="primary")) faellt unter
         einem ANDEREN kind-Wert als primary - ohne diese Ergaenzung blieb es
         bei Streamlits ungestyltem Default-Rot (#ff4b4b), identisch mit Data
         Red/Notbremse-Rot, fuer voellig harmlose Aktionen wie "Speichern". */
      button[kind="primary"], button[kind="primaryFormSubmit"] {background-color: #e5edf9; color: #0b1120; border-color: #e5edf9;}
      button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover {background-color: #c7d2e3; border-color: #c7d2e3; color: #0b1120;}
      button[kind="primary"]:focus-visible, button[kind="primaryFormSubmit"]:focus-visible {outline: 2px solid #6ee7b7; outline-offset: 2px;}
      button[kind="primary"]:disabled, button[kind="primaryFormSubmit"]:disabled {background-color: #26344d; border-color: #26344d; color: #aab7cc; opacity: .6;}
      /* st.segmented_control markiert die aktive Option standardmaessig in
         Streamlits Default-Rot (#ff4b4b) - identisch mit Data Red/Notbremse-
         Rot (DESIGN.md). Eine reine Zeitraum-Auswahl ist keine Warnung/
         Verlust-Anzeige, deshalb hier auf die neutrale Text-Primary-Farbe
         umgestellt statt eine zweite Bedeutung fuer Rot einzufuehren. */
      [data-testid="stBaseButton-segmented_controlActive"] {
        border-color: #e5edf9 !important; color: #e5edf9 !important;
        background-color: rgba(229, 237, 249, 0.12) !important;
      }
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
      button[kind="primary"], button[kind="primaryFormSubmit"] {background-color: #182235; color: #ffffff; border-color: #182235;}
      button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover {background-color: #2b3a52; border-color: #2b3a52; color: #ffffff;}
      button[kind="primary"]:focus-visible, button[kind="primaryFormSubmit"]:focus-visible {outline: 2px solid #6ee7b7; outline-offset: 2px;}
      button[kind="primary"]:disabled, button[kind="primaryFormSubmit"]:disabled {background-color: #dbe4f0; border-color: #dbe4f0; color: #64748b; opacity: .6;}
      [data-testid="stBaseButton-segmented_controlActive"] {
        border-color: #182235 !important; color: #182235 !important;
        background-color: rgba(24, 34, 53, 0.08) !important;
      }
    """
    st.markdown("""
    <style>
      /* padding-top muss den fixierten stHeader (60px) überragen, sonst wird das
         allererste Element der Seite (.pp-eyebrow) darunter verdeckt/abgeschnitten */
      .block-container {max-width: 1420px; padding-top: 5rem; padding-bottom: 3rem;}
      /* Lange zusammengesetzte deutsche Seitentitel ohne Bindestrich (z.B.
         "Einzelwertanalyse") brechen auf schmalen Viewports sonst mitten im
         Wort um (ein einzelner Buchstabe faellt in die naechste Zeile) -
         hyphens: auto erlaubt stattdessen einen sprachlich korrekten
         Trennstrich. Braucht ein gesetztes lang="de" (siehe
         _bump_resize_on_tab_switch()), sonst ignorieren Browser die Regel. */
      h1 {hyphens: auto; -webkit-hyphens: auto; overflow-wrap: break-word;}
      [data-testid="stMetric"] {border: 1px solid; border-radius: 14px; padding: 1rem 1.1rem;}
      [data-testid="stMetricLabel"] {font-size: .88rem;}
      [data-testid="stMetricValue"] {font-weight: 650;}
      /* Dashboard: Gesamtvermögen bewusst groesser als jede andere Kennzahl -
         Hierarchie ueber Groesse/Gewicht statt einer neuen Akzentfarbe
         (DESIGN.md Typography-Prinzip), volle Breite statt 1-von-4-Spalten. */
      div[class*="st-key-dash_total_wealth"] [data-testid="stMetricLabel"] {font-size: 1.05rem;}
      div[class*="st-key-dash_total_wealth"] [data-testid="stMetricValue"] {font-size: 2.75rem;}
      /* Der 5-Buttons-Zeitraum-Regler bricht auf schmalen Viewports auf
         4+1 um - ohne dies bleibt der letzte Button ("1 Jahr") winzig und
         allein in der zweiten Zeile. flex-grow verteilt die Restbreite
         JE ZEILE (flex-wrap erzeugt mehrere Flex-Linien), die zweite Zeile
         bekommt dadurch automatisch volle Breite statt einer Restluecke. */
      div[class*="st-key-dashboard_delta_period"] [data-testid^="stBaseButton-segmented_control"] {
        flex: 1 1 auto;
      }
      div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
      .pp-eyebrow {color: #6ee7b7; font-size: .78rem; font-weight: 700;
        letter-spacing: .08em; text-transform: uppercase; margin-bottom: .25rem;}
      .pp-card {border: 1px solid; border-radius: 14px;
        padding: 1rem 1.1rem; height: 100%;}
      .pp-positive {color: #4ade80; font-weight: 650;}
      .pp-negative {color: #fb7185; font-weight: 650;}
      /* Der Abstand nach unten muss die schwebende Dataframe-Toolbar (Auge/
         Download/Suche/Vollbild, von Streamlit automatisch ueber JEDER
         st.dataframe eingeblendet) frei lassen - sie sitzt einige Pixel
         ueber dem eigentlichen Tabellenrand und ueberlappt sonst den Text. */
      .pp-scroll-hint {display: none; font-size: .85rem; margin: 0 0 1.75rem;}
      [data-testid="stSidebar"] {border-right: 1px solid;}
    """ + palette + """
      /* Notbremse: einzige Stelle, an der Data Red als Flaeche statt als
         G/V-Text auftritt - siehe DESIGN.md, Named Rule "Notbremse-Rot".
         Eigene Regel statt button[kind="primary"], damit der Kill-Switch-
         Button bewusst secondary bleibt (keine Verwechslung mit anderen
         primaeren Aktionen) und trotzdem eindeutig als Notbremse auffaellt. */
      div[class*="st-key-bot_kill_action"] button {
        background-color: #ff4b4b; border-color: #ff4b4b; color: #fff;
      }
      div[class*="st-key-bot_kill_action"] button:hover {
        background-color: #e23c3c; border-color: #e23c3c; color: #fff;
      }
      div[class*="st-key-bot_kill_action"] button:focus-visible {
        outline: 2px solid #6ee7b7; outline-offset: 2px;
      }
      /* Mobile (<=640px, Streamlits eigener Stapel-Schwellenwert fuer st.columns):
         nur Feinschliff dort, wo natives Stapeln allein nicht reicht - keine
         Aenderung an Farben/Radius/Card-Regeln, die gelten auf jeder Breite. */
      @media (max-width: 640px) {
        .block-container {padding-left: 1rem; padding-right: 1rem; padding-bottom: 1.5rem;}
        [data-testid="stMetric"] {padding: .75rem .85rem;}
        /* horizontale Radios (Konto-/Zeitraum-Filter) brechen sonst nicht um
           und laufen bei mehr als ~3 Optionen ueber den Bildschirmrand hinaus */
        [data-testid="stRadio"] div[role="radiogroup"] {flex-wrap: wrap;}
        /* Fallback gegen das Plotly-"responsive"-Clipping: lieber ein
           Scrollbalken als ein still abgeschnittener Chart-Bereich */
        [data-testid="stPlotlyChart"] {overflow-x: auto;}
        /* Cash-Schnell-Buttons (views/cash.py): Streamlit stapelt Spalten
           unterhalb dieses Breakpoints IMMER auf 100% Breite, egal wie
           viele es sind - hier gezielt aufgehoben, damit die 2 markierten
           4er-Reihen echte 4-Spalten-Raster bleiben statt zu 8 einzeln
           gestapelten Vollbreite-Buttons zu werden. */
        div[class*="st-key-cash_quick_row"] [data-testid="stColumn"] {
          min-width: 0 !important; flex: 1 1 0 !important;
        }
        /* st.tabs mit 4+ Reitern (z.B. Einstellungen: Zielallokation/API-Keys/
           KI & Kosten/Daten/Updates) ueberlaufen auf 375px nur knapp (~12px) -
           der 16px-Standardabstand zwischen Reitern allein reicht schon, um
           den letzten Reiter ("Updates") ganz aus dem sichtbaren Bereich zu
           schieben. Reiter sind zwar bereits horizontal wischbar (Streamlit
           setzt overflow-x:scroll selbst), aber ohne jeden visuellen Hinweis -
           der schmalere Abstand vermeidet das Ueberlaufen meist ganz, statt
           nur auf "entdeckt der Nutzer das Wischen" zu hoffen. */
        [data-baseweb="tab-list"] {gap: 8px !important;}
        [data-testid="stTab"] {font-size: .85rem;}
        .pp-scroll-hint {display: block;}
      }
    </style>
    """, unsafe_allow_html=True)
    _bump_resize_on_tab_switch()


def _bump_resize_on_tab_switch():
    """Plotly-Charts in st.tabs bleiben sonst auf ihrer Mount-Breite haengen.

    st.tabs() haelt beide Panels im DOM und blendet das inaktive nur per
    display:none aus. Ein Plotly-Chart, der WAEHREND display:none gemountet
    wird, bekommt keine gueltige Container-Breite und faellt auf Plotlys
    700px-Default zurueck; da display:none->block kein 'resize'-Event
    ausloest, bleibt "responsive: True" wirkungslos. Betrifft jede
    Bildschirmbreite, faellt aber auf schmalen Handy-Viewports (Container
    z.B. 343px) am staerksten auf - der Chart ragt dann weit ueber den Rand
    hinaus. Fix: ein <script> im Streamlit-eigenen iframe (components.v1.html
    ist die einzige Stelle, an der st.markdown() tatsaechlich JS ausfuehrt)
    horcht im Eltern-Dokument auf Tab-Klicks und stoesst danach ein echtes
    'resize'-Event an, das Plotlys ResizeObserver zur Neuberechnung bewegt.
    Guard per Flag auf window.parent, da apply_theme() bei jedem Rerun neu
    aufgerufen wird und sonst bei jedem Klick doppelte/dreifache Handler
    anhäufen wuerden.

    Setzt hier auch document.lang = 'de' (Streamlit liefert kein lang-Attribut
    auf dem <html>-Tag) - ohne das ignorieren Browser die "hyphens: auto"-Regel
    auf h1 (siehe apply_theme()'s CSS) fuer lange zusammengesetzte Seitentitel."""
    st.components.v1.html("""
    <script>
      window.parent.document.documentElement.lang = 'de';
      if (!window.parent.__ppTabResizeBound) {
        window.parent.__ppTabResizeBound = true;
        window.parent.document.addEventListener('click', function(e) {
          if (e.target.closest('[role="tab"]')) {
            setTimeout(function() { window.parent.dispatchEvent(new Event('resize')); }, 60);
            setTimeout(function() { window.parent.dispatchEvent(new Event('resize')); }, 300);
          }
        }, true);
      }
    </script>
    """, height=0)


def page_header(eyebrow: str, title: str, subtitle: str | None = None):
    """Konsistente Titelhierarchie für die Hauptseiten."""
    st.markdown(f'<div class="pp-eyebrow">{eyebrow}</div>', unsafe_allow_html=True)
    st.title(title)
    if subtitle:
        st.markdown(f'<div class="pp-subtle">{subtitle}</div>', unsafe_allow_html=True)


def render_mobile_scroll_hint():
    """Hinweis ueber einer spaltenreichen st.dataframe, dass sie horizontal
    wischbar ist - NUR auf schmalen Viewports sichtbar (.pp-scroll-hint in
    apply_theme()). st.dataframe basiert auf einem Canvas-Grid (glide-data-
    grid): auf 375px zeigt es oft nur die ersten 2-3 Spalten, der Rest ist
    zwar wischbar, aber ohne jeden sichtbaren Hinweis darauf - Nutzer haetten
    sonst keinen Grund anzunehmen, dass ueberhaupt mehr Spalten existieren.
    Auf Desktop, wo die Tabelle ohnehin komplett sichtbar ist, waere derselbe
    Hinweis nur Ablenkung ohne Zweck (siehe PRODUCT.md "Daten vor
    Dekoration") - deshalb per CSS defaultmaessig ausgeblendet."""
    st.markdown('<p class="pp-scroll-hint pp-subtle">→ Tabelle nach rechts wischen für weitere Spalten.</p>',
               unsafe_allow_html=True)


def gauge_color(score: float, invert: bool = False) -> str:
    """Farbe passend zu den Ampel-Stufen von render_gauge() - für Text, der
    neben/über einem Gauge dieselbe Farbsemantik tragen soll (z.B. die
    Klartext-Einordnung "Neutral" neben der Markt-Temperatur)."""
    if invert:
        if score < 30:
            return "#23c55e"
        if score < 60:
            return "#ffa500"
        return "#ff4b4b"
    if score < 40:
        return "#ff4b4b"
    if score < 70:
        return "#ffa500"
    return "#23c55e"


def render_gauge(score: float, title: str = "Gesamt-Rating", key: str | None = None,
                 invert: bool = False, height: int = 250):
    """invert=True dreht die Farbsemantik um (hoch = rot statt grün) - für
    Kennzahlen, bei denen ein hoher Wert eine Warnung ist (z.B. Markt-
    Überhitzung/Gier), nicht eine Kauf-Chance.

    height < 200 rendert eine kompakte Variante (kleinere Schrift, engere
    Ränder) - für mehrere Barometer nebeneinander (z.B. Indikator-Aufschlüsselung)."""
    text = _text_color()
    compact = height < 200
    steps = [
        {"range": [0, 40], "color": "#ff4b4b"},
        {"range": [40, 70], "color": "#ffa500"},
        {"range": [70, 100], "color": "#23c55e"},
    ]
    if invert:
        steps = [
            {"range": [0, 30], "color": "#23c55e"},
            {"range": [30, 60], "color": "#ffa500"},
            {"range": [60, 100], "color": "#ff4b4b"},
        ]
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"font": {"color": text, "size": 20 if compact else 40}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": text,
                     "tickfont": {"color": text, "size": 9 if compact else 12}},
            "steps": steps,
            "bar": {"color": text},
        },
        title={"text": title, "font": {"color": text, "size": 12 if compact else 17}},
    ))
    fig.update_layout(height=height,
                      margin=dict(t=25 if compact else 50, b=0, l=5 if compact else 20, r=5 if compact else 20),
                      paper_bgcolor="rgba(0,0,0,0)", autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


def _shade(hex_color: str, factor: float) -> str:
    """Hex-Farbe Richtung Weiß (factor > 0) oder Schwarz (factor < 0) verschieben,
    Betrag 0..1 - für abgestufte Helligkeiten EINER Grundfarbe, ohne neue
    Akzenttöne einzuführen (DESIGN.md: kein neuer Akzentton)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    if factor >= 0:
        r, g, b = (int(c + (255 - c) * factor) for c in (r, g, b))
    else:
        r, g, b = (int(c * (1 + factor)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def render_ladder_gauge(score: float, title: str, buy_thresholds: list[float],
                        sell_thresholds: list[float], key: str | None = None,
                        height: int = 200):
    """Barometer mit bis zu 6 nutzerdefinierten Stufen-Markierungen (3 Kauf/
    grün, 3 Verkauf/rot) statt der festen 3-Band-Skala von render_gauge().

    Baut aus den sortierten Schwellen 7 Farbbänder: 3 Kauf-Tiefenstufen (dunkel
    -> hell), eine neutrale Mittelzone, 3 Verkauf-Tiefenstufen (hell -> dunkel).
    Nutzt ausschließlich die bestehenden Palette-Töne (Grün/Rot/Chart-Slate),
    nur in abgestufter Helligkeit - siehe _shade()."""
    text = _text_color()
    compact = height < 200
    buy_sorted = sorted(buy_thresholds)
    sell_sorted = sorted(sell_thresholds)
    edges = [0.0] + buy_sorted + sell_sorted + [100.0]
    green, red, neutral = "#23c55e", "#ff4b4b", "#94a3b8"
    band_colors = [
        _shade(green, -0.35), green, _shade(green, 0.45),
        neutral,
        _shade(red, 0.45), red, _shade(red, -0.35),
    ]
    steps = [{"range": [edges[i], edges[i + 1]], "color": band_colors[i]} for i in range(7)]
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"font": {"color": text, "size": 20 if compact else 40}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": text,
                     "tickfont": {"color": text, "size": 9 if compact else 12}},
            "steps": steps,
            "bar": {"color": text},
        },
        title={"text": title, "font": {"color": text, "size": 12 if compact else 17}},
    ))
    fig.update_layout(height=height,
                      margin=dict(t=25 if compact else 50, b=0, l=5 if compact else 20, r=5 if compact else 20),
                      paper_bgcolor="rgba(0,0,0,0)", autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


def render_bar_list(rows: list[dict], key: str | None = None, height: int | None = None):
    """Kompakte horizontale Balkenliste für mehrere 0-100-Kennzahlen, gestapelt
    UNTEREINANDER statt eine Reihe kleiner render_gauge()-Halbkreise NEBEN-
    einander - deren Werte waren auf einen Blick kaum ablesbar (Nutzer-
    Feedback). Feste Skala 0-100 (nicht auto-skaliert wie render_allocation_bars)
    macht die Balkenlängen zwischen den Zeilen direkt vergleichbar.

    rows: [{"label": str, "score": float, "invert": bool, "horizon": str}, ...] -
    Farbe je Balken über dieselbe Ampel-Logik wie render_gauge() (gauge_color()).
    `horizon` (optional) landet nur im Hover-Tooltip, nicht im sichtbaren Label -
    das Label wird vom Aufrufer meist schon auf den Kurztitel gekürzt (Klammer-
    zusatz abgeschnitten), der Zeithorizont bleibt darüber trotzdem auffindbar."""
    text = _text_color()
    labels = [r["label"] for r in rows]
    scores = [r["score"] for r in rows]
    colors = [gauge_color(r["score"], invert=r.get("invert", False)) for r in rows]
    customdata = [r.get("horizon", "") for r in rows]
    fig = go.Figure(go.Bar(
        x=scores, y=labels, orientation="h",
        marker=dict(color=colors),
        text=[f"{s:.0f}" for s in scores], textposition="outside", cliponaxis=False,
        textfont=dict(color=text),
        customdata=customdata,
        hovertemplate="%{y}: %{x:.0f}<br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        height=height or max(140, 34 * len(rows) + 20),
        margin=dict(t=10, b=10, l=10, r=35),
        xaxis=dict(range=[0, 100], showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(autorange="reversed", tickfont=dict(color=text)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False, autosize=True,
    )
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


_FUNDAMENTAL_FORMATTERS = {
    "marktkap_eur": lambda v: f"{v / 1e9:,.2f} Mrd. €", "marktkap": lambda v: f"{v / 1e9:,.2f} Mrd. €",
    "volumen_24h_eur": lambda v: f"{v / 1e9:,.2f} Mrd. €",
    "kurs_eur": lambda v: f"{v:,.4g} €", "ath_eur": lambda v: f"{v:,.4g} €",
    "analysten_kursziel": lambda v: f"{v:,.2f} €",
    "52w_hoch": lambda v: f"{v:,.2f} €", "52w_tief": lambda v: f"{v:,.2f} €",
    "dividendenrendite": lambda v: f"{v:.2f}%",      # yfinance liefert bereits Prozent-Skala (verifiziert)
    "gewinnmarge": lambda v: f"{v * 100:.1f}%",        # yfinance liefert einen Bruch (0.15 = 15%)
    "umsatzwachstum": lambda v: f"{v * 100:.1f}%",     # dito
    "kgv": lambda v: f"{v:.2f}", "kgv_forward": lambda v: f"{v:.2f}", "beta": lambda v: f"{v:.2f}",
    "umlauf_supply": lambda v: f"{v:,.0f}", "max_supply": lambda v: f"{v:,.0f}",
}


def format_fundamental(key: str, value) -> str:
    """Feldname-bewusste Formatierung für Fundamentaldaten (Krypto: data/crypto.py
    get_market_data(), Aktien: data/stocks.py get_fundamentals()) - eine pauschale
    f"{v:,.2f}" für ALLE Werte zeigte Marktkap. als Rohzahl statt Mrd., Dividenden-
    rendite als 0.02 statt 2%, Kursziele ohne Währung. Unbekannte Felder fallen auf
    die alte pauschale Formatierung zurück, brechen also nicht."""
    if key.endswith("_pct"):    # Krypto *_pct-Felder sind bereits Prozent (CoinGecko-Konvention)
        return f"{value:.1f}%"
    fmt = _FUNDAMENTAL_FORMATTERS.get(key)
    if fmt:
        try:
            return fmt(value)
        except (TypeError, ZeroDivisionError):
            pass
    return f"{value:,.2f}" if isinstance(value, float) else str(value)


def render_datenstand(coverage_pct: float, sources: str, note: str = "", updated: str | None = None):
    """Konsolidierte Datenstand-Zeile (Quelle + Abdeckung) für Analysen, die aus
    mehreren freien Datenquellen rechnen - schafft Vertrauen, dass sichtbar
    bleibt, was gerade tatsächlich eingeflossen ist, statt es über mehrere
    verstreute Captions zu erraten.

    `updated`: echter Stand, wenn bekannt (z.B. das jüngste Datum einer
    On-Chain-Serie - die steht ohnehin schon in core.db.onchain_history, keine
    neue Infrastruktur nötig). Ohne Angabe "zuletzt aktualisiert: soeben" -
    das ist für die MEISTEN hier gezeigten Quellen richtig (jedes Rendern holt
    live neu), aber NICHT für alle: On-Chain-Metriken sind bis zu 24h alt
    (ttl_cache in data/onchain.py) - deshalb reicht cycle.cycle_score() dort
    inzwischen den echten Stand durch, statt pauschal "soeben" zu behaupten."""
    when = updated or "soeben"
    st.caption(f"📊 Datenstand: zuletzt aktualisiert {when} · Quellen: {sources} · "
              f"Abdeckung {coverage_pct:.0f}%" + (f" · {note}" if note else ""))


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
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


def render_score_history_chart(series: pd.Series, title: str, invert: bool = True,
                               height: int = 260, key: str | None = None):
    """Score-Verlauf (0-100) mit denselben 3 Farbbändern wie render_gauge()
    im Hintergrund - dieselben Schwellen/Hex-Werte, damit Barometer und
    Verlaufs-Chart optisch zusammengehören. 'Leicht gefärbt' (Nutzerwunsch) =
    niedrige opacity, damit die Linie darüber lesbar bleibt. yaxis_range fest
    auf [0,100], sonst würden die Bänder nicht die volle Chart-Höhe decken."""
    series = series.dropna()
    if series.empty or len(series) < 2:
        st.caption(f"Noch zu wenige Datenpunkte für den {title}-Verlauf.")
        return
    text = _text_color()
    bands = ([(0, 30, "#23c55e"), (30, 60, "#ffa500"), (60, 100, "#ff4b4b")] if invert
            else [(0, 40, "#ff4b4b"), (40, 70, "#ffa500"), (70, 100, "#23c55e")])
    fig = go.Figure()
    for y0, y1, color in bands:
        fig.add_hrect(y0=y0, y1=y1, fillcolor=color, opacity=0.15, line_width=0)
    fig.add_trace(go.Scatter(x=series.index, y=series.values, mode="lines",
                             line=dict(color=text, width=2), name=title,
                             hovertemplate="%{x|%d.%m.%Y}: %{y:.0f}<extra></extra>"))
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_range=[0, 100], showlegend=False, autosize=True)
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


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
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


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
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False}, key=key)


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
