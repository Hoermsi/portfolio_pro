"""SQLite-Datenhaltung: Assets, Positionen, Snapshots, Agenten-Historie.

Migration: beim ersten Start wird das portfolio.json der V1 (Ordnerstruktur)
als Aktien-Positionen übernommen.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from core import clock

from core import config
from core.models import Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'crypto')),
    name TEXT DEFAULT '',
    currency TEXT DEFAULT 'EUR',
    coingecko_id TEXT DEFAULT '',
    UNIQUE (symbol, asset_type)
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    quantity REAL NOT NULL DEFAULT 0,
    buy_price_eur REAL NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'Standard',
    source TEXT NOT NULL DEFAULT 'manuell',
    updated_at TEXT,
    UNIQUE (asset_id, category)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    snap_date TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    total_value_eur REAL NOT NULL,
    UNIQUE (snap_date, asset_type)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    mode TEXT NOT NULL,
    total_score INTEGER,
    recommendation TEXT,
    cost_usd REAL DEFAULT 0,
    report_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    UNIQUE (scope, symbol, asset_type)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    run_id INTEGER,
    created_at TEXT NOT NULL,
    aktion TEXT NOT NULL,
    symbol TEXT,
    asset_type TEXT,
    ziel_symbol TEXT,
    ziel_asset_type TEXT,
    anteil_pct REAL,
    begruendung TEXT,
    kurs_symbol_eur REAL,
    kurs_ziel_eur REAL,
    angewendet INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shadow_log (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    aktion TEXT NOT NULL,
    von_symbol TEXT,
    nach_symbol TEXT,
    menge_von REAL,
    menge_nach REAL,
    kurs_von_eur REAL,
    kurs_nach_eur REAL,
    wert_eur REAL,
    recommendation_id INTEGER,
    notiz TEXT
);
CREATE TABLE IF NOT EXISTS shadow_snapshots (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    snap_date TEXT NOT NULL,
    total_value_eur REAL NOT NULL,
    UNIQUE (snap_date, scope)
);
CREATE TABLE IF NOT EXISTS cash_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    balance_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_cashflows (
    id INTEGER PRIMARY KEY,
    flow_date TEXT NOT NULL,
    amount_eur REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    price_eur REAL NOT NULL,
    fees_eur REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kraken_value_history (
    snap_date TEXT PRIMARY KEY,
    value_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sentiment_history (
    id INTEGER PRIMARY KEY,
    snap_date TEXT NOT NULL,
    indicator TEXT NOT NULL,
    value REAL NOT NULL,
    UNIQUE (snap_date, indicator)
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'crypto')),
    name TEXT DEFAULT '',
    target_above REAL,
    target_below REAL,
    day_move_pct REAL,
    rsi_alert INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    UNIQUE (symbol, asset_type)
);
CREATE TABLE IF NOT EXISTS onchain_history (
    id INTEGER PRIMARY KEY,
    metric TEXT NOT NULL,
    d TEXT NOT NULL,
    value REAL NOT NULL,
    UNIQUE (metric, d)
);
CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    size REAL NOT NULL,
    entry_px REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1,
    stop_px REAL,
    -- Letzter nachweislich auf der Boerse bestaetigter Trigger. Der
    -- angeforderte Code-Stop kann bei einem API-Fehler weiter sein als dieser
    -- Wert und wird deshalb getrennt gefuehrt.
    exchange_stop_px REAL,
    exchange_oid TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_px REAL,
    realized_pnl_usd REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    -- Herkunft: 'bot' (selbst eroeffnet), 'adopted' (auf der Boerse
    -- vorgefunden und uebernommen), 'manual'. BEWUSST ohne CHECK-Constraint:
    -- SQLite kann per ALTER TABLE ADD COLUMN keine CHECK-Klausel ergaenzen,
    -- eine frisch angelegte DB haette sonst ein anderes Schema als eine
    -- migrierte. Ohne dieses Feld ist eine uebernommene oder manuell
    -- eroeffnete Position spaeter nicht von einer Bot-Entscheidung zu
    -- unterscheiden - und wuerde jede Auswertung verfaelschen.
    origin TEXT NOT NULL DEFAULT 'bot',
    origin_note TEXT,
    decision_id INTEGER,
    -- Tatsaechlicher Eroeffnungszeitpunkt auf der Boerse, falls bekannt.
    -- opened_at ist bei einer Uebernahme der Zeitpunkt der UEBERNAHME; ohne
    -- dieses Feld bekaeme eine seit Tagen offene Fremdposition eine frische
    -- max_hold_hours-Uhr (core.bot_signals.exit_signal).
    exchange_opened_at TEXT,
    -- UNVERAENDERLICH, im Gegensatz zu stop_px: stop_px wird von
    -- update_bot_position_stop() beim Nachziehen ueberschrieben. Ohne diese
    -- Kopie rechnete die R-Multiple-Anzeige (views.trading_bot) mit dem
    -- ZULETZT nachgezogenen statt dem urspruenglich riskierten Abstand - ein
    -- Trade, der anfangs 2 $ riskierte, spaeter fast bis zum Einstieg
    -- nachgezogen wurde und 4 $ gewann, waere faelschlich als +10R statt +2R
    -- erschienen.
    initial_stop_px REAL,
    -- Unter welcher Scoring-Version (core.bot_signals.STRATEGY_VERSION) und
    -- welcher effektiven Konfiguration (core.bot_config.config_fingerprint())
    -- diese Position entstand - fehlte bislang komplett auf bot_positions,
    -- obwohl bot_signal_log das laengst je Signal festhaelt. Ohne beides ist
    -- im Nachhinein nicht rekonstruierbar, welches Regelwerk fuer einen
    -- bestimmten Trade tatsaechlich galt.
    strategy_version TEXT,
    config_hash TEXT,
    -- Bruttogewinn/-verlust bereits geschlossener TEIL-Fuellungen dieser
    -- Position (core.bot.close_position() bei einer Teilschliessung durch
    -- die Boerse). realized_pnl_usd am Ende (close_bot_position) zaehlt
    -- diesen Wert hinzu - ohne ihn wuerde eine fruehere Teil-Schliessung
    -- beim finalen Schluss der Restgroesse verschwinden, weil (fill_px -
    -- entry_px) * size dann nur noch die RESTgroesse bewertet.
    partial_realized_pnl_usd REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS bot_orders (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    intent TEXT NOT NULL,
    side TEXT,
    size REAL,
    order_type TEXT,
    requested_px REAL,
    status TEXT NOT NULL,
    exchange_oid TEXT,
    fill_px REAL,
    fee_usd REAL,
    error TEXT,
    decision_id INTEGER,
    position_id INTEGER
);
CREATE TABLE IF NOT EXISTS bot_decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    cycle_type TEXT NOT NULL CHECK (cycle_type IN ('deterministic', 'llm')),
    signals_json TEXT,
    run_id INTEGER,
    action TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    -- core.bot_config.config_fingerprint() zum Zeitpunkt DIESER Entscheidung -
    -- siehe bot_positions.config_hash-Kommentar oben fuer das Warum.
    config_hash TEXT
);
-- Eine Zeile je KANDIDAT je Takt - nicht nur je Trade. Uebersprungene und
-- blockierte Signale sind fuer eine spaetere Auswertung genauso wichtig wie
-- ausgefuehrte ("Fehler der Untaetigkeit"); bot_decisions.signals_json ist
-- dafuer ein undurchsuchbarer Textblob. Reine Datensammlung: nichts hiervon
-- wirkt auf die Live-Strategie zurueck.
CREATE TABLE IF NOT EXISTS bot_signal_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    candle_ts TEXT,
    symbol TEXT NOT NULL,
    side TEXT,
    score REAL,
    long_score REAL,
    short_score REAL,
    components_json TEXT,
    readings_json TEXT,
    price REAL,
    atr_pct REAL,
    stop_pct REAL,
    funding_hourly REAL,
    threshold REAL,
    risk_level INTEGER,
    strategy_version TEXT,
    -- core.bot_config.config_fingerprint() zum Zeitpunkt DIESES Kandidaten -
    -- siehe bot_positions.config_hash-Kommentar fuer das Warum.
    config_hash TEXT,
    -- 'opened' | 'skipped' | 'blocked' | 'disarmed' | 'error'
    -- ('ai_veto' ist seit Phase 8 Geschichte - die KI kann nichts mehr
    -- sperren, siehe agents/trader.py - bleibt aber in historischen Zeilen
    -- stehen, dieselbe Read-Time-Robustheit wie ueberall im Bot-Code)
    action TEXT NOT NULL,
    block_reason TEXT,
    decision_id INTEGER,
    position_id INTEGER,
    outcome_json TEXT,
    outcome_last_h INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bot_signal_log_pending
    ON bot_signal_log (outcome_last_h, ts);
CREATE INDEX IF NOT EXISTS idx_bot_signal_log_symbol ON bot_signal_log (symbol, ts);
-- Der Bot pollt alle 15 Min, eine Kerze schliesst nur stuendlich - ohne
-- diesen Schutz schreibt derselbe Kandidat denselben Kerzenstand bis zu
-- 4x. Ein spaeteres Lernmodell wuerde Stunden mit durchgehendem Runner
-- dadurch staerker gewichten als Stunden mit Ausfaellen. UNIQUE INDEX statt
-- Tabellen-Constraint, weil er wie bei bot_signal_log_pending oben per
-- IF NOT EXISTS idempotent bleibt (kein ALTER TABLE noetig).
CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_signal_log_dedup
    ON bot_signal_log (symbol, candle_ts, strategy_version, risk_level);
-- Wiedereinstieg ueber Signal-Reset statt reiner Uhr (Phase 3): der
-- zeitbasierte Cooldown (core.bot_config bot_limits()["symbol_cooldown_hours"],
-- core.bot_guards.check_symbol_cooldown) verhindert nur, dass DIESELBE
-- frisch geschlossene Kerze sofort erneut ausloest - nicht, dass ein Symbol
-- Stunden spaeter auf demselben, laengst bekannten Setup wieder einsteigt
-- (CLAUDE.md-Feedback: 17 Wiedereinstiege in dasselbe Symbol innerhalb von
-- 24h, davon 14 erneut in dieselbe Richtung, zusammen -6,59 $). Ein Symbol
-- wird bei jedem Schluss SCHARF GESTELLT (armed=0) und erst wieder scharf,
-- wenn core.bot._reentry_check() das Setup als nachweislich weg einstuft -
-- siehe dessen Docstring fuer die genauen Bedingungen.
CREATE TABLE IF NOT EXISTS bot_symbol_state (
    symbol TEXT PRIMARY KEY,
    armed INTEGER NOT NULL DEFAULT 1,
    disarmed_at TEXT,
    last_side TEXT
);
CREATE TABLE IF NOT EXISTS bot_equity (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    equity_usd REAL NOT NULL,
    equity_eur REAL NOT NULL,
    cash_usd REAL,
    fees_cum_usd REAL,
    funding_cum_usd REAL,
    net_flows_usd REAL
);
-- JEDER Walk-Forward-Lauf, bestanden UND durchgefallen (analysis.
-- bot_walkforward.run_walkforward, core.bot_config.log_walkforward_run).
-- Ohne diese Tabelle war ausschliesslich der zuletzt gespeicherte Lauf
-- sichtbar - wie viele Fenstergroessen/Risikostufen vorher DURCHGEFALLEN
-- waren, bevor eine Kombination bestand, war nirgends festgehalten. Das ist
-- im Kern eine Parametersuche auf dem Freigabe-Kriterium selbst, wenn sie
-- unsichtbar bleibt; diese Tabelle macht sie sichtbar, ohne das Ausprobieren
-- selbst zu verbieten (das waere eine Verhaltensaenderung, keine
-- Transparenz). Der Lauf ist reine Analyse - kein Zertifikat, das den
-- Live-Schalter beeinflusst (der verlangt nur Verbindungs-Vorpruefung +
-- Tippbestaetigung "LIVE", siehe views/trading_bot.py::_live_gate_dialog()).
CREATE TABLE IF NOT EXISTS bot_walkforward_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    num_windows INTEGER NOT NULL,
    risk_level INTEGER NOT NULL,
    gate_ok INTEGER NOT NULL,
    gate_reasons_json TEXT,
    pooled_json TEXT,
    -- Ungenutztes Relikt aus der frueheren Live-Nachweis-Zertifizierung
    -- (core.bot_config.log_walkforward_run() schreibt seither immer NULL) -
    -- Spalte bleibt bestehen, um keine Migration fuer einen reinen
    -- Aufraeum-Schritt zu brauchen.
    validation_hash TEXT
);
"""


