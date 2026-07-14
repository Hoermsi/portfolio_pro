"""Zentrale Konfiguration: Pfade, API-Keys, Claude-Modell-Registry."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    """Stabiler, vom Code getrennter Ordner für veränderliche Nutzerdaten (DB, .env).

    So überlebt ein Update (Austausch des Code-Ordners) die Daten des Nutzers.
    Reihenfolge: expliziter Env-Override (Dev) > %LOCALAPPDATA%\\PortfolioPro
    (Windows) > ~/.portfolio_pro (sonst).
    """
    override = os.getenv("PORTFOLIO_PRO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "PortfolioPro"
    return Path.home() / ".portfolio_pro"


# Verzeichnis der Nutzerdaten (kein mkdir beim Import -> seiteneffektfrei für Tests;
# das Anlegen übernimmt db.init_db()).
DATA_DIR = _default_data_dir()

# Alter Ort der DB (im Code-Ordner) - nur noch zur einmaligen Migration relevant.
LEGACY_DB_PATH = BASE_DIR / "portfolio.db"

# .env: zuerst aus dem Nutzerdaten-Ordner, dann die alten Orte als Fallback.
load_dotenv(DATA_DIR / ".env")
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

DB_PATH = DATA_DIR / "portfolio.db"
LEGACY_PORTFOLIO_JSON = BASE_DIR.parent / "portfolio.json"


def _meta_or_env(meta_key: str, env_key: str) -> str | None:
    """API-Key zuerst aus der lokalen DB (in-App eingegeben), sonst aus .env/Umgebung.

    So kann ein Nutzer seine Keys im Onboarding/den Einstellungen eingeben, ohne
    eine .env zu bearbeiten - das eigene .env-Setup bleibt als Fallback erhalten.
    Die DB wird nur angefasst, wenn die Datei existiert (kein versehentliches
    Anlegen einer leeren portfolio.db vor init_db oder in Tests); Lazy-Import
    von db vermeidet den Zirkelimport config<->db.
    """
    try:
        if DB_PATH.exists():
            from core import db
            val = db.get_meta(meta_key)
            if val and val.strip():
                return val.strip()
    except Exception:
        pass
    return os.getenv(env_key)


def anthropic_api_key() -> str | None:
    return _meta_or_env("anthropic_api_key", "ANTHROPIC_API_KEY")


def kraken_keys() -> tuple[str | None, str | None]:
    return (_meta_or_env("kraken_api_key", "KRAKEN_API_KEY"),
            _meta_or_env("kraken_api_secret", "KRAKEN_API_SECRET"))


# Meta-Schlüssel, unter denen in-App eingegebene Keys liegen (für Settings/Onboarding).
API_KEY_META = {
    "anthropic": "anthropic_api_key",
    "kraken_key": "kraken_api_key",
    "kraken_secret": "kraken_api_secret",
}


def save_api_key(meta_key: str, value: str | None):
    """API-Key in der DB setzen (nicht-leer) oder entfernen (leer/None -> .env-Fallback)."""
    from core import db
    if value and value.strip():
        db.set_meta(meta_key, value.strip())
    else:
        db.delete_meta(meta_key)


# Anzeigename -> Modell-ID (exakte Strings, keine Datums-Suffixe)
CLAUDE_MODELS = {
    "Haiku 4.5 (schnell & günstig)": "claude-haiku-4-5",
    "Sonnet 5 (ausgewogen)": "claude-sonnet-5",
    "Opus 4.8 (max. Qualität)": "claude-opus-4-8",
}

# Preise pro 1 Mio. Token in USD: (Input, Output)
CLAUDE_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}

DEFAULT_SPECIALIST_MODEL = "claude-haiku-4-5"
DEFAULT_SENIOR_MODEL = "claude-opus-4-8"
DEFAULT_CHAT_MODEL = "claude-opus-4-8"


def model_label(model_id: str) -> str:
    for label, mid in CLAUDE_MODELS.items():
        if mid == model_id:
            return label
    return model_id
