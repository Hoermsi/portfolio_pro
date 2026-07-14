---
name: Portfolio Pro
description: Dunkles Terminal für Aktien, Krypto und Cash mit KI-gestützter Zweitmeinung
colors:
  bg-void: "#0b1120"
  bg-sidebar: "#101827"
  bg-surface: "#111827"
  surface-gradient-start: "#172033"
  surface-gradient-end: "#111827"
  border-subtle: "#26344d"
  text-primary: "#e5edf9"
  text-muted: "#aab7cc"
  accent-eyebrow: "#6ee7b7"
  data-positive: "#23c55e"
  data-negative: "#ff4b4b"
  data-warning: "#ffa500"
  metric-positive: "#4ade80"
  metric-negative: "#fb7185"
  chart-mint: "#34d399"
  chart-blue: "#60a5fa"
  chart-violet: "#a78bfa"
  chart-amber: "#fbbf24"
  chart-rose: "#fb7185"
  chart-slate: "#94a3b8"
  light-bg: "#f7f9fc"
  light-surface: "#ffffff"
  light-surface-alt: "#f5f8fc"
  light-border: "#dbe4f0"
  light-text-primary: "#182235"
  light-text-muted: "#64748b"
typography:
  headline:
    fontFamily: "Source Sans Pro, -apple-system, BlinkMacSystemFont, sans-serif"
    fontSize: "1.9rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "normal"
  title:
    fontFamily: "Source Sans Pro, -apple-system, BlinkMacSystemFont, sans-serif"
    fontSize: "1.1rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "Source Sans Pro, -apple-system, BlinkMacSystemFont, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Source Sans Pro, -apple-system, BlinkMacSystemFont, sans-serif"
    fontSize: ".78rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: ".08em"
rounded:
  sm: "8px"
  md: "14px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "24px"
components:
  button-primary:
    backgroundColor: "{colors.text-primary}"
    textColor: "{colors.bg-void}"
    rounded: "{rounded.sm}"
    padding: "0.5rem 1rem"
  button-primary-hover:
    backgroundColor: "#c7d2e3"
    textColor: "{colors.bg-void}"
  metric-card:
    backgroundColor: "linear-gradient(145deg, {colors.surface-gradient-start}, {colors.surface-gradient-end})"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "16px 17.6px"
  pp-card:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "16px 17.6px"
  eyebrow-label:
    textColor: "{colors.accent-eyebrow}"
    typography: "{typography.label}"
---

# Design System: Portfolio Pro

## 1. Overview

**Creative North Star: "Der Portfolio Manager"**

Portfolio Pro sieht aus, wie es sich anfühlt, das eigene Depot abends zu prüfen: ein dunkles, ruhiges Instrumentenbrett, das Zahlen zuerst zeigt und keine Ablenkung duldet. Der Bildschirm ist fast durchgängig ein tiefes Marineblau-Schwarz (`#0b1120`), in dem Karten sich nur durch einen minimal helleren Verlauf und eine dünne Border vom Hintergrund abheben — keine Schatten, keine Spielereien. Farbe wird knapp eingesetzt und immer bedeutungstragend: Grün und Rot markieren Gewinn und Verlust, ein einzelnes Mint-Grün markiert Rubriken-Eyebrows, der Rest bleibt Graublau in Abstufungen.

Das System lehnt bewusst zwei verbreitete Muster ab: das generische graue SaaS-Dashboard mit austauschbaren KPI-Kacheln und Gradient-Zahlen, und das verspielte Consumer-Fintech mit bunten Illustrationen. Stattdessen wirkt Portfolio Pro wie professionelle Terminal-Software — seriös, dicht mit echten Zahlen, ohne dekorative Distanz zwischen Nutzer und Daten.

**Key Characteristics:**
- Fast schwarzer, marineblauer Grund als Standard-Bühne, kein warmes Grau oder Creme
- Karten und Metrics heben sich durch tonale Verläufe + 1px-Border ab, nie durch Schlagschatten
- Grün/Rot ausschließlich für Gewinn/Verlust-Semantik, sonst zurückhaltende Graublau-Palette
- Ein einziger Akzentton (Mint-Grün `#6ee7b7`) für Eyebrow-Labels als einzige "Marken"-Farbe
- Helles Theme spiegelt exakt dieselbe Struktur, invertiert nur die Tonwerte

## 2. Colors

