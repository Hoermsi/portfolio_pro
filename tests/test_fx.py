import pytest

from data import fx


@pytest.fixture(autouse=True)
def clear_fx_caches():
    fx._cache.clear()
    fx._last_good.clear()
    yield
    fx._cache.clear()
    fx._last_good.clear()


def test_eur_is_one():
    assert fx.get_fx_to_eur("EUR") == 1.0
    assert fx.get_fx_to_eur("") == 1.0


def test_plausible_rate_accepted(monkeypatch):
    monkeypatch.setattr(fx, "_fetch", lambda pair: 0.8751)
    assert fx.get_fx_to_eur("USD") == 0.8751


def test_implausible_rate_rejected_uses_last_good(monkeypatch):
    # erst ein guter Wert
    monkeypatch.setattr(fx, "_fetch", lambda pair: 0.8751)
    assert fx.get_fx_to_eur("USD") == 0.8751
    fx._cache.clear()  # Cache leeren, damit erneut gefetcht wird
    # jetzt ein kaputter Wert (der reale Bug: ~210 statt 0.87)
    monkeypatch.setattr(fx, "_fetch", lambda pair: 210.18)
    assert fx.get_fx_to_eur("USD") == 0.8751   # letzter guter Wert statt Ausreißer


def test_implausible_without_history_returns_none(monkeypatch):
    monkeypatch.setattr(fx, "_fetch", lambda pair: 240.0)
    assert fx.get_fx_to_eur("USD") is None


def test_gbp_pence_scaled(monkeypatch):
    monkeypatch.setattr(fx, "_fetch", lambda pair: 1.15)   # GBP -> EUR
    assert abs(fx.get_fx_to_eur("GBp") - 0.0115) < 1e-9
