import sys
from pathlib import Path

import pytest

# portfolio_pro auf den Pfad legen, damit die Pakete importierbar sind
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Leere Test-DB + kein Legacy-JSON."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "LEGACY_PORTFOLIO_JSON", tmp_path / "missing.json")
    from core import db
    db.init_db()
    return db
