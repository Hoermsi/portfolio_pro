# Portfolio Pro – CLAUDE.md

## Projektüberblick

Professionelles Streamlit-Tool zur Verwaltung und KI-gestützten Analyse von **Aktien, Krypto und Cash** – getrennt geführt. Nachfolger der alten App unter `../` (V1, bleibt unangetastet liegen; ihr `portfolio.json` wird beim ersten Start automatisch migriert). Multi-Agenten-System ("Senior Asset Manager") liefert Analysen; zwei **KI-Portfolios** (Schatten-Depots) testen automatisiert, ob eine KI-Strategie das echte Depot schlägt.

---

## Starten

```bat
cd portfolio_pro
pip install -r requirements.txt
copy .env.example .env    :: API-Keys eintragen
python -m streamlit run app.py
:: oder Doppelklick auf "Start Portfolio Pro.bat"
```

```bat
python -m pytest tests/ -q
```

---

## Architektur

```
app.py                  Streamlit-Entry, st.navigation, Sidebar (Modellwahl, Kosten)
core/
  config.py             .env-Loading, Claude-Modell-Registry + Preise
  db.py                 SQLite: Schema, alle CRUD-Funktionen, V1-Migration
  models.py             Position/Valuation-Dataclasses, evaluate()
  portfolio.py          Positionen + Live-Kurse -> bewertete Positionen
  shadow.py             KI-Portfolio-Engine (Schatten-Depots, siehe unten)
  cache.py              Framework-unabhängiger TTL-Cache-Decorator
data/
  stocks.py             yfinance: Kurse, Historie, Fundamentaldaten
  crypto.py             CoinGecko (Batch) + Kraken-Fallback, EUR-Pegging
  kraken.py             Kraken read-only API: Sync, Einstandskurse, EUR-Cash
  fx.py                 Währungsumrechnung mit Plausibilitätsband
  news.py                Google-News-RSS
  flatex.py              Flatex-CSV-Import (mehrere Konten, Replace-Semantik)
analysis/
  technical.py           RSI, MA20/50/200, Bollinger, MACD, Fibonacci, Scoring
  risk.py                 Volatilität, Max Drawdown, Sharpe, Konzentration, Korrelation
  performance.py          Snapshot-Historie -> Wertverlauf
agents/
  base.py                 Claude-Call, Structured Outputs, Kosten-Tracking
  specialists.py          4 Spezialisten (Technik/Fundamental/News/Risiko)
  senior_manager.py       Orchestriert Spezialisten + Synthese (Einzelwert/Portfolio)
  strategist.py           Portfolio-Stratege für die KI-Portfolios (siehe unten)
  chat.py                 Claude-Chat mit Tool-Use (Portfolio, Assets, Schatten-Depots)
  dossier.py              Baut Prompt-Kontext (Fakten-Dossiers) für Agenten
views/                    Eine Datei je Nav-Seite (siehe Tabelle unten)
ui/components.py          Wiederverwendbare Streamlit-Bausteine (Gauge, Charts, Reports)
tests/                    pytest, externe APIs gemockt/monkeypatched
```

### Navigation (app.py)

| Seite | Datei | url_path |
|---|---|---|
| Dashboard | `views/dashboard.py` | `/` |
| Aktien | `views/stocks.py` + `views/positions.py` | `aktien` |
| Krypto | `views/crypto.py` + `views/positions.py` | `krypto` |
| Cash | `views/cash.py` | `cash` |
| Einzelwert-Analyse | `views/asset_detail.py` | `analyse` |
| AI Desk | `views/ai_desk.py` | `ai-desk` |
| KI-Portfolios | `views/ai_portfolio.py` | `ki-portfolio` |

`views/positions.py` ist **gemeinsam** für Aktien- und Krypto-Positionstabelle (Filter, Namen, G/V-Ampel, Pie-Chart) – Änderungen dort wirken auf beide Seiten.

---

## Datenhaltung: SQLite (`portfolio.db`)

Zentrale Tabellen (Details: `core/db.py`, ein `_SCHEMA`-String, `CREATE TABLE IF NOT EXISTS`):

| Tabelle | Zweck |
|---|---|
| `assets` | symbol, asset_type (`stock`/`crypto`), name, currency – UNIQUE(symbol, asset_type) |
| `positions` | Menge, Einstandskurs (EUR), **category** (= Konto/Ordner), source – UNIQUE(asset_id, category) |
| `snapshots` | täglicher Portfoliowert je asset_type -> Dashboard-Verlauf |
| `agent_runs` | Analyse-Historie (Einzelwert/Portfolio-Review/Strategie), Kosten |
| `cash_log` | Bankkonto-Stand-Historie (jeder Eintrag = Datenpunkt) |
| `shadow_positions`, `recommendations`, `shadow_log`, `shadow_snapshots` | KI-Portfolios, **alle mit `scope`-Spalte** (`crypto`\|`stock`) |
| `meta` | Key/Value (Migrationsflags, `shadow_start_crypto`/`shadow_start_stock`) |

