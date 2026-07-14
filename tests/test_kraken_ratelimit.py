import pytest

from data import kraken


def test_private_with_retry_recovers_from_rate_limit(monkeypatch):
    calls = {"n": 0}

    def fake_private(endpoint, data):
        calls["n"] += 1
        if calls["n"] < 3:
            raise kraken.KrakenError("EAPI:Rate limit exceeded")
        return {"result": "ok"}

    monkeypatch.setattr(kraken, "_private", fake_private)
    sleeps = []
    monkeypatch.setattr(kraken.time, "sleep", lambda s: sleeps.append(s))

    result = kraken._private_with_retry("Ledgers", {"ofs": 0})
    assert result == {"result": "ok"}
    assert calls["n"] == 3
    assert len(sleeps) == 2   # zwei Backoff-Wartezeiten vor dem dritten (erfolgreichen) Versuch


def test_private_with_retry_reraises_non_rate_limit_error(monkeypatch):
    def fake_private(endpoint, data):
        raise kraken.KrakenError("EGeneral:Invalid arguments")

    monkeypatch.setattr(kraken, "_private", fake_private)
    monkeypatch.setattr(kraken.time, "sleep", lambda s: None)

    with pytest.raises(kraken.KrakenError, match="Invalid arguments"):
        kraken._private_with_retry("Ledgers", {"ofs": 0})


def test_private_with_retry_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(kraken, "_private",
                        lambda e, d: (_ for _ in ()).throw(kraken.KrakenError("Rate limit exceeded")))
    monkeypatch.setattr(kraken.time, "sleep", lambda s: None)

    with pytest.raises(kraken.KrakenError, match="Rate limit"):
        kraken._private_with_retry("Ledgers", {"ofs": 0}, max_retries=2)


def test_paginated_uses_retry_and_delay(monkeypatch):
    pages = [
        {"ledger": {"a": 1, "b": 2}, "count": 3},
        {"ledger": {"c": 3}, "count": 3},
    ]
    calls = {"n": 0}

    def fake_private_with_retry(endpoint, data):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    monkeypatch.setattr(kraken, "_private_with_retry", fake_private_with_retry)
    sleeps = []
    monkeypatch.setattr(kraken.time, "sleep", lambda s: sleeps.append(s))

    items = kraken._paginated("Ledgers", "ledger")
    assert items == [1, 2, 3]
    assert sleeps == [3.0]   # Pause nur zwischen den beiden Seiten
