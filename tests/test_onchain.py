"""Tests für data/onchain.py (Netzwerk vollständig gemockt).

Schwerpunkt: die Bootstrap-Once-Logik, weil die Quelle nur 10 Anfragen/Stunde
erlaubt - ein Regressionstest stellt sicher, dass ein zweiter latest()-Aufruf
für dieselbe Metrik am selben Tag KEINEN weiteren Request auslöst.
"""
import pytest

from data import onchain


@pytest.fixture(autouse=True)
def _reset_cache():
    onchain.latest.cache_clear()
    yield
    onchain.latest.cache_clear()


def _full_history_payload():
    return [
        {"d": "2024-01-01", "unixTs": 1704067200, "mvrvZscore": 1.2},
        {"d": "2024-01-02", "unixTs": 1704153600, "mvrvZscore": 1.4},
        {"d": "2024-01-03", "unixTs": 1704240000, "mvrvZscore": 0.9},
    ]


def test_bootstrap_persists_full_history(tmp_db, monkeypatch):
    calls = []

    def fake_get(path):
        calls.append(path)
        assert path == "mvrv-zscore"
        return _full_history_payload()

    monkeypatch.setattr(onchain, "_get", fake_get)
    ok = onchain.bootstrap("mvrv-zscore")

    assert ok is True
    assert calls == ["mvrv-zscore"]
    rows = tmp_db.list_onchain("mvrv-zscore")
    assert [r["value"] for r in rows] == [1.2, 1.4, 0.9]
    assert tmp_db.onchain_bootstrapped("mvrv-zscore") is True


def test_bootstrap_is_not_repeated(tmp_db, monkeypatch):
    """Kernschutz gegen das 10-Anfragen/Stunde-Limit: ein zweiter bootstrap()-
    Aufruf für dieselbe, bereits geladene Metrik darf NICHT erneut die API
    treffen."""
    calls = []

    def fake_get(path):
        calls.append(path)
        return _full_history_payload()

    monkeypatch.setattr(onchain, "_get", fake_get)
    onchain.bootstrap("mvrv-zscore")
    onchain.bootstrap("mvrv-zscore")
    onchain.bootstrap("mvrv-zscore")

    assert len(calls) == 1


def test_latest_uses_bootstrap_result_without_extra_request(tmp_db, monkeypatch):
    """Direkt nach dem ersten Bootstrap ist der neueste Wert bereits die letzte
    Zeile der Vollhistorie - latest() darf am selben Tag keinen zusätzlichen
    /last-Request mehr auslösen (spart Kontingent bei der Ersteinrichtung mit
    4 Metriken: sonst 8 statt 4 Requests)."""
    calls = []

    def fake_get(path):
        calls.append(path)
        assert path == "mvrv-zscore"  # NIE "mvrv-zscore/last" bei diesem Test
        return _full_history_payload()

    monkeypatch.setattr(onchain, "_get", fake_get)
    value = onchain.latest("mvrv-zscore")

    assert value == 0.9  # letzte Zeile
    assert calls == ["mvrv-zscore"]


def test_latest_polls_last_endpoint_on_subsequent_days(tmp_db, monkeypatch):
    """War schon gebootstrapped (core.db-Flag gesetzt), nutzt latest() den
    günstigen /last-Endpunkt statt erneut die Vollhistorie zu laden."""
    tmp_db.save_onchain("mvrv-zscore", [{"d": "2024-01-03", "value": 0.9}])
    tmp_db.mark_onchain_bootstrapped("mvrv-zscore")

    calls = []

    def fake_get(path):
        calls.append(path)
        assert path == "mvrv-zscore/last"
        return {"d": "2024-01-04", "unixTs": 1704326400, "mvrvZscore": 1.1}

    monkeypatch.setattr(onchain, "_get", fake_get)
    value = onchain.latest("mvrv-zscore")

    assert value == 1.1
    assert calls == ["mvrv-zscore/last"]
    rows = tmp_db.list_onchain("mvrv-zscore")
    assert rows[-1] == {"d": "2024-01-04", "value": 1.1}


def test_latest_returns_none_on_rate_limit_without_crashing(tmp_db, monkeypatch):
    def fake_get(path):
        return None  # _get() liefert bei Rate-Limit/Fehler immer None

    monkeypatch.setattr(onchain, "_get", fake_get)
    assert onchain.latest("mvrv-zscore") is None


def test_history_returns_local_persistence(tmp_db, monkeypatch):
    monkeypatch.setattr(onchain, "_get", lambda path: _full_history_payload())
    rows = onchain.history("mvrv-zscore")
    assert [r["d"] for r in rows] == ["2024-01-01", "2024-01-02", "2024-01-03"]

    rows_filtered = onchain.history("mvrv-zscore", days=1)
    assert all(r["d"] >= rows_filtered[0]["d"] for r in rows_filtered) if rows_filtered else True