Die Palette ist im Kern monochrom-neutral (Marineblau-Grau-Skala), mit Farbe ausschließlich als Bedeutungsträger für Gewinn/Verlust und einem einzigen Akzentton für Struktur-Labels.

### Primary
- **Void Navy** (`#0b1120`): Haupt-Hintergrund der App im Dunkelmodus (`stAppViewContainer`, `stHeader`). Die "Bühne", auf der alle Daten liegen.
- **Sidebar Ink** (`#101827`): Sidebar-Hintergrund, minimal heller als Void Navy für dezente Trennung.
- **Surface Slate** (`#111827`): Basisfläche für Karten (`.pp-card`), Expander, Formulare — der zweite Dunkelheitsgrad der Skala.

### Secondary
- **Mint Eyebrow** (`#6ee7b7`): Einziger echter Akzentton. Ausschließlich für `.pp-eyebrow`-Labels über Seitentiteln — signalisiert "hier beginnt ein neuer Abschnitt", sonst nirgends verwendet.

### Data Semantics (Gewinn/Verlust)
Zwei parallele Grün/Rot-Paare sind aktuell im Code aktiv — dokumentiert wie vorgefunden, siehe Named Rule unten.
- **Data Green** (`#23c55e`) / **Data Red** (`#ff4b4b`): Gewinn/Verlust in Tabellen (G/V-Ampel via Pandas-Styler), Gauge-Steps, Chart-Linien (z.B. Wertentwicklungs-Linie).
- **Metric Green** (`#4ade80`) / **Metric Rose** (`#fb7185`): `.pp-positive` / `.pp-negative` Textklassen, z.B. für Inline-Badges neben Metrics.
- **Amber Warn** (`#ffa500`): mittlerer Gauge-Step (40–70 Punkte), Warnzone zwischen Rot und Grün.

### Tertiary (Chart-Kategorien)
- **Chart Mint** (`#34d399`), **Chart Blue** (`#60a5fa`), **Chart Violet** (`#a78bfa`), **Chart Amber** (`#fbbf24`), **Chart Rose** (`#fb7185`), **Chart Slate** (`#94a3b8`): sechsteilige kategoriale Palette für Allokations-Kreisdiagramme (Positionen nach Symbol/Kategorie).

### Neutral
- **Border Slate** (`#26344d`): einzige Border-Farbe im Dunkelmodus — Karten, Sidebar, Tabellen, Formulare, `hr`.
- **Text Primary** (`#e5edf9`): Haupttext, Metric-Werte, Überschriften im Dunkelmodus.
- **Text Muted** (`#aab7cc`): Sekundärtext — Metric-Labels, Captions, Widget-Labels, `.pp-subtle`.
- Helles Theme spiegelt dieselben Rollen: **Light Bg** (`#f7f9fc`), **Light Surface** (`#ffffff`), **Light Surface Alt** (`#f5f8fc`), **Light Border** (`#dbe4f0`), **Light Text Primary** (`#182235`), **Light Text Muted** (`#64748b`).

### Named Rules
**Die Ein-Akzent-Regel.** Mint-Grün (`#6ee7b7`) ist der einzige nicht-semantische Farbakzent im System und erscheint ausschließlich in Eyebrow-Labels. Keine weitere "Markenfarbe" für Buttons, Links oder Icons einführen.

**Die Doppel-Ampel-Inkonsistenz.** Data Green/Red (`#23c55e`/`#ff4b4b`) und Metric Green/Rose (`#4ade80`/`#fb7185`) sind zwei separate, historisch gewachsene Paare für dieselbe Gewinn/Verlust-Semantik. Das ist aktuell so im Code, aber keine bewusste Design-Entscheidung — bei nächster Gelegenheit auf ein einziges Paar vereinheitlichen, statt ein drittes hinzuzufügen.

## 3. Typography

**Body/Display Font:** Source Sans Pro (Streamlit-Standard-Systemfont, kein eigener Font-Import im Projekt)
**Label/Mono Font:** keiner — Labels nutzen dieselbe Sans-Familie in Großbuchstaben mit weiter Laufweite statt einer Mono-Schrift.

**Character:** Rein funktional — die App verlässt sich vollständig auf Streamlits Standard-Sans-Stack. Hierarchie entsteht durch Gewicht, Größe und Farbe (Eyebrow/Muted-Text), nicht durch Font-Pairing. Das passt zum nüchternen Terminal-Charakter: keine Schriftwahl, die Aufmerksamkeit auf sich zieht.

