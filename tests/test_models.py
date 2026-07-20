"""Tests für core/models.evaluate() - insbesondere den FX-Fallback-Fix."""
from core.models import Position, evaluate


def _pos(**kw):
    defaults = dict(id=1, symbol="AAPL", asset_type="stock", name="Apple",
                    currency="USD", quantity=10.0, buy_price_eur=100.0,
                    category="Standard", source="manuell")
    defaults.update(kw)
    return Position(**defaults)


def test_evaluate_normal_case():
    v = evaluate(_pos(), price_native=200.0, fx_to_eur=0.9)
    assert v.error is None
    assert v.price_eur == 180.0
    assert v.value_eur == 1800.0


def test_evaluate_no_price():
    v = evaluate(_pos(), price_native=None, fx_to_eur=0.9)
    assert v.error == "Kein Kurs verfügbar"
    assert v.value_eur is None


def test_evaluate_missing_fx_is_error_not_1to1():
    """Ein nicht ermittelbarer Wechselkurs darf NICHT stillschweigend als 1.0
    behandelt werden - eine 185-USD-Aktie würde sonst als 185 EUR erscheinen."""
    v = evaluate(_pos(), price_native=185.0, fx_to_eur=None)
    assert v.error == "Kein Wechselkurs verfügbar"
    assert v.value_eur is None
    assert v.price_eur is None


def test_evaluate_eur_position_fx_one():
    v = evaluate(_pos(currency="EUR"), price_native=50.0, fx_to_eur=1.0)
    assert v.error is None
    assert v.value_eur == 500.0