@contextmanager
def _connect():
    # busy_timeout + WAL: der Trading-Bot laeuft als eigener Prozess (core/bot.py,
    # gestartet ueber bot_runner.py) und schreibt parallel zu Streamlit in dieselbe
    # DB. Ohne WAL blockiert ein Schreiber jeden Leser sofort; ohne busy_timeout
    # wirft ein Schreib-Konflikt sofort "database is locked" statt kurz zu warten.
    # Fuer die bisherige Single-Writer-Nutzung (nur Streamlit) aendert das nichts.
    con = sqlite3.connect(config.DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_data_dir():
    """Datenordner anlegen und eine evtl. vorhandene Alt-DB (aus dem Code-Ordner)
    einmalig übernehmen. So gehen weder deine bestehenden Daten noch die eines
    Freundes verloren, der zuvor die ordnerbasierte Version genutzt hat.
    """
    import shutil

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Migration nur am echten Standard-Ort ausführen. Tests hängen DB_PATH auf
    # einen tmp-Pfad um; dort darf die reale Repo-DB nicht hineinkopiert werden.
    is_default_location = config.DB_PATH == config.DATA_DIR / "portfolio.db"
    legacy = getattr(config, "LEGACY_DB_PATH", None)
    if (is_default_location and legacy and legacy != config.DB_PATH
            and legacy.exists() and not config.DB_PATH.exists()):
        shutil.copy2(legacy, config.DB_PATH)


def init_db():
    _ensure_data_dir()
    with _connect() as con:
        # Einmalig beim Initialisieren umschalten; ein PRAGMA journal_mode=WAL
        # bei JEDEM Lesezugriff würde unter parallelem Streamlit-/Runner-Betrieb
        # unnötig selbst Schreibsperren anfordern.
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(_SCHEMA)
        _migrate_shadow_scope(con)
        _migrate_bot_equity_net_flows(con)
        _migrate_bot_position_origin(con)
        _migrate_bot_config_hash(con)
        _migrate_bot_exchange_stop_px(con)
        _migrate_bot_partial_realized_pnl(con)
        _migrate_sentiment_prefix(con)
        _migrate_bot_positions_unique_open_symbol(con)
        _backfill_onboarded(con)
    _migrate_legacy_json()


def _backfill_onboarded(con):
    """Bestehende Installationen als 'onboarded' markieren, damit der Nutzer den
    Ersteinrichtungs-Assistenten NICHT sieht. Nur eine wirklich leere DB (Freund,
    frischer Start) bleibt un-onboarded und bekommt den Assistenten.
    """
    if con.execute("SELECT 1 FROM meta WHERE key = 'onboarded'").fetchone():
        return
    has_content = any(
        con.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone()
        for tbl in ("positions", "snapshots", "cash_log", "transactions")
    )
    if not has_content:
        has_content = con.execute(
            "SELECT 1 FROM meta WHERE key IN ('risk_profile', 'target_allocation') LIMIT 1"
        ).fetchone() is not None
    if has_content:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('onboarded', ?)",
                    (datetime.now().isoformat(timespec="seconds"),))


def _migrate_shadow_scope(con):
    """Alt-Schema ohne scope-Spalte (kombiniertes KI-Portfolio) -> neu aufbauen.

    Die Unique-Constraints ändern sich mit; SQLite kann das nicht per ALTER,
    daher DROP + Neuanlage. Ein evtl. altes kombiniertes Experiment verfällt
    bewusst - es lässt sich nicht sinnvoll in zwei Scopes aufteilen.
    """
    cols = [r["name"] for r in con.execute("PRAGMA table_info(shadow_positions)")]
    if "scope" in cols:
        return
    con.executescript(
        "DROP TABLE IF EXISTS shadow_positions;"
        "DROP TABLE IF EXISTS shadow_log;"
        "DROP TABLE IF EXISTS shadow_snapshots;"
        "DROP TABLE IF EXISTS recommendations;"
        "DELETE FROM meta WHERE key = 'shadow_start';"
    )
    con.executescript(_SCHEMA)


def _migrate_bot_equity_net_flows(con):
    """Reine Spalten-Ergänzung (kein UNIQUE-Constraint betroffen), daher per
    ALTER TABLE statt Drop+Neuanlage wie bei _migrate_shadow_scope()."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_equity)")]
    if "net_flows_usd" not in cols:
        con.execute("ALTER TABLE bot_equity ADD COLUMN net_flows_usd REAL")


def _migrate_bot_position_origin(con):
    """Herkunfts-Spalten fuer bot_positions nachziehen. Reine Spalten-
    Ergaenzung wie _migrate_bot_equity_net_flows(), daher ALTER TABLE statt
    Drop+Neuanlage. Bestehende Zeilen behalten den DEFAULT 'bot' und werden
    direkt im Anschluss anhand von bot_decisions korrigiert (siehe unten) -
    fuer alles, was DORT nicht mehr rekonstruierbar ist, bleibt 'bot' die
    ehrlichste verbleibende Annahme."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_positions)")]
    is_new_migration = "initial_stop_px" not in cols
    for name, ddl in (("origin", "TEXT NOT NULL DEFAULT 'bot'"),
                      ("origin_note", "TEXT"),
                      ("decision_id", "INTEGER"),
                      ("exchange_opened_at", "TEXT"),
                      ("initial_stop_px", "REAL")):
        if name not in cols:
            con.execute(f"ALTER TABLE bot_positions ADD COLUMN {name} {ddl}")
    if is_new_migration:
        # Best-effort-Rueckwirkung: der TATSAECHLICHE urspruengliche Stop
        # bestehender Zeilen ist nicht mehr rekonstruierbar (stop_px wurde
        # ggf. laengst nachgezogen), aber der aktuelle Wert ist immer noch
        # besser als NULL - identisch zu dem, was die R-Multiple-Anzeige
        # bislang ohnehin verwendet hat. Ab hier wird initial_stop_px beim
        # Anlegen einer Position korrekt EINMALIG gesetzt und nie wieder
        # veraendert.
        con.execute("UPDATE bot_positions SET initial_stop_px = stop_px "
                   "WHERE initial_stop_px IS NULL")
        _reconcile_adopted_origin_from_decisions(con)


