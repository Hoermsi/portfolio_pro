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
    assert "requirements.txt" in content
    assert "runtime" in content
