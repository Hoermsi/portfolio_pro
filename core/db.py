"""SQLite-Datenhaltung: Assets, Positionen, Snapshots, Agenten-Historie.

Migration: beim ersten Start wird das portfolio.json der V1 (Ordnerstruktur)
als Aktien-Positionen übernommen.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

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
"""


@contextmanager
def _connect():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
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
        con.executescript(_SCHEMA)
        _migrate_shadow_scope(con)
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
                 trade_date: str | None = None, note: str = "") -> int:
    """Bucht einen Kauf/Verkauf und aktualisiert die betroffene Position atomar.

    Käufe bilden den durchschnittlichen Einstand inklusive Gebühren neu. Bei
    Verkäufen bleibt der bisherige durchschnittliche Einstand erhalten.
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