def _reconcile_adopted_origin_from_decisions(con):
    """Positionen, die laut bot_decisions tatsaechlich UEBERNOMMEN wurden,
    aber durch die blanket-Migration oben auf origin='bot' landeten, anhand
    des Audit-Trails richtigstellen.

    Konkret beobachtet: fuenf noch offene Positionen (ETH/SOL/LINK/DOT/ADA)
    trugen 'position_adopted'-Eintraege in bot_decisions, standen nach der
    Migration aber auf origin='bot' - sie waeren sonst faelschlich als
    eigene Bot-Entscheidungen ins Lernen eingeflossen. Nur bei zeitlicher
    Naehe (<= 60s) zwischen Uebernahme-Entscheidung und opened_at korrigiert,
    damit eine SPAETERE, tatsaechlich vom Bot eroeffnete Position im selben
    Symbol nicht versehentlich umetikettiert wird."""
    decisions = con.execute(
        "SELECT signals_json, created_at FROM bot_decisions WHERE action = 'position_adopted'"
    ).fetchall()
    for d in decisions:
        try:
            info = json.loads(d["signals_json"] or "{}")
            symbol = info["symbol"]
            decided_at = datetime.fromisoformat(d["created_at"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        row = con.execute(
            "SELECT id, opened_at, origin FROM bot_positions "
            "WHERE symbol = ? AND status = 'open'", (symbol,)
        ).fetchone()
        if row is None or row["origin"] != "bot":
            continue
        try:
            opened_at = datetime.fromisoformat(row["opened_at"])
        except (TypeError, ValueError):
            continue
        if abs((decided_at - opened_at).total_seconds()) > 60:
            continue
        con.execute(
            "UPDATE bot_positions SET origin = 'adopted', origin_note = ? WHERE id = ?",
            (f"Nachträglich korrigiert: laut bot_decisions am {d['created_at']} "
            f"automatisch übernommen, aber durch die Migration auf origin='bot' "
            f"gesetzt.", row["id"]))


def _migrate_bot_config_hash(con):
    """Konfigurations-Signatur nachziehen: core.bot_positions.strategy_version/
    config_hash sowie bot_decisions.config_hash und bot_signal_log.config_hash
    fehlten bislang komplett. Reine Spalten-Ergaenzung wie
    _migrate_bot_equity_net_flows(), daher ALTER TABLE statt Drop+Neuanlage.
    Bestehende (Alt-)Zeilen bleiben NULL - der Fingerabdruck laesst sich im
    Nachhinein nicht rekonstruieren, das ist ehrlicher als ein erfundener
    Wert."""
    pos_cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_positions)")]
    for name, ddl in (("strategy_version", "TEXT"), ("config_hash", "TEXT")):
        if name not in pos_cols:
            con.execute(f"ALTER TABLE bot_positions ADD COLUMN {name} {ddl}")
    decision_cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_decisions)")]
    if "config_hash" not in decision_cols:
        con.execute("ALTER TABLE bot_decisions ADD COLUMN config_hash TEXT")
    signal_cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_signal_log)")]
    if "config_hash" not in signal_cols:
        con.execute("ALTER TABLE bot_signal_log ADD COLUMN config_hash TEXT")


def _migrate_bot_exchange_stop_px(con):
    """Letzten bestaetigten Exchange-Stop fuer Altpositionen nachziehen.

    Ein alter nachgezogener ``stop_px`` kann durch den bisherigen Fehler nur
    lokal gespeichert worden sein. Der Anfangsstop ist die konservative
    Rueckfallbasis; ohne ihn bleibt der aktuelle Stop die einzig bekannte
    Naeherung. So loest der naechste Takt bei Bedarf eine sichere, erneute
    Ersetzung aus statt einen nur vermeintlich engeren Schutz zu glauben.
    """
    cols = [r["name"] for r in con.execute("PRAGMA table_info(bot_positions)")]
    if "exchange_stop_px" not in cols:
        con.execute("ALTER TABLE bot_positions ADD COLUMN exchange_stop_px REAL")
    con.execute(
        "UPDATE bot_positions SET exchange_stop_px = COALESCE(initial_stop_px, stop_px) "
        "WHERE exchange_stop_px IS NULL"
    )


def _migrate_bot_partial_realized_pnl(con):
    """ADD COLUMN fuer partial_realized_pnl_usd (siehe Schema-Docstring) -
    Bestandskonten (Migration von vor dieser Spalte) bekommen 0.0, identisch
    zum Schema-Default, damit eine bereits offene Position beim naechsten
    finalen Schluss nicht mit NULL + Zahl rechnet."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(bot_positions)")]
    if "partial_realized_pnl_usd" not in cols:
        con.execute(
            "ALTER TABLE bot_positions ADD COLUMN partial_realized_pnl_usd REAL NOT NULL DEFAULT 0.0"
        )


def _migrate_bot_positions_unique_open_symbol(con):
    """Partial-UNIQUE-Index: hoechstens eine OFFENE Zeile je Symbol in
    bot_positions - zweite Verteidigungslinie gegen zwei parallel laufende
    Runner-Prozesse (die eigentliche Sperre ist die Lockdatei in
    bot_runner.py; upsert_open_bot_position() ist SELECT-dann-INSERT/UPDATE,
    nicht atomar ueber zwei Verbindungen/Prozesse hinweg). SQLite
    unterstuetzt partielle UNIQUE-Indizes direkt (WHERE status='open') - kein
    DROP+Neuanlage wie bei _migrate_shadow_scope() noetig, ein Index ist
    additiv.

    CREATE UNIQUE INDEX schlaegt fehl, wenn zum Migrationszeitpunkt bereits
    ein Verstoss in der DB steht - das darf init_db() bei einer Single-User-
    App mit evtl. bereits laufendem Live-Bot nicht zum Absturz bringen. In
    dem (erwartet seltenen) Fall bleibt der Index unangelegt und wird bei
    jedem weiteren Start erneut versucht, bis der Konflikt manuell bereinigt
    wurde."""
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_bot_positions_open_symbol'"
    ).fetchone()
    if exists:
        return
    dupes = con.execute(
        "SELECT symbol FROM bot_positions WHERE status='open' "
        "GROUP BY symbol HAVING COUNT(*) > 1"
    ).fetchall()
    if dupes:
        return
    con.execute(
        "CREATE UNIQUE INDEX idx_bot_positions_open_symbol "
        "ON bot_positions(symbol) WHERE status='open'"
    )


def _migrate_sentiment_prefix(con):
    """Sentiment-Indikatoren bekamen bislang keinen Markt-Präfix - "overall"
    von Krypto und Aktien hätten sich sonst am selben Tag überschrieben
    (UNIQUE(snap_date, indicator)). Alte, präfixlose Zeilen waren per
    Definition Krypto (das einzige Sentiment vor dem Aktien-Set) und werden
    einmalig auf "crypto:"-Präfix umgeschrieben. Natürlich idempotent: nach
    dem ersten Lauf enthält jeder Schlüssel bereits ":", das WHERE greift
    dann ins Leere.
    """
    con.execute(
        "UPDATE sentiment_history SET indicator = 'crypto:' || indicator "
        "WHERE indicator NOT LIKE '%:%'"
    )


# --- ASSETS & POSITIONEN ---

def upsert_asset(con, symbol: str, asset_type: str, name: str = "",
                 currency: str = "EUR", coingecko_id: str = "") -> int:
    symbol = symbol.strip().upper()
    row = con.execute(
        "SELECT id FROM assets WHERE symbol = ? AND asset_type = ?",
        (symbol, asset_type),
    ).fetchone()
    if row:
        if name or coingecko_id:
            con.execute(
                "UPDATE assets SET name = COALESCE(NULLIF(?, ''), name), "
                "coingecko_id = COALESCE(NULLIF(?, ''), coingecko_id) WHERE id = ?",
                (name, coingecko_id, row["id"]),
            )
        return row["id"]
    cur = con.execute(
        "INSERT INTO assets (symbol, asset_type, name, currency, coingecko_id) VALUES (?, ?, ?, ?, ?)",
        (symbol, asset_type, name, currency, coingecko_id),
    )
    return cur.lastrowid


def get_asset_coingecko_id(symbol: str, asset_type: str) -> str | None:
    """Vom Nutzer bestätigte CoinGecko-ID für ein Symbol, falls hinterlegt
    (siehe set_coingecko_id) - None wenn nicht gesetzt oder Asset unbekannt."""
    with _connect() as con:
        row = con.execute(
            "SELECT coingecko_id FROM assets WHERE symbol = ? AND asset_type = ?",
            (symbol.strip().upper(), asset_type),
        ).fetchone()
    cid = row["coingecko_id"] if row else None
    return cid or None


def set_coingecko_id(symbol: str, asset_type: str, coingecko_id: str):
    """Bestätigte CoinGecko-ID für ein Symbol hinterlegen (überschreibt bei
    mehrdeutigen Symbolen die automatische 'erstbester Treffer'-Auflösung)."""
    with _connect() as con:
        upsert_asset(con, symbol, asset_type, coingecko_id=coingecko_id.strip())


def save_position(symbol: str, asset_type: str, quantity: float, buy_price_eur: float,
                  category: str = "Standard", source: str = "manuell", name: str = ""):
    with _connect() as con:
        asset_id = upsert_asset(con, symbol, asset_type, name=name)
        now = datetime.now().isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO positions (asset_id, quantity, buy_price_eur, category, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (asset_id, category) DO UPDATE SET "
            "quantity = excluded.quantity, buy_price_eur = excluded.buy_price_eur, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (asset_id, float(quantity), float(buy_price_eur), category.strip(), source, now),
        )


def list_positions(asset_type: str | None = None) -> list[Position]:
    q = (
        "SELECT p.id, a.symbol, a.asset_type, a.name, a.currency, "
        "p.quantity, p.buy_price_eur, p.category, p.source "
        "FROM positions p JOIN assets a ON a.id = p.asset_id "
    )
    params: tuple = ()
    if asset_type:
        q += "WHERE a.asset_type = ? "
        params = (asset_type,)
    q += "ORDER BY a.symbol"
    with _connect() as con:
        rows = con.execute(q, params).fetchall()
    return [Position(**dict(r)) for r in rows]


def list_categories(asset_type: str) -> list[str]:
    with _connect() as con:
        rows = con.execute(
            "SELECT DISTINCT p.category FROM positions p JOIN assets a ON a.id = p.asset_id "
            "WHERE a.asset_type = ? ORDER BY p.category",
            (asset_type,),
        ).fetchall()
    return [r["category"] for r in rows]


def delete_position(position_id: int):
    with _connect() as con:
        con.execute("DELETE FROM positions WHERE id = ?", (position_id,))


def set_asset_name(symbol: str, asset_type: str, name: str):
    """Setzt den Anzeigenamen eines Assets (z.B. aus yfinance/CoinGecko-Backfill)."""
    name = (name or "").strip()
    if not name:
        return
    with _connect() as con:
        con.execute(
            "UPDATE assets SET name = ? WHERE symbol = ? AND asset_type = ?",
            (name, symbol.strip().upper(), asset_type),
        )


def delete_positions_by_category(asset_type: str, category: str) -> int:
    """Löscht alle Positionen einer Kategorie (z.B. beim Ersetzen eines Flatex-Kontos).
    Gibt die Anzahl gelöschter Positionen zurück."""
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM positions WHERE category = ? AND asset_id IN "
            "(SELECT id FROM assets WHERE asset_type = ?)",
            (category.strip(), asset_type),
        )
        return cur.rowcount


def get_position_quantity(symbol: str, asset_type: str) -> float:
    """Gesamtstückzahl eines Symbols über alle Kategorien."""
    with _connect() as con:
        row = con.execute(
            "SELECT SUM(p.quantity) AS q FROM positions p JOIN assets a ON a.id = p.asset_id "
            "WHERE a.symbol = ? AND a.asset_type = ?",
            (symbol.strip().upper(), asset_type),
        ).fetchone()
    return float(row["q"] or 0.0)


# --- WATCHLIST (Favoriten + Kursalarme) ---

def add_watchlist(symbol: str, asset_type: str, name: str = "") -> int:
    """Symbol zur Watchlist hinzufügen (idempotent pro symbol+asset_type)."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO watchlist (symbol, asset_type, name, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (symbol, asset_type) DO NOTHING",
            (symbol.strip().upper(), asset_type, (name or "").strip(),
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def list_watchlist(asset_type: str | None = None) -> list[dict]:
    q = "SELECT * FROM watchlist"
    params: tuple = ()
    if asset_type:
        q += " WHERE asset_type = ?"
        params = (asset_type,)
    q += " ORDER BY symbol"
    with _connect() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def update_watchlist_alert(watch_id: int, target_above: float | None,
                           target_below: float | None, day_move_pct: float | None,
                           rsi_alert: bool):
    with _connect() as con:
        con.execute(
            "UPDATE watchlist SET target_above = ?, target_below = ?, "
            "day_move_pct = ?, rsi_alert = ? WHERE id = ?",
            (target_above, target_below, day_move_pct, 1 if rsi_alert else 0, watch_id),
        )


def set_watchlist_name(watch_id: int, name: str):
    name = (name or "").strip()
    if not name:
        return
    with _connect() as con:
        con.execute("UPDATE watchlist SET name = ? WHERE id = ?", (name, watch_id))


def remove_watchlist(watch_id: int):
    with _connect() as con:
        con.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))