### Hierarchy
- **Headline** (700, `1.9rem` via `st.title`, Zeilenhöhe 1.2): Seitentitel, ein Vorkommen pro Seite, immer mit `.pp-eyebrow` darüber.
- **Title** (650, `1.1rem`): Karten-/Abschnittsüberschriften, `st.markdown("####")`-Level.
- **Body** (400, `1rem`, Zeilenhöhe 1.5): Fließtext, Tabelleninhalte, Chat-Antworten.
- **Label** (700, `.78rem`, Laufweite `.08em`, GROSSBUCHSTABEN): `.pp-eyebrow` — die einzige uppercase-getrackte Textrolle im System, reserviert für Abschnitts-Kennzeichnung direkt über Seitentiteln.
- **Metric Value** (650, Streamlit-Metric-Standardgröße): Kennzahlen in `[data-testid="stMetricValue"]`, immer in Text Primary.

### Named Rules
**Die Eine-Uppercase-Regel.** Getrackte Großbuchstaben-Labels sind ausschließlich der Eyebrow über Seitentiteln vorbehalten — nicht für Buttons, Tabs oder Badges wiederverwenden, sonst verliert das Muster seine Signalwirkung.

## 4. Elevation

Portfolio Pro kennt keine Schlagschatten. Tiefe entsteht ausschließlich durch tonale Abstufung: Karten und Metrics liegen als minimal hellere Fläche mit dezentem Verlauf und einer 1px-Border auf dem dunkleren Hintergrund. Das erzeugt Struktur ohne die weiche, "schwebende" Anmutung von Drop-Shadows — passend zum nüchternen, flachen Terminal-Charakter.

### Shadow Vocabulary
- Keine `box-shadow`-Werte im System. Tiefe wird über `background: linear-gradient(145deg, {surface-gradient-start}, {surface-gradient-end})` plus `border: 1px solid {border-subtle}` simuliert.

### Named Rules
**Die Flach-durch-Verlauf-Regel.** Wo andere Systeme einen Schatten für Tiefe einsetzen würden, nutzt Portfolio Pro einen 145°-Verlauf zwischen zwei eng benachbarten Surface-Tönen plus 1px-Border. Kein `box-shadow` einführen, auch nicht bei Hover — das würde den flachen Terminal-Look brechen.

## 5. Components

Alle Komponenten sind Streamlit-native Bausteine (`st.metric`, `st.dataframe`, `st.tabs`, `st.expander`), gestylt über global injiziertes CSS in `apply_theme()` — kein eigenes Komponenten-Framework.

### Buttons
- **Primary** (`button[kind="primary"]`): invertierter Fill — Text-Primary-Farbe als Hintergrund, Void-Navy/Ink als Textfarbe (im Hellmodus gespiegelt: Ink-Fill, weißer Text). Bewusst **keine** Akzent- oder G/V-Farbe, da Primary-Buttons auf dieser Seite reale Trades/API-Calls auslösen und nicht mit Gewinn/Verlust-Signalen verwechselt werden dürfen.
- **Hover:** eine Stufe heller/dunkler als der Fill, gleiche Textfarbe.
- **Focus:** `outline: 2px solid {colors.accent-eyebrow}` — einzige Stelle außerhalb der Eyebrow, an der der Mint-Akzent auftaucht, hier rein funktional als Fokusring.
- **Disabled:** fällt auf Border-Slate/Light-Border zurück, gedämpfte Textfarbe, `opacity: .6`.
- **Secondary:** Streamlit-Default (transparent, Border-Slate-Outline) bleibt unverändert.

### Metric Cards
- **Shape:** `border-radius: 14px`, `border: 1px solid {border-subtle}`, Innenabstand `1rem 1.1rem`.
- **Background:** `linear-gradient(145deg, {surface-gradient-start}, {surface-gradient-end})`.
- **Label:** `.88rem`, Text Muted.
- **Value:** `font-weight: 650`, Text Primary.
- Verwendet für jede Kennzahl im Dashboard, in Aktien/Krypto/Cash-Übersichten und im KI-Portfolio-Vergleich.

### Cards (`.pp-card`)
- **Shape:** `border-radius: 14px`, `border: 1px solid {border-subtle}`.
- **Background:** einfarbig Surface Slate (kein Verlauf, im Gegensatz zu Metric Cards) — Verlauf ist Metrics vorbehalten.
- **Padding:** `1rem 1.1rem`, `height: 100%` für gleichmäßige Grid-Höhen.

