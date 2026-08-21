"""Tests für die Update-Prüfung (ohne echten Netzwerkzugriff)."""
import pytest

from core import updater


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_cache():
    updater.check_for_update.cache_clear()
    yield
    updater.check_for_update.cache_clear()


def _release(tag, assets=None, zipball="https://z/zip"):
    return {"tag_name": tag, "body": "Notes", "zipball_url": zipball,
            "assets": assets or []}


def test_tag_to_version_variants():
    assert str(updater._tag_to_version("v1.2.3")) == "1.2.3"
    assert str(updater._tag_to_version("1.2.3")) == "1.2.3"
    assert updater._tag_to_version("kein-tag") is None


def test_no_update_when_not_configured(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", updater._PLACEHOLDER)
    assert updater.check_for_update() is None


def test_detects_newer_version(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", "me/portfolio_pro")
    monkeypatch.setattr(updater, "APP_VERSION", "1.0.0")
    assets = [{"name": "portfolio_pro-1.2.0.zip",
               "browser_download_url": "https://dl/code.zip"}]
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: _FakeResp(_release("v1.2.0", assets)))
    info = updater.check_for_update()
    assert info is not None
    assert info["version"] == "1.2.0"
    assert info["asset_url"] == "https://dl/code.zip"


def test_no_update_when_same_or_older(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", "me/portfolio_pro")
    monkeypatch.setattr(updater, "APP_VERSION", "1.2.0")
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: _FakeResp(_release("v1.2.0")))
    assert updater.check_for_update() is None


def test_falls_back_to_zipball_without_assets(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", "me/portfolio_pro")
    monkeypatch.setattr(updater, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: _FakeResp(_release("v1.1.0", zipball="https://z/ball")))
    info = updater.check_for_update()
    assert info["asset_url"] == "https://z/ball"


def test_network_error_returns_none(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", "me/portfolio_pro")

    def _boom(*a, **k):
        raise updater.requests.RequestException("offline")

    monkeypatch.setattr(updater.requests, "get", _boom)
    assert updater.check_for_update() is None


def test_swap_script_contains_pip_install(tmp_path, monkeypatch):
    """Der Swap-Helfer installiert geänderte Abhängigkeiten in die Laufzeit nach."""
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    install = tmp_path / "app"
    install.mkdir()
    path = updater._write_swap_script(src, install, None)
    content = path.read_text(encoding="utf-8")
    assert "pip install -r" in content


def test_swap_script_robocopy_forces_same_files(tmp_path, monkeypatch):
    """Robocopy muss /IS (+ /IT) nutzen - ohne die Flags ueberspringt Robocopy
    Dateien mit gleicher Groesse+Zeitstempel (z.B. core/version.py, wenn alte
    und neue Versionsnummer gleich lang sind), obwohl sich der Inhalt aendert."""
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    install = tmp_path / "app"
    install.mkdir()
    path = updater._write_swap_script(src, install, None)
    content = path.read_text(encoding="utf-8")
    assert " /IS" in content
    assert "robocopy" in content.lower()
    assert "requirements.txt" in content
    assert "runtime" in content


def test_swap_script_writes_diagnostic_log(tmp_path, monkeypatch):
    """Jeder Schritt muss protokolliert werden - sonst ist ein Fehlschlag des
    detached laufenden Skripts von aussen nicht diagnostizierbar."""
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    install = tmp_path / "app"
    install.mkdir()
    path = updater._write_swap_script(src, install, None)
    content = path.read_text(encoding="utf-8")
    assert "apply_update.log" in content
    assert content.count(">> %LOG%") >= 2  # mindestens robocopy- und Abschluss-Zeile


def test_read_last_update_log_none_when_missing(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert updater.read_last_update_log() is None


def test_read_last_update_log_returns_content(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    updater._swap_log_path().parent.mkdir(parents=True, exist_ok=True)
    updater._swap_log_path().write_text("robocopy beendet, exit code 1", encoding="utf-8")
    assert "exit code 1" in updater.read_last_update_log()


def test_launch_detached_falls_back_without_breakaway(tmp_path, monkeypatch):
    """Erlaubt das Job-Object kein Breakaway (CreateProcess schlaegt fehl),
    muss ein zweiter Versuch ohne das Flag greifen statt die App abstuerzen
    zu lassen."""
    from core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    calls = []
    breakaway = getattr(updater.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

    def _fake_popen(args, cwd=None, creationflags=0, close_fds=True):
        calls.append(creationflags)
        if breakaway and (creationflags & breakaway):
            raise OSError("Breakaway vom Job nicht erlaubt")
        return object()

    monkeypatch.setattr(updater.subprocess, "Popen", _fake_popen)
    updater._launch_detached(tmp_path / "dummy.bat")
    assert len(calls) == 2  # erster Versuch (mit Breakaway) schlaegt fehl, zweiter greift