# --- BUCHUNGSJOURNAL (manuelle Käufe und Verkäufe) ---

def record_trade(symbol: str, asset_type: str, side: str, quantity: float,
                 price_eur: float, category: str = "Standard", fees_eur: float = 0.0,
                 trade_date: str | None = None, note: str = "",
                 fund_from_cash: bool = False) -> int:
    """Bucht einen Kauf/Verkauf und aktualisiert die betroffene Position atomar.

    Käufe bilden den durchschnittlichen Einstand inklusive Gebühren neu. Bei
    Verkäufen bleibt der bisherige durchschnittliche Einstand erhalten.

    `fund_from_cash=True` bucht zusätzlich eine Cash-Gegenbuchung in `cash_log`
    (Kauf: -(Menge*Preis+Gebühren), Verkauf: +(Menge*Preis-Gebühren)) - ohne das
    würde ein aus dem getrackten Bank-Cash bezahlter Kauf das Gesamtvermögen
    künstlich erhöhen (Asset-Seite steigt, Cash-Seite bleibt unverändert). Der
    Zeitstempel der Cash-Buchung ist bewusst "jetzt", nicht `trade_date`:
    latest_cash_balance()/die Liquiditäts-Chart sortieren nach Einfüge-
    Reihenfolge (id) - ein rückdatierter Cash-Eintrag würde diese Annahme brechen.
    """
    side = side.lower().strip()
    quantity, price_eur, fees_eur = float(quantity), float(price_eur), float(fees_eur)
    if side not in {"buy", "sell"}:
        raise ValueError("side muss 'buy' oder 'sell' sein")
    if quantity <= 0 or price_eur < 0 or fees_eur < 0:
        raise ValueError("Menge muss positiv sein; Preis und Gebühren dürfen nicht negativ sein")
    category = category.strip() or "Standard"
    trade_date = trade_date or date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as con:
        asset_id = upsert_asset(con, symbol, asset_type)
        existing = con.execute(
            "SELECT id, quantity, buy_price_eur, source FROM positions "
            "WHERE asset_id = ? AND category = ?", (asset_id, category)
        ).fetchone()
        old_qty = float(existing["quantity"]) if existing else 0.0
        old_buy = float(existing["buy_price_eur"]) if existing else 0.0
        if side == "sell" and quantity > old_qty + 1e-10:
            raise ValueError(f"Für {symbol.strip().upper()} in „{category}“ sind nur {old_qty:g} verfügbar.")

        if side == "buy":
            new_qty = old_qty + quantity
            new_buy = (old_qty * old_buy + quantity * price_eur + fees_eur) / new_qty
        else:
            new_qty = max(0.0, old_qty - quantity)
            new_buy = old_buy

        if new_qty <= 1e-10:
            if existing:
                con.execute("DELETE FROM positions WHERE id = ?", (existing["id"],))
        elif existing:
            con.execute(
                "UPDATE positions SET quantity = ?, buy_price_eur = ?, source = 'manuell', updated_at = ? WHERE id = ?",
                (new_qty, new_buy, now, existing["id"]),
            )
        else:
            con.execute(
                "INSERT INTO positions (asset_id, quantity, buy_price_eur, category, source, updated_at) "
                "VALUES (?, ?, ?, ?, 'manuell', ?)",
                (asset_id, new_qty, new_buy, category, now),
            )
        cur = con.execute(
            "INSERT INTO transactions (created_at, trade_date, asset_id, category, side, quantity, price_eur, fees_eur, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, trade_date, asset_id, category, side, quantity, price_eur, fees_eur, note.strip()),
        )
        if fund_from_cash:
            gross = quantity * price_eur
            delta = -(gross + fees_eur) if side == "buy" else (gross - fees_eur)
            cash_row = con.execute(
                "SELECT balance_eur FROM cash_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            new_balance = (float(cash_row["balance_eur"]) if cash_row else 0.0) + delta
            con.execute(
                "INSERT INTO cash_log (created_at, balance_eur) VALUES (?, ?)",
                (now, new_balance),
            )
        return cur.lastrowid


def list_transactions(asset_type: str | None = None, limit: int = 200) -> list[dict]:
    query = (
        "SELECT t.id, t.trade_date, t.category, t.side, t.quantity, t.price_eur, t.fees_eur, t.note, "
        "a.symbol, a.name, a.asset_type FROM transactions t JOIN assets a ON a.id = t.asset_id "
    )
    params: tuple = ()
    if asset_type:
        query += "WHERE a.asset_type = ? "
        params = (asset_type,)
    query += "ORDER BY t.trade_date DESC, t.id DESC LIMIT ?"
    params += (int(limit),)
    with _connect() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# --- SNAPSHOTS ---

def save_snapshot(asset_type: str, total_value_eur: float, snap_date: str | None = None):
    d = snap_date or date.today().isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO snapshots (snap_date, asset_type, total_value_eur) VALUES (?, ?, ?) "
            "ON CONFLICT (snap_date, asset_type) DO UPDATE SET total_value_eur = excluded.total_value_eur",
            (d, asset_type, float(total_value_eur)),
        )


def list_snapshots() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT snap_date, asset_type, total_value_eur FROM snapshots ORDER BY snap_date"
        ).fetchall()
    return [dict(r) for r in rows]


# --- SENTIMENT-HISTORIE (Krypto-Markt-Temperatur) ---
# CoinGecko liefert Dominanz/Meme-Marktkap. nur als Momentanwert ohne eigene
# Historie - hier zeichnet die App selbst einen Verlauf auf (ein Wert pro Tag
# und Indikator, wie snapshots/save_snapshot).

def save_sentiment(indicator: str, value: float, snap_date: str | None = None):
    d = snap_date or date.today().isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO sentiment_history (snap_date, indicator, value) VALUES (?, ?, ?) "
            "ON CONFLICT (snap_date, indicator) DO UPDATE SET value = excluded.value",
            (d, indicator, float(value)),
        )


def list_sentiment(indicator: str | None = None, days: int | None = None) -> list[dict]:
    q = "SELECT snap_date, indicator, value FROM sentiment_history"
    conditions = []
    params: list = []
    if indicator is not None:
        conditions.append("indicator = ?")
        params.append(indicator)
    if days is not None:
        conditions.append("snap_date >= ?")
        params.append((date.today() - timedelta(days=days)).isoformat())
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY snap_date"
    with _connect() as con:
        rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]


# On-Chain-Historie (BTC MVRV/Puell/Mayer/STH-MVRV) - separat von sentiment_history,
# weil die Quelle (bitcoin-data.com) ein hartes Limit von 10 Anfragen/Stunde hat: die
# Vollhistorie wird einmalig geladen und lokal gehalten statt bei jedem Seitenaufruf
# neu abgefragt (siehe data/onchain.py).

def save_onchain(metric: str, rows: list[dict]):
    """rows: [{"d": "YYYY-MM-DD", "value": float}, ...] - Upsert pro (metric, d)."""
    if not rows:
        return
    with _connect() as con:
        con.executemany(
            "INSERT INTO onchain_history (metric, d, value) VALUES (?, ?, ?) "
            "ON CONFLICT (metric, d) DO UPDATE SET value = excluded.value",
            [(metric, r["d"], float(r["value"])) for r in rows],
        )


def list_onchain(metric: str, days: int | None = None) -> list[dict]:
    q = "SELECT d, value FROM onchain_history WHERE metric = ?"
    params: list = [metric]
    if days is not None:
        q += " AND d >= ?"
        params.append((date.today() - timedelta(days=days)).isoformat())
    q += " ORDER BY d"
    with _connect() as con:
        rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def onchain_bootstrapped(metric: str) -> bool:
    """True, sobald für die Metrik schon einmal Vollhistorie geladen wurde -
    verhindert einen erneuten teuren Bootstrap-Request nach einem Neustart."""
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM meta WHERE key = ?", (f"onchain_bootstrap:{metric}",)
        ).fetchone()
    return row is not None