### Data Tables
- **Style:** `st.dataframe` mit Pandas-Styler für G/V-Spalten (`_gv_style`), Border in Border Slate, kein eigenes Zebra-Muster über Streamlit-Default hinaus.
- **State:** Gewinn/Verlust-Zellen bekommen `color: {data-positive}` bzw. `{data-negative}` plus `font-weight: 600` — Farbe ist die einzige Kodierung, kein zusätzliches Icon/Symbol.

### Gauge (Senior-Rating)
- **Style:** Plotly-Indicator mit drei Zonen: Rot (`0–40`, `#ff4b4b`), Amber (`40–70`, `#ffa500`), Grün (`70–100`, `#23c55e`). Zeiger- und Zahlenfarbe folgt `_text_color()` und passt sich live dem Theme an.

### Charts
- **Preis-Chart:** Linie in Standardfarbe + gestrichelte MA50 (`deepskyblue`) / MA200 (`orange`), Bollinger-Band als transparentes Hellblau (`rgba(173,216,230,0.5)`).
- **Allocation Pie:** Donut (`hole: 0.62`) mit der sechsteiligen Chart-Palette, Legende horizontal unter dem Chart.
- **Allocation Bars:** horizontale Balken in Chart Blue, für Portfolios mit vielen Positionen lesbarer als eine große Torte.

### Navigation
- **Style:** Streamlit `st.navigation` mit Gruppen-Überschriften (Übersicht, Depots, Analysen, Verwaltung), Icon + Label pro Eintrag, Sidebar-Hintergrund in Sidebar Ink.
- **Sidebar-Header:** `## Portfolio Pro` + Caption, gefolgt von Dark-Mode-Toggle und API-Key-Status (`st.success`/`st.warning`).

### Tabs
- Streamlit-native `st.tabs`, genutzt zur Unterteilung verwandter Ansichten auf einer Seite (z.B. Krypto-KI/Aktien-KI, Kontostand/Ein-Auszahlungen, Positionen/Import).

## 6. Do's and Don'ts

### Do:
- **Do** den Void-Navy-Grund (`#0b1120`) als Standard-Bühne beibehalten — kein warmes Grau, kein Creme, keine helle Standardfläche im Dunkelmodus.
- **Do** Tiefe ausschließlich über den 145°-Verlauf + 1px-Border erzeugen (Metric Cards, `.pp-card`), niemals über `box-shadow`.
- **Do** Grün/Rot strikt für Gewinn/Verlust-Semantik reservieren — keine dekorative Verwendung dieser Farben anderswo.
- **Do** neue Kennzahlen konsequent als Streamlit-`st.metric` mit demselben Card-Styling einbinden, damit alle Seiten (Aktien, Krypto, Cash) gleich aussehen.
- **Do** Eyebrow-Labels (`.pp-eyebrow`) für Seitentitel weiterverwenden, aber nirgends sonst — die Knappheit ist der Punkt.

### Don't:
- **Don't** ein generisches SaaS-Dashboard-Grau einführen — keine austauschbaren Card-Grids mit Gradient-Zahlen "wie aus der Box".
- **Don't** verspieltes Consumer-Fintech-Styling verwenden — keine bunten Illustrationen, keine Konfetti-/Erfolgs-Animationen, keine runden Maskottchen-Icons.
- **Don't** ein drittes Grün/Rot-Paar hinzufügen, um die bestehende Doppel-Ampel-Inkonsistenz zu "lösen" — bei Gelegenheit auf eines der beiden bestehenden Paare vereinheitlichen.
- **Don't** Drop-Shadows für Karten oder Buttons einführen, auch nicht subtil bei Hover — bricht die Flach-durch-Verlauf-Regel.
- **Don't** eine zweite Akzentfarbe neben Mint-Eyebrow (`#6ee7b7`) einführen, ohne eine klare neue Rolle dafür zu definieren — das System lebt von der Zurückhaltung.
- **Don't** `st.button(type="primary")` ungestylt lassen — Streamlits Default-Rot (`#ff4b4b`) ist identisch mit Data Red und würde jede kostenpflichtige/destruktive Aktion wie eine Verlust-Warnung aussehen lassen. Immer über `button[kind="primary"]` in `apply_theme()` führen (siehe Components → Buttons).