**Kategorie = Konto.** `positions.category` trennt nicht nur "Ordner", sondern echte Konten: `Flatex-Aktien`, `Flatex-ETF`, `Kraken`, `Standard`. Der Radio-Filter in `views/positions.py` und die Konten-Übersicht gruppieren danach.

**Schema-Änderungen:** Neue Spalten/Tabellen kommen in `_SCHEMA` (idempotent). Falls sich UNIQUE-Constraints ändern (SQLite kann kein ALTER dafür), Migrationsfunktion nach dem Vorbild von `_migrate_shadow_scope()` schreiben: per `PRAGMA table_info` prüfen, sonst DROP + Neuanlage.

---

## Multi-Agenten-System

Zwei getrennte Verwendungen der Claude API, beide über `agents/base.py` (Structured Outputs via `output_config.format.json_schema`, Fallback ohne Schema bei `BadRequestError`, Kosten aus `response.usage`):

### 1. Senior Asset Manager (Analyse)
`agents/senior_manager.py` – **4 Spezialisten parallel** (ThreadPoolExecutor) + Senior-Synthese.
- **Einzelwert-Analyse**: Technik + Fundamental + News + Risiko -> Senior-Urteil (Score, Empfehlung, Chancen/Risiken)
- **Portfolio-Review**: nur Risiko-Manager + Senior, mit **Scope-Auswahl** (Gesamt/Aktien/Krypto)
- Modelle separat wählbar (Spezialisten vs. Senior), Kostenschätzung vor dem Start

### 2. KI-Portfolios / Portfolio-Stratege (`core/shadow.py` + `agents/strategist.py`)
**Zwei komplett getrennte Experimente**, `scope="crypto"` und `scope="stock"`:
- Jedes startet als Kopie der jeweiligen Asset-Klasse des echten Portfolios (`shadow.init_from_real(scope)`)
- Der Stratege gibt konkrete Anweisungen (kaufen/verkaufen/umschichten/halten, Symbol+Anteil%+Ziel) – Structured-Output-Schema ist **je Scope eingeschränkt** (`asset_type`-Enum nur `[scope, "cash"]`), zusätzlich Cross-Type-Guard in `apply_recommendation` (keine Aktie im Krypto-Depot etc.)
- Trades werden **automatisch virtuell ausgeführt** (0,25 % Trade-Kosten), Cash-Position (`CASH`/`cash`) erlaubt
- Stratege sieht bei jedem Lauf seine **früheren Empfehlungen samt Performance seither** (Erfolgskontrolle) – `_history_block()` in `strategist.py`
- Vergleich: `shadow.comparison_df(scope)` – **beide Reihen ab Experiment-Start rebasiert auf 100**, echter Zweig nutzt nur `snapshots` des passenden `asset_type` (fairer Vergleich: Krypto-KI vs. echtes Krypto-Depot)
- Changelog (`shadow_log`) + Empfehlungs-Historie in der UI (`views/ai_portfolio.py`, zwei Tabs)

### Chat (`agents/chat.py`)
Tool-Use-Chat: `get_portfolio_summary`, `get_asset_data`, `get_shadow_portfolio` (liefert beide KI-Depots). Claude holt sich Daten selbst statt zu raten.

---

## Wichtige Nicht-Offensichtlichkeiten (aus Bugfixes dieser Session)