def mark_onchain_bootstrapped(metric: str):
    set_meta(f"onchain_bootstrap:{metric}", datetime.now().isoformat(timespec="seconds"))


# --- AGENTEN-HISTORIE ---

def log_agent_run(target: str, mode: str, total_score: int | None,
                  recommendation: str, cost_usd: float, report: dict) -> int:
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO agent_runs (created_at, target, mode, total_score, recommendation, cost_usd, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), target, mode,
             total_score, recommendation, cost_usd, json.dumps(report, ensure_ascii=False, default=str)),
        )
        return cur.lastrowid


def list_agent_runs(limit: int = 50) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def total_agent_cost() -> float:
    with _connect() as con:
        row = con.execute("SELECT SUM(cost_usd) AS s FROM agent_runs").fetchone()
    return float(row["s"] or 0.0)


# --- META (Key/Value) ---

def set_meta(key: str, value: str):
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )


def get_meta(key: str) -> str | None:
    with _connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def delete_meta(key: str):
    with _connect() as con:
        con.execute("DELETE FROM meta WHERE key = ?", (key,))


# --- BACKUP & RESTORE ---

_SQLITE_HEADER = b"SQLite format 3\x00"


def backup_bytes() -> bytes:
    """Konsistente Kopie der aktiven DB als Bytes (sqlite3-Backup-API).

    Die Backup-API kopiert transaktionssicher - auch wenn parallel geschrieben
    würde. Für den Download-Button in den Einstellungen.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "backup.db"
        with _connect() as src:
            dest = sqlite3.connect(target)
            try:
                src.backup(dest)
            finally:
                dest.close()
        return target.read_bytes()


def restore_from_bytes(data: bytes):
    """Aktive DB durch ein hochgeladenes Backup ersetzen.

    Validiert den SQLite-Header, legt die bisherige DB als
    `<name>.bak-<timestamp>` daneben und ersetzt die Datei dann atomar.
    Wirft ValueError bei ungültigen Daten - die bestehende DB bleibt
    in dem Fall unangetastet.
    """
    import os

    if not data or not data.startswith(_SQLITE_HEADER):
        raise ValueError("Die Datei ist keine gültige SQLite-Datenbank.")

    db_path = config.DB_PATH
    if db_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        backup_path.write_bytes(db_path.read_bytes())

    tmp_path = db_path.with_name(db_path.name + ".restore-tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, db_path)


# --- SCHATTEN-PORTFOLIO (je scope = 'crypto' | 'stock' ein eigenes Experiment) ---

def shadow_positions(scope: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT symbol, asset_type, quantity FROM shadow_positions "
            "WHERE scope = ? AND (quantity > 0 OR asset_type = 'cash') "
            "ORDER BY asset_type, symbol",
            (scope,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_shadow_position(scope: str, symbol: str, asset_type: str, quantity: float):
    """Setzt die Menge absolut. Menge <= 0 löscht die Position (außer Cash)."""
    symbol = symbol.strip().upper()
    with _connect() as con:
        if quantity <= 0 and asset_type != "cash":
            con.execute(
                "DELETE FROM shadow_positions WHERE scope = ? AND symbol = ? AND asset_type = ?",
                (scope, symbol, asset_type),
            )
            return
        con.execute(
            "INSERT INTO shadow_positions (scope, symbol, asset_type, quantity) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope, symbol, asset_type) DO UPDATE SET quantity = excluded.quantity",
            (scope, symbol, asset_type, float(quantity)),
        )


def get_shadow_quantity(scope: str, symbol: str, asset_type: str) -> float:
    with _connect() as con:
        row = con.execute(
            "SELECT quantity FROM shadow_positions WHERE scope = ? AND symbol = ? AND asset_type = ?",
            (scope, symbol.strip().upper(), asset_type),
        ).fetchone()
    return float(row["quantity"]) if row else 0.0


def clear_shadow(scope: str):
    """Löscht das Schatten-Portfolio-Experiment EINES Scopes."""
    with _connect() as con:
        con.execute("DELETE FROM shadow_positions WHERE scope = ?", (scope,))
        con.execute("DELETE FROM shadow_log WHERE scope = ?", (scope,))
        con.execute("DELETE FROM shadow_snapshots WHERE scope = ?", (scope,))
        con.execute("DELETE FROM recommendations WHERE scope = ?", (scope,))
        con.execute("DELETE FROM meta WHERE key = ?", (f"shadow_start_{scope}",))


def save_shadow_snapshot(scope: str, total_value_eur: float, snap_date: str | None = None):
    d = snap_date or date.today().isoformat()
    with _connect() as con:
        con.execute(
            "INSERT INTO shadow_snapshots (scope, snap_date, total_value_eur) VALUES (?, ?, ?) "
            "ON CONFLICT (snap_date, scope) DO UPDATE SET total_value_eur = excluded.total_value_eur",
            (scope, d, float(total_value_eur)),
        )


def list_shadow_snapshots(scope: str) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT snap_date, total_value_eur FROM shadow_snapshots "
            "WHERE scope = ? ORDER BY snap_date",
            (scope,),
        ).fetchall()
    return [dict(r) for r in rows]


def shadow_turnover(scope: str) -> dict:
    """Anzahl und EUR-Volumen der tatsaechlich ausgefuehrten Trades (ohne
    'halten') seit Start des Experiments - shadow_log wird bei reset()/
    clear_shadow() geleert, jede vorhandene Zeile gehoert also zum laufenden
    Experiment, keine Datums-Filterung noetig."""
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(wert_eur), 0) AS vol "
            "FROM shadow_log WHERE scope = ? AND aktion != 'halten'",
            (scope,),
        ).fetchone()
    return {"trade_count": row["cnt"], "volume_eur": float(row["vol"])}


def add_shadow_log(scope: str, aktion: str, von_symbol: str | None, nach_symbol: str | None,
                   menge_von: float | None, menge_nach: float | None,
                   kurs_von_eur: float | None, kurs_nach_eur: float | None,
                   wert_eur: float | None, recommendation_id: int | None = None,
                   notiz: str = ""):
    with _connect() as con:
        con.execute(
            "INSERT INTO shadow_log (scope, created_at, aktion, von_symbol, nach_symbol, "
            "menge_von, menge_nach, kurs_von_eur, kurs_nach_eur, wert_eur, "
            "recommendation_id, notiz) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, datetime.now().isoformat(timespec="seconds"), aktion, von_symbol, nach_symbol,
             menge_von, menge_nach, kurs_von_eur, kurs_nach_eur, wert_eur,
             recommendation_id, notiz),
        )


def list_shadow_log(scope: str, limit: int = 200) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM shadow_log WHERE scope = ? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_recommendation(scope: str, run_id: int | None, aktion: str, symbol: str | None,
                       asset_type: str | None, ziel_symbol: str | None,
                       ziel_asset_type: str | None, anteil_pct: float | None,
                       begruendung: str, kurs_symbol_eur: float | None,
                       kurs_ziel_eur: float | None, angewendet: bool) -> int:
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO recommendations (scope, run_id, created_at, aktion, symbol, asset_type, "
            "ziel_symbol, ziel_asset_type, anteil_pct, begruendung, kurs_symbol_eur, "
            "kurs_ziel_eur, angewendet) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, run_id, datetime.now().isoformat(timespec="seconds"), aktion, symbol, asset_type,
             ziel_symbol, ziel_asset_type, anteil_pct, begruendung, kurs_symbol_eur,
             kurs_ziel_eur, 1 if angewendet else 0),
        )
        return cur.lastrowid


def list_recommendations(scope: str, limit: int = 200) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM recommendations WHERE scope = ? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --- CASH (Bankkonto) ---

def add_cash_entry(balance_eur: float) -> int:
    """Neuen Kontostand festhalten - jeder Eintrag ist ein Verlaufs-Datenpunkt."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO cash_log (created_at, balance_eur) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), float(balance_eur)),
        )
        return cur.lastrowid


