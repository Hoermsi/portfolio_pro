"""core/notify.py: Telegram-Versand ist fire-and-forget - nie ein echter
Netzwerkcall in Tests, ein Fehlschlag darf nie eine Ausnahme werfen."""
from core import notify


def test_send_without_credentials_does_not_call_requests(monkeypatch):
    """Ohne Token/chat_id darf gar nicht erst versucht werden zu senden -
    requests.post wird bei einem Aufruf zum Fehlschlag."""
    monkeypatch.setattr(notify.config, "telegram_credentials", lambda: (None, None))

    def _boom(*args, **kwargs):
        raise AssertionError("requests.post haette nicht aufgerufen werden duerfen")

    monkeypatch.setattr(notify.requests, "post", _boom)
    assert notify.send("Testnachricht") is False


def test_send_success(monkeypatch):
    monkeypatch.setattr(notify.config, "telegram_credentials", lambda: ("tok", "42"))

    class _Resp:
        ok = True

    captured = {}

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(notify.requests, "post", _fake_post)
    assert notify.send("Testnachricht") is True
    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["json"] == {"chat_id": "42", "text": "Testnachricht"}


def test_send_false_on_non_ok_response(monkeypatch):
    monkeypatch.setattr(notify.config, "telegram_credentials", lambda: ("tok", "42"))

    class _Resp:
        ok = False

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: _Resp())
    assert notify.send("Testnachricht") is False


def test_send_swallows_network_errors(monkeypatch):
    """Kein Internet, Timeout, Telegram down - darf die Handelsentscheidung
    des Aufrufers nie mit einer Ausnahme unterbrechen."""
    monkeypatch.setattr(notify.config, "telegram_credentials", lambda: ("tok", "42"))

    def _raise(*args, **kwargs):
        raise ConnectionError("kein Netz")

    monkeypatch.setattr(notify.requests, "post", _raise)
    assert notify.send("Testnachricht") is False
