from core import config, db


def test_anthropic_key_db_first(tmp_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    db.set_meta("anthropic_api_key", "db-value")
    assert config.anthropic_api_key() == "db-value"


def test_anthropic_key_env_fallback(tmp_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    assert config.anthropic_api_key() == "env-value"        # kein DB-Wert -> .env


def test_empty_db_value_falls_back_to_env(tmp_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    db.set_meta("anthropic_api_key", "   ")
    assert config.anthropic_api_key() == "env-value"


def test_kraken_keys_db_first(tmp_db, monkeypatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    db.set_meta("kraken_api_key", "k")
    db.set_meta("kraken_api_secret", "s")
    assert config.kraken_keys() == ("k", "s")


def test_save_and_remove_api_key(tmp_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.save_api_key("anthropic_api_key", "abc")
    assert config.anthropic_api_key() == "abc"
    config.save_api_key("anthropic_api_key", "")            # entfernen
    assert config.anthropic_api_key() is None
    assert db.get_meta("anthropic_api_key") is None


def test_no_db_file_is_not_created(tmp_path, monkeypatch):
    """Fehlt die DB-Datei, wird sie beim Key-Lesen NICHT angelegt (nur .env-Fallback)."""
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(config, "DB_PATH", missing)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-only")
    assert config.anthropic_api_key() == "env-only"
    assert not missing.exists()