def list_cash_entries() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT id, created_at, balance_eur FROM cash_log ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def latest_cash_balance() -> float | None:
    with _connect() as con:
        row = con.execute(
            "SELECT balance_eur FROM cash_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return float(row["balance_eur"]) if row else None


def delete_last_cash_entry():
    """Letzten Eintrag entfernen (Fehleingabe-Korrektur)."""
    with _connect() as con:
        con.execute(
            "DELETE FROM cash_log WHERE id = (SELECT MAX(id) FROM cash_log)"
        )


# --- EIN- UND AUSZAHLUNGEN (für die bereinigte Performance) ---

def add_cashflow(amount_eur: float, flow_date: str | None = None, note: str = "") -> int:
    """Erfasst extern zugeflossenes oder entnommenes Kapital.

    Positive Beträge sind Einzahlungen, negative Beträge Auszahlungen. Diese
    Buchungen verändern keinen Depotbestand; sie dienen ausschließlich dazu,
    die Performance um eigenes Kapital zu bereinigen.
    """
    d = flow_date or date.today().isoformat()
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO portfolio_cashflows (flow_date, amount_eur, note, created_at) "
            "VALUES (?, ?, ?, ?)",
            (d, float(amount_eur), note.strip(), datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def list_cashflows(limit: int | None = None) -> list[dict]:
    query = "SELECT id, flow_date, amount_eur, note, created_at FROM portfolio_cashflows ORDER BY flow_date DESC, id DESC"
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (int(limit),)
    with _connect() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_cashflow(cashflow_id: int):
    with _connect() as con:
        con.execute("DELETE FROM portfolio_cashflows WHERE id = ?", (cashflow_id,))


# --- KRYPTO-WERTVERLAUF (aus Kraken-Ledger rekonstruiert) ---

def replace_kraken_value_history(rows: list[tuple[str, float]]):
    """Ersetzt die komplette rekonstruierte Verlaufsreihe (Liste (snap_date, value_eur))."""
    with _connect() as con:
        con.execute("DELETE FROM kraken_value_history")
        con.executemany(
            "INSERT INTO kraken_value_history (snap_date, value_eur) VALUES (?, ?)",
            [(d, float(v)) for d, v in rows],
        )


def list_kraken_value_history() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT snap_date, value_eur FROM kraken_value_history ORDER BY snap_date"
        ).fetchall()
    return [dict(r) for r in rows]


# --- TRADING-BOT (core/bot.py) ---
# Eine offene Position je Symbol (upsert wie set_shadow_position), geschlossene
# Positionen bleiben als eigene Zeile stehen (status='closed') - lückenloser
# Verlauf statt Überschreiben, gleiche Denkweise wie shadow_log gegenüber
# shadow_positions.

def get_bot_position(symbol: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM bot_positions WHERE symbol = ? AND status = 'open'",
            (symbol.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def list_open_bot_positions() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_positions WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_open_bot_position(symbol: str, side: str, size: float, entry_px: float,
                             leverage: float, stop_px: float | None,
                             exchange_oid: str | None, opened_at: str | None = None,
                             origin: str = "bot", origin_note: str | None = None,
                             decision_id: int | None = None,
                             exchange_opened_at: str | None = None,
                             strategy_version: str | None = None,
                             config_hash: str | None = None,
                             exchange_stop_px: float | None = None) -> int:
    """Neue Position anlegen, oder - falls bereits eine offene Position im
    selben Symbol existiert (Nachkauf) - Menge/Einstand/Stop dort aktualisieren
    statt eine zweite Zeile zu erzeugen. `opened_at` bleibt beim Nachkauf der
    ursprüngliche Eröffnungszeitpunkt.

    `origin`/`origin_note`/`decision_id`/`exchange_opened_at`/`strategy_version`/
    `config_hash` werden NUR beim Anlegen gesetzt: ein Nachkauf ändert nicht,
    unter welcher Strategie/Konfiguration eine Position ursprünglich entstand.
    Der Default 'bot' hält bestehende Aufrufer und Tests unverändert lauffähig.

    `strategy_version`/`config_hash` (core.bot_signals.STRATEGY_VERSION bzw.
    core.bot_config.config_fingerprint()) fehlten bislang komplett auf
    bot_positions - eine spätere Auswertung "wie liefen Positionen unter
    Konfiguration X" war dadurch nicht möglich, obwohl bot_signal_log genau
    das schon für einzelne Signale festhält."""
    symbol = symbol.strip().upper()
    confirmed_stop = stop_px if exchange_stop_px is None else exchange_stop_px
    with _connect() as con:
        existing = con.execute(
            "SELECT id FROM bot_positions WHERE symbol = ? AND status = 'open'", (symbol,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE bot_positions SET side = ?, size = ?, entry_px = ?, leverage = ?, "
                "stop_px = ?, exchange_stop_px = ?, exchange_oid = ? WHERE id = ?",
                (side, float(size), float(entry_px), float(leverage),
                 stop_px, confirmed_stop, exchange_oid, existing["id"]),
            )
            return existing["id"]
        cur = con.execute(
            "INSERT INTO bot_positions (symbol, side, size, entry_px, leverage, stop_px, "
            "exchange_stop_px, exchange_oid, opened_at, status, origin, origin_note, decision_id, "
            "exchange_opened_at, initial_stop_px, strategy_version, config_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, float(size), float(entry_px), float(leverage),
             stop_px, confirmed_stop, exchange_oid, opened_at or clock.iso_utc(),
             origin, origin_note, decision_id, exchange_opened_at,
             # NUR beim Anlegen gesetzt, danach nie wieder - dieselbe
             # UNVERAENDERLICHKEITs-Garantie wie bei origin/decision_id.
             stop_px, strategy_version, config_hash),
        )
        return cur.lastrowid


def set_bot_position_exchange_opened_at(position_id: int, exchange_opened_at: str) -> bool:
    """Einmaliger NACHTRAEGLICHER Backfill fuer bereits laengst uebernommene
    Positionen, deren exchange_opened_at beim Uebernehmen noch nicht ermittelt
    werden konnte (Feature kam erst spaeter dazu). Bewusst nur bei aktuell
    NULL wirksam (WHERE exchange_opened_at IS NULL) - dieselbe
    Unveraenderlichkeits-Garantie wie beim Setzen in upsert_open_bot_position:
    ein bereits bekannter echter Wert darf nie ueberschrieben werden."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE bot_positions SET exchange_opened_at = ? "
            "WHERE id = ? AND exchange_opened_at IS NULL",
            (exchange_opened_at, position_id),
        )
        return cur.rowcount > 0


def close_bot_position(symbol: str, close_px: float, realized_pnl_usd: float,
                       closed_at: str | None = None):
    """`realized_pnl_usd` ist der PnL DIESES letzten (Rest-)Schlusses -
    `partial_realized_pnl_usd` (aus etwaigen frueheren Teilschliessungen
    derselben Position, siehe reduce_bot_position_size()) wird hier
    hinzuaddiert, sonst wuerde ein frueherer Teil-Gewinn/-Verlust beim
    finalen Schluss der Restgroesse verschwinden. Der uebergebene PnL darf
    deshalb NUR den Rest enthalten - nie Fills, die schon in
    partial_realized_pnl_usd stecken (siehe core.bot._actual_close_from_fills,
    das per remaining_size nur die Fills des offenen Rests auswaehlt)."""
    with _connect() as con:
        con.execute(
            "UPDATE bot_positions SET status = 'closed', closed_at = ?, close_px = ?, "
            "realized_pnl_usd = partial_realized_pnl_usd + ? WHERE symbol = ? AND status = 'open'",
            (closed_at or clock.iso_utc(),
             float(close_px), float(realized_pnl_usd), symbol.strip().upper()),
        )


def reduce_bot_position_size(symbol: str, new_size: float, add_realized_pnl_usd: float):
    """Teilschliessung: die Boerse hat nur einen Teil der angeforderten
    Schliess-Order gefuellt (core.bot.close_position()). Die Position bleibt
    OFFEN mit der Restgroesse - `entry_px`/`stop_px` bleiben unveraendert,
    weil sie sich auf die verbleibenden Einheiten weiterhin unveraendert
    beziehen. Der Bruttogewinn/-verlust der bereits gefuellten Teilmenge wird
    in `partial_realized_pnl_usd` akkumuliert (mehrere Teilschliessungen
    hintereinander sind damit ebenfalls korrekt), nicht sofort in
    `realized_pnl_usd` geschrieben - die Position ist ja noch nicht
    geschlossen."""
    with _connect() as con:
        con.execute(
            "UPDATE bot_positions SET size = ?, "
            "partial_realized_pnl_usd = partial_realized_pnl_usd + ? "
            "WHERE symbol = ? AND status = 'open'",
            (float(new_size), float(add_realized_pnl_usd), symbol.strip().upper()),
        )


def set_bot_position_size(symbol: str, size: float):
    """Groesse einer offenen Position direkt auf einen von der Boerse
    bestaetigten Wert setzen - fuer core.bot.reconcile_with_exchange()'s
    Groessen-Abgleich bei einer manuellen Teilschliessung/Aufstockung
    AUSSERHALB des Bots. Anders als reduce_bot_position_size() wird HIER kein
    realisierter PnL gebucht - der ist bei einer Fremdaenderung nicht aus der
    DB rekonstruierbar, nur die Grunddaten muessen wieder stimmen, bevor
    Heat-/Exposure-/Trailing-Berechnungen im selben Takt eine veraltete
    Groesse lesen."""
    with _connect() as con:
        con.execute(
            "UPDATE bot_positions SET size = ? WHERE symbol = ? AND status = 'open'",
            (float(size), symbol.strip().upper()),
        )


def update_bot_position_stop(symbol: str, stop_px: float, exchange_oid: str | None = None,
                             exchange_stop_px: float | None = None):
    """Nachgezogenen Stop speichern (core.bot.update_trailing_stops).

    ``stop_px`` ist der lokale, angeforderte Schutz. ``exchange_stop_px``
    wird nur nach einem bestaetigten Exchange-Update geschrieben; damit wird
    ein fehlgeschlagener API-Call im naechsten Takt erneut versucht.
    """
    with _connect() as con:
        if exchange_oid is None and exchange_stop_px is None:
            con.execute(
                "UPDATE bot_positions SET stop_px = ? WHERE symbol = ? AND status = 'open'",
                (float(stop_px), symbol.strip().upper()),
            )
        elif exchange_oid is None:
            con.execute(
                "UPDATE bot_positions SET stop_px = ?, exchange_stop_px = ? "
                "WHERE symbol = ? AND status = 'open'",
                (float(stop_px), float(exchange_stop_px), symbol.strip().upper()),
            )
        else:
            con.execute(
                "UPDATE bot_positions SET stop_px = ?, exchange_stop_px = COALESCE(?, exchange_stop_px), "
                "exchange_oid = ? "
                "WHERE symbol = ? AND status = 'open'",
                (float(stop_px), exchange_stop_px, exchange_oid, symbol.strip().upper()),
            )


def recent_closed_bot_positions(limit: int = 10, origin: str = "bot") -> list[dict]:
    """Zuletzt geschlossene Bot-Positionen, JUENGSTE ZUERST nach closed_at
    (nicht nach id/opened_at - zwei Positionen koennen in anderer Reihenfolge
    schliessen als sie eroeffnet wurden) - Grundlage fuer
    core.bot_guards.check_loss_streak(). Default origin='bot': eine
    uebernommene oder manuell eroeffnete Position sagt nichts ueber die
    Signal-Engine aus und wuerde die Verlustserie faelschlich zurechnen."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_positions WHERE status = 'closed' AND origin = ? "
            "ORDER BY closed_at DESC LIMIT ?", (origin, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def last_closed_bot_position_at(symbol: str) -> str | None:
    with _connect() as con:
        row = con.execute(
            "SELECT MAX(closed_at) AS at FROM bot_positions WHERE symbol = ? AND status = 'closed'",
            (symbol.strip().upper(),),
        ).fetchone()
    return row["at"] if row and row["at"] else None


# --- SIGNAL-RESET (Phase 3): Wiedereinstieg ueber Setup-Zustand, nicht nur
# Zeit - siehe Tabellenkommentar in _SCHEMA und core.bot._reentry_check(). ---

def get_bot_symbol_state(symbol: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM bot_symbol_state WHERE symbol = ?", (symbol.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


def disarm_bot_symbol(symbol: str, side: str, disarmed_at: str | None = None):
    """Bei JEDEM Schluss aufgerufen (core.bot.close_position() und der
    Phantom-Schluss-Zweig in reconcile_with_exchange()) - UPSERT, da das
    Symbol beim allerersten Schluss noch keine Zeile hat."""
    with _connect() as con:
        con.execute(
            "INSERT INTO bot_symbol_state (symbol, armed, disarmed_at, last_side) "
            "VALUES (?, 0, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET armed = 0, disarmed_at = excluded.disarmed_at, "
            "last_side = excluded.last_side",
            (symbol.strip().upper(), disarmed_at or clock.iso_utc(), side),
        )


def arm_bot_symbol(symbol: str):
    """Scharf stellen - nur core.bot._reentry_check() ruft das auf, sobald
    Mindestwartezeit UND 'Setup nachweislich weg' beide erfuellt sind.
    disarmed_at/last_side bleiben als Verlauf stehen (nicht geloescht), damit
    sichtbar bleibt, wann/warum das Symbol zuletzt gesperrt war."""
    with _connect() as con:
        con.execute(
            "INSERT INTO bot_symbol_state (symbol, armed) VALUES (?, 1) "
            "ON CONFLICT(symbol) DO UPDATE SET armed = 1",
            (symbol.strip().upper(),),
        )


def add_bot_order(symbol: str, intent: str, side: str | None, size: float | None,
                  order_type: str | None, requested_px: float | None, status: str,
                  exchange_oid: str | None = None, fill_px: float | None = None,
                  fee_usd: float | None = None, error: str | None = None,
                  decision_id: int | None = None, position_id: int | None = None) -> int:
    """Jeder Orderversuch wird geloggt - AUCH ein von core.bot_guards blockierter
    (status='blocked', kein exchange_oid/fill_px) - lückenloser Audit-Trail,
    unabhängig davon, ob tatsächlich gehandelt wurde."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO bot_orders (created_at, symbol, intent, side, size, order_type, "
            "requested_px, status, exchange_oid, fill_px, fee_usd, error, decision_id, "
            "position_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (clock.iso_utc(), symbol.strip().upper(), intent, side,
             size, order_type, requested_px, status, exchange_oid, fill_px, fee_usd, error,
             decision_id, position_id),
        )
        return cur.lastrowid


# --- SIGNAL-LOG (Datensammlung, siehe Tabellenkommentar in _SCHEMA) ---

_SIGNAL_LOG_FIELDS = (
    "ts", "candle_ts", "symbol", "side", "score", "long_score", "short_score",
    "components_json", "readings_json", "price", "atr_pct", "stop_pct",
    "funding_hourly", "threshold", "risk_level", "strategy_version", "config_hash",
    "action", "block_reason", "decision_id", "position_id",
)


_SIGNAL_LOG_CONFLICT_KEY = ("symbol", "candle_ts", "strategy_version", "risk_level")
_SIGNAL_LOG_UPDATE_FIELDS = tuple(
    f for f in _SIGNAL_LOG_FIELDS if f not in _SIGNAL_LOG_CONFLICT_KEY)


def add_bot_signal_logs(rows: list[dict]) -> int:
    """Kompletten Scan in EINER Transaktion schreiben statt einer Verbindung
    je Kandidat - der Bot-Runner teilt sich die SQLite-Datei mit Streamlit
    (WAL + busy_timeout, siehe _connect), da soll ein Takt nicht zehn
    Schreib-Fenster oeffnen.

    UPSERT statt reinem INSERT OR IGNORE: derselbe (symbol, candle_ts,
    strategy_version, risk_level) kann innerhalb einer Stunde mehrfach
    ankommen (vier 15-Minuten-Takte, eine Kerze) - idx_bot_signal_log_dedup
    soll die Wiederholungen verwerfen, NICHT aber einen echten Trade. Score
    und Zustand koennen sich zwischen zwei Takten DERSELBEN Kerze aendern
    (live_price, freie Positionsplaetze, ein zwischenzeitlich freigewordener
    Slot senkt die Schwelle) - ein Kandidat, der beim ersten Takt noch
    'skipped' war, kann beim naechsten Takt tatsaechlich eroeffnet werden.
    Ein reines OR IGNORE haette diese zweite, wichtigere Zeile stillschweigend
    verworfen und den echten Trade dauerhaft als 'skipped' verzeichnet.
    DO UPDATE greift deshalb NUR, wenn die neue Zeile 'opened' ist und die
    bestehende es noch nicht war - einmal 'opened' bleibt 'opened' (wird
    durch einen spaeteren, weniger aussagekraeftigen Takt nicht wieder
    ueberschrieben), und zwei nicht-'opened'-Wiederholungen bleiben weiterhin
    eine einzige Zeile. outcome_json/outcome_last_h bleiben unberuehrt (nicht
    Teil von _SIGNAL_LOG_FIELDS) - ein spaeterer Nachtrag geht nie verloren.
    Gibt die Zahl der TATSAECHLICH neu geschriebenen ODER aktualisierten
    Zeilen zurueck (nicht die Anzahl der Versuche)."""
    if not rows:
        return 0
    cols = ", ".join(_SIGNAL_LOG_FIELDS)
    marks = ", ".join("?" for _ in _SIGNAL_LOG_FIELDS)
    conflict_cols = ", ".join(_SIGNAL_LOG_CONFLICT_KEY)
    update_set = ", ".join(f"{f} = excluded.{f}" for f in _SIGNAL_LOG_UPDATE_FIELDS)
    payload = [tuple(r.get(f) for f in _SIGNAL_LOG_FIELDS) for r in rows]
    with _connect() as con:
        before = con.total_changes
        con.executemany(
            f"INSERT INTO bot_signal_log ({cols}) VALUES ({marks}) "
            f"ON CONFLICT({conflict_cols}) DO UPDATE SET {update_set} "
            f"WHERE excluded.action = 'opened' AND bot_signal_log.action != 'opened'",
            payload)
        return con.total_changes - before


def list_bot_signal_log_pending(max_done_h: int, before_ts: str,
                                limit: int = 200) -> list[dict]:
    """Zeilen, deren Ergebnis-Fenster `max_done_h` abgelaufen ist und fuer die
    noch nichts nachgetragen wurde. Der Aufrufer geht die Horizonte
    aufsteigend durch (6/12/24/48/96 h) und liefert je Horizont den passenden
    `before_ts` - so bleibt die Auswahl eine einfache, indizierte Abfrage."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_signal_log WHERE outcome_last_h < ? AND ts <= ? "
            "ORDER BY ts LIMIT ?",
            (int(max_done_h), before_ts, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def set_bot_signal_outcome(row_id: int, outcome_json: str, outcome_last_h: int):
    with _connect() as con:
        con.execute(
            "UPDATE bot_signal_log SET outcome_json = ?, outcome_last_h = ? WHERE id = ?",
            (outcome_json, int(outcome_last_h), int(row_id)),
        )


def list_bot_signal_log(limit: int = 500, symbol: str | None = None) -> list[dict]:
    with _connect() as con:
        if symbol:
            rows = con.execute(
                "SELECT * FROM bot_signal_log WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol.strip().upper(), int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM bot_signal_log ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
    return [dict(r) for r in rows]


def count_bot_signal_log() -> dict:
    """Zeilen gesamt und davon mit vollstaendig nachgetragenem Ergebnis -
    fuer die Anzeige "wie weit ist die Datensammlung"."""
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN outcome_last_h >= 96 THEN 1 ELSE 0 END) AS complete "
            "FROM bot_signal_log"
        ).fetchone()
    return {"total": int(row["total"] or 0), "complete": int(row["complete"] or 0)}


def bot_position_fees_usd(position_id: int) -> float:
    """Summe aller tatsaechlich gezahlten Gebuehren EINER Position (Eroeffnung,
    Nachkaeufe, Schliessung) - Grundlage fuer ein NETTO realisiertes Ergebnis.

    Solange bot_positions.realized_pnl_usd brutto blieb, konnten Trade-Historie
    und performance_summary (das auf der ohnehin netto gefuehrten Equity
    rechnet) nie uebereinstimmen, und ein Trade, dessen Kursgewinn kleiner als
    die Round-Trip-Gebuehr war, erschien in der Historie als Gewinn."""
    with _connect() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(fee_usd), 0.0) AS fees FROM bot_orders "
            "WHERE position_id = ? AND status = 'filled'",
            (position_id,),
        ).fetchone()
    return float(row["fees"] or 0.0)


def set_bot_order_position(order_id: int, position_id: int):
    with _connect() as con:
        con.execute("UPDATE bot_orders SET position_id = ? WHERE id = ?", (position_id, order_id))


def count_bot_orders_today(status: str = "filled", day: str | None = None,
                          intents: tuple[str, ...] | None = None) -> int:
    """`intents=None` zaehlt JEDE passende Order (Rueckwaertskompatibilitaet).
    core.bot.trades_today() grenzt bewusst auf echte Handels-Intents ein
    (open_long/open_short/close) - ohne das zaehlten auch
    exchange_adopt/exchange_reconciled_close-Zeilen (Reconciliation, kein
    eigener Trade) gegen das Tageslimit MAX_TRADES_PER_DAY, sodass wenige
    Uebernahmen/Phantom-Schluesse das Einstiegsbudget des Tages ohne einen
    einzigen echten Trade aufbrauchen konnten."""
    day = day or clock.now_utc().date().isoformat()
    query = "SELECT COUNT(*) AS cnt FROM bot_orders WHERE status = ? AND created_at LIKE ?"
    params: list = [status, f"{day}%"]
    if intents:
        query += f" AND intent IN ({','.join('?' for _ in intents)})"
        params.extend(intents)
    with _connect() as con:
        row = con.execute(query, params).fetchone()
    return int(row["cnt"])


def list_bot_orders(limit: int = 200) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_bot_decision(cycle_type: str, signals_json: str | None, run_id: int | None,
                     action: str | None, executed: bool, reason: str | None = None) -> int:
    """`config_hash` wird HIER (nicht von den ~18 Aufrufstellen in core.bot/
    agents.trader) mit der GERADE wirksamen Konfiguration befuellt - nach dem
    Vorbild von `ts`, das ebenfalls intern defaultet statt jeden Aufrufer zu
    zwingen, seine eigene Zeit mitzubringen. Lazy-Import wie in
    core.bot_config.live_trading_allowed(), um den Zirkelimport zu vermeiden
    (core.bot_config importiert bereits core.db) - UND bewusst VOR dem
    `with _connect()` unten aufgeloest: config_fingerprint() oeffnet selbst
    eine Verbindung (ueber bot_limits()/risk_level()), die vor dem INSERT
    hier vollstaendig abgeschlossen sein soll, statt zwei Verbindungen
    ineinander verschachtelt offen zu halten."""
    from core import bot_config
    config_hash = bot_config.config_fingerprint()
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO bot_decisions (created_at, cycle_type, signals_json, run_id, action, "
            "executed, reason, config_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (clock.iso_utc(), cycle_type, signals_json, run_id,
             action, 1 if executed else 0, reason, config_hash),
        )
        return cur.lastrowid


def list_bot_decisions(limit: int = 200) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_bot_decision(decision_id: int, executed: bool, reason: str | None = None):
    """Der Aufrufer (agents/trader.py) legt eine Entscheidung VOR der
    Ausführung an (damit der resultierende bot_orders-Eintrag per decision_id
    zurückverweisen kann, siehe core.bot.open_position/close_position) und
    trägt das tatsächliche Ergebnis erst danach nach."""
    with _connect() as con:
        if reason is not None:
            con.execute("UPDATE bot_decisions SET executed = ?, reason = ? WHERE id = ?",
                       (1 if executed else 0, reason, decision_id))
        else:
            con.execute("UPDATE bot_decisions SET executed = ? WHERE id = ?",
                       (1 if executed else 0, decision_id))


def list_bot_positions(limit: int = 200) -> list[dict]:
    """ALLE Positionen (offen + geschlossen), neueste zuerst - anders als
    list_open_bot_positions() für die Trade-Historie (agents/trader.py),
    die auch vergangene, bereits realisierte Trades zeigen muss."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_positions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_bot_equity_point(equity_usd: float, equity_eur: float, cash_usd: float | None,
                         fees_cum_usd: float | None, funding_cum_usd: float | None,
                         net_flows_usd: float | None = None,
                         ts: str | None = None) -> int:
    """Bewusst OHNE UNIQUE(Tag) - anders als snapshots/shadow_snapshots braucht
    der Bot Intraday-Auflösung (Tageswert reicht für einen 15-Minuten-Zyklus
    nicht, um z.B. den Tagesverlust-Guard sinnvoll zu berechnen)."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO bot_equity (ts, equity_usd, equity_eur, cash_usd, fees_cum_usd, "
            "funding_cum_usd, net_flows_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts or clock.iso_utc(), float(equity_usd),
             float(equity_eur), cash_usd, fees_cum_usd, funding_cum_usd, net_flows_usd),
        )
        return cur.lastrowid


