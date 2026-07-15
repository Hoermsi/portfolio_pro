"""Tests für Datensicherung (backup_bytes / restore_from_bytes)."""
import pytest


def test_backup_bytes_is_sqlite(tmp_db):
    tmp_db.save_position("NVDA", "stock", 1.0, 100.0)
    data = tmp_db.backup_bytes()
    assert data.startswith(b"SQLite format 3\x00")
    assert len(data) > 1000


def test_backup_restore_roundtrip(tmp_db):
    db = tmp_db
    db.save_position("NVDA", "stock", 2.0, 100.0)
    backup = db.backup_bytes()

    db.save_position("AAPL", "stock", 5.0, 150.0)      # Zustand danach verändern
    assert len(db.list_positions("stock")) == 2

    db.restore_from_bytes(backup)                       # zurück zum Backup-Stand
    positions = db.list_positions("stock")
    assert [p.symbol for p in positions] == ["NVDA"]


def test_restore_rejects_invalid_bytes(tmp_db):
    db = tmp_db
    db.save_position("NVDA", "stock", 1.0, 100.0)
    with pytest.raises(ValueError):
        db.restore_from_bytes(b"das ist keine datenbank")
    # Original unangetastet
    assert len(db.list_positions("stock")) == 1


def test_restore_keeps_bak_copy(tmp_db):
    from core import config
    db = tmp_db
    db.save_position("NVDA", "stock", 1.0, 100.0)
    backup = db.backup_bytes()
    db.restore_from_bytes(backup)
    baks = list(config.DB_PATH.parent.glob(f"{config.DB_PATH.name}.bak-*"))
    assert baks, "Vor dem Restore muss eine .bak-Kopie angelegt werden"