- **FX-Härtung** (`data/fx.py`): yfinance liefert unter Last gelegentlich einen komplett falschen Kurs (z.B. USD→EUR als ~210 statt 0,875 – hat einmal eine NVDA-Position auf 44.000 € statt 184 € hochgerechnet). Plausibilitätsband `1e-5 < rate < 5.0`; Ausreißer werden verworfen, letzter guter Wert wird verwendet statt 1h lang einen Fehlwert zu cachen.
- **Krypto-Kurse als Batch**: `data/crypto.get_prices_eur(symbols)` holt **alle** Portfolio-Coins in einem CoinGecko-Request (freies Rate-Limit ist eng), mit Kraken-Public-Ticker als Fallback für Kurs **und** Historie (`_kraken_ohlc_eur`). Nie einzeln pro Coin abfragen.
- **EUR ist 1:1 gepegt**: `_EUR_PEGGED = {"EUR","EURC","EURT","EURR"}` – wird nie an CoinGecko/Kraken geschickt (würde falsch auflösen). Kraken-EUR-Cash (inkl. `.HOLD`) landet als Position `symbol="EUR", asset_type="crypto"` und wird bei `shadow.init_from_real` automatisch zu einer Cash-Position im Krypto-KI-Depot.
- **Plotly-Chart-`key`s sind Pflicht**: Sobald zwei Reports/Charts auf derselben Seite stehen (Historie, Live+Historie, zwei Scope-Tabs), führt ein fehlender `key` zu `StreamlitDuplicateElementId`. Immer `key=f"..._{scope}"` o.ä. vergeben.
- **`max_tokens` bei Structured Outputs**: Opus 4.8/Sonnet 5 nutzen adaptives Thinking – Denk-Token zählen gegen `max_tokens`. Zu knapp (früher 8000) → erzwungene JSON-Antwort wird leer abgeschnitten. Jetzt 16000, plus `stop_reason == "max_tokens"` wird als Fehler erkannt statt stillschweigend leere Felder zu zeigen.
- **`init_from_real` / Baseline-Konsistenz**: Positionen ohne Kurs (`value_eur is None`) werden beim Anlegen eines KI-Portfolios **ausgelassen und gemeldet** (`info["skipped"]`), nicht mit Wert 0 mitgezählt – sonst verzerrt eine kurzzeitig nicht abrufbare Position (yfinance-Hänger) die gesamte Vergleichsbasis.
- **Mengen-Aggregation**: Ein Symbol kann in mehreren Kategorien liegen (z.B. NVDA in `Standard` und `Flatex-Aktien`). `init_from_real` aggregiert Mengen pro Symbol vor dem Kopieren ins Schatten-Depot – sonst überschreibt die zweite Kategorie die erste.
- **Flatex-Import ist Replace-per-Konto**: `import_csv(file, category, replace=True)` löscht **nur** die Zielkategorie vor dem Import (`db.delete_positions_by_category`), erst nachdem alle Zeilen erfolgreich geparst wurden (kein Datenverlust bei defekter CSV). Namen kommen aus der `Bezeichnung`-Spalte, falls vorhanden.
- **Namen-Backfill ist lazy**: `assets.name` wird beim ersten Rendern einer Position ohne Namen einmalig aus yfinance/CoinGecko nachgeladen und per `db.set_asset_name()` persistiert – kein Netz-Call bei jedem Seitenaufruf.
- **Dunkelmodus = nativer Theme-Switch + CSS-Overlay** (`ui/components.apply_theme`, Toggle in `app.py`, persistiert in `meta.ui_theme`): `_set_native_theme` schaltet Streamlits eingebautes Theme pro Sitzung um (`st._config.set_option("theme.base"/…)`, interne API in try/except) – nur so folgen Canvas-Tabellen (per CSS grundsätzlich NICHT stylbar), Plotly-Charts, Eingabefelder, Tabs und Alerts. Das CSS-Overlay liefert darüber die eigene Palette (Metrics, `.pp-card`, Sidebar). Plotly-Textfarben, die explizit gesetzt werden müssen (z.B. Gauge), nutzen `_text_color()`. `apply_theme` MUSS vor jeglichem Seiteninhalt laufen (app.py, früh).

---

## Konventionen

- **Deutsche UI-Texte**, deutsche Docstrings/Kommentare wo sie Kontext brauchen (User ist deutschsprachig, Kommentare erklären das *Warum*, nicht das *Was*).
- **Keine Anlageberatung**-Framing in allen Agenten-System-Prompts beibehalten – bewusste Design-Entscheidung.
- Tests mocken **immer** externe APIs (yfinance, CoinGecko, Kraken, Anthropic) – nie echte Netzwerkcalls in `tests/`. `tests/conftest.py` stellt `tmp_db`-Fixture (isolierte SQLite-DB via `monkeypatch` auf `config.DB_PATH`).
- Claude-Modell-IDs zentral in `core/config.CLAUDE_MODELS`/`CLAUDE_PRICING` – nie Modellstrings hardcoden.
- Bei Änderungen an `views/positions.py`: betrifft **immer beide** Aktien- und Krypto-Seite.

## Bekannte offene Punkte

- Kraken-Sync braucht API-Key mit Berechtigungen **"Query Funds"** + **"Query Closed Orders & Trades"** (für Einstandskurse).
- KI-Portfolios/Agenten-Analysen brauchen `ANTHROPIC_API_KEY` (separater Console-Key, nicht das Claude-Pro-Abo).
- Kein Steuer-Modell in den KI-Portfolios, nur pauschale 0,25 % Trade-Kosten.
- Ändert der User sein echtes Depot (frisches Geld, eigene Trades), verzerrt das den KI-Portfolio-Vergleich etwas – der indexierte %-Vergleich mildert das nur teilweise.

## Design Context

Strategischer und visueller Rahmen für UI-Arbeit liegt in [`PRODUCT.md`](PRODUCT.md) (Register `product`, Single-User-Tool, Positionierung, Anti-Referenzen) und [`DESIGN.md`](DESIGN.md) (Farb-/Typo-/Komponenten-System: dunkles Marineblau-Terminal, tonale Tiefe statt Schatten, Grün/Rot nur für G/V-Semantik). Vor UI-Änderungen beide Dateien lesen; sie sind die Quelle für Tokens und Do's/Don'ts, nicht dieses Kapitel.