def latest_bot_equity() -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM bot_equity ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def first_bot_equity_of_day(day: str | None = None) -> dict | None:
    day = day or clock.now_utc().date().isoformat()
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM bot_equity WHERE ts LIKE ? ORDER BY id ASC LIMIT 1", (f"{day}%",)
        ).fetchone()
    return dict(row) if row else None


def first_bot_equity() -> dict | None:
    """Der allererste Punkt des aktuellen Bot-Experiments (von initialize()
    im selben Atemzug wie der Start-Anker geschrieben) - genauerer Zeitstempel
    als ein reines Kalenderdatum, siehe core.bot._cost_window_since_ms()."""
    with _connect() as con:
        row = con.execute("SELECT * FROM bot_equity ORDER BY id ASC LIMIT 1").fetchone()
    return dict(row) if row else None


def list_bot_equity(limit: int | None = None) -> list[dict]:
    query = "SELECT * FROM bot_equity ORDER BY ts"
    params: tuple = ()
    if limit is not None:
        query = "SELECT * FROM (SELECT * FROM bot_equity ORDER BY id DESC LIMIT ?) ORDER BY ts"
        params = (int(limit),)
    with _connect() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def add_bot_walkforward_run(window_days: int, num_windows: int, risk_level: int,
                            gate_ok: bool, gate_reasons: list[str] | None,
                            pooled: dict | None, validation_hash: str | None) -> int:
    """Protokolliert JEDEN Walk-Forward-Lauf, bestanden UND durchgefallen -
    siehe Tabellenkommentar in _SCHEMA fuer das Warum. Ueberlebt
    clear_bot_state() bewusst (kein Bot-Live-Zustand, sondern
    Strategie-Validierungshistorie)."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO bot_walkforward_runs (created_at, window_days, num_windows, "
            "risk_level, gate_ok, gate_reasons_json, pooled_json, validation_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (clock.iso_utc(), int(window_days), int(num_windows), int(risk_level),
             1 if gate_ok else 0,
             json.dumps(gate_reasons or [], ensure_ascii=False),
             json.dumps(pooled, default=str, ensure_ascii=False) if pooled else None,
             validation_hash),
        )
        return cur.lastrowid


def list_bot_walkforward_runs(limit: int = 50) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM bot_walkforward_runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_bot_state():
    """Löscht den KOMPLETTEN Bot-Zustand (Positionen, Orders, Entscheidungen,
    Equity-Verlauf, Signal-Log) sowie Start-/Kill-Switch-Meta - explizite
    Nutzeraktion (views/trading_bot.py, Phase E), passiert nie automatisch.
    Analog zu shadow.reset()/clear_shadow().

    bot_signal_log wird seit dem Umbau auf Strategie V2 (4h-Swing) MIT
    geloescht, anders als zuvor: die alte Ausnahme ("reine Beobachtungs-
    datensammlung, ueberlebt Resets") galt fuer 1430 Zeilen, die alle unter
    strategy_version="1.0" liefen, aber ueber mehrere tatsaechlich
    verschiedene Konfigurationszustaende (Risikoregler-Overrides kamen und
    gingen) - die Historie war dadurch bereits vermischt und als Lern-
    grundlage nicht mehr sauber nutzbar. Ein kompletter Neustart mit V2 ist
    ehrlicher als eine Tabelle, die zwei Strategien ununterscheidbar
    vermengt. decision_id/position_id muessen deshalb nicht mehr gekappt
    werden - die Zeilen, auf die sie zeigten, sind ohnehin weg."""
    with _connect() as con:
        for table in ("bot_positions", "bot_orders", "bot_decisions", "bot_equity",
                      "bot_signal_log", "bot_symbol_state"):
            con.execute(f"DELETE FROM {table}")
        for key in ("bot_start", "bot_kill_switch", "bot_kill_switch_reason",
                   "bot_kill_switch_at", "bot_fees_cum_usd", "bot_funding_cum_usd",
                   "bot_net_flows_usd", "bot_session_start_usd", "bot_session_start_at",
                   "bot_equity_floor_hits", "bot_equity_untrusted_at",
                   "bot_last_llm_review_date", "bot_last_llm_review_at",
                   "bot_runner_heartbeat", "bot_runner_mode", "bot_runner_pid",
                   "bot_runner_stop_requested", "bot_close_all_requested", "bot_runner_last_error",
                   "bot_runner_last_error_at", "bot_last_preflight_ok_at",
                   "bot_last_cost_refresh_date", "bot_phase_f_verified_at", "bot_dry_run",
                   "bot_llm_veto", "bot_first_live_order_done", "bot_llm_cost_day",
                   "bot_llm_cost_usd", "bot_last_decision_candle",
                   # Beide fehlten hier: ein tz-naiver Altwert unter
                   # bot_last_flows_refresh_at ueberlebte dadurch einen Reset
                   # und liess core.bot_runner._maybe_refresh_flows() bei
                   # JEDEM Live-Takt mit "can't subtract offset-naive and
                   # offset-aware datetimes" abstuerzen (core.clock, Phase 1,
                   # schreibt neu ausschliesslich tz-aware).
                   "bot_last_flows_refresh_at", "bot_backfill_opened_at_requested",
                   "bot_runner_consecutive_failures", "bot_runner_last_success_at",
                   # Manuelle Stop-/Schliess-Anfragen und ihre Ergebnisse
                   # beziehen sich auf Positionen, die die DELETEs oben gerade
                   # entfernt haben - ohne Aufraeumen bliebe eine stehen
                   # gelassene Anfrage/ein altes Ergebnis fuer ein Symbol
                   # stehen, das es im neuen Experiment (noch) gar nicht gibt.
                   "bot_manual_stop_requests", "bot_manual_stop_results",
                   "bot_close_position_requests", "bot_close_position_results",
                   # Chart-Beginn bezieht sich auf den gerade gelöschten Equity-Verlauf.
                   "bot_chart_start"):
            con.execute("DELETE FROM meta WHERE key = ?", (key,))
        con.execute("DELETE FROM meta WHERE key LIKE 'bot_stop_retry:%'")


# --- MIGRATION VON V1 ---

def _migrate_legacy_json():
    """Übernimmt portfolio.json der alten App einmalig als Aktien-Positionen."""
    with _connect() as con:
        done = con.execute("SELECT value FROM meta WHERE key = 'migrated_v1'").fetchone()
        if done:
            return
    path = config.LEGACY_PORTFOLIO_JSON
    imported = 0
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for category, stocks in data.items():
            for s in stocks:
                symbol = str(s.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                save_position(
                    symbol, "stock",
                    quantity=float(s.get("quantity", 0.0)),
                    buy_price_eur=float(s.get("buy_price", 0.0)),
                    category=category, source="flatex" if "flatex" in category.lower() else "manuell",
                )
                imported += 1
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_v1', ?)",
            (f"{datetime.now().isoformat(timespec='seconds')}|{imported}",),
        )
