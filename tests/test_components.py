"""Tests für ui/components.format_fundamental() (reine Formatierungslogik,
kein Streamlit-Aufruf nötig)."""
import pytest

from ui import components


def test_format_fundamental_market_cap_in_billions():
    assert components.format_fundamental("marktkap_eur", 2_847_000_000.0) == "2.85 Mrd. €"
    assert components.format_fundamental("marktkap", 500_000_000.0) == "0.50 Mrd. €"


def test_format_fundamental_dividend_yield_no_multiplier():
    """Verifiziert (yfinance-Livecheck): dividendYield kommt bereits in
    Prozent-Skala, also kein ×100 - sonst wären reale Renditen absurd klein
    oder groß."""
    assert components.format_fundamental("dividendenrendite", 2.33) == "2.33%"


def test_format_fundamental_profit_margin_and_revenue_growth_multiply_fraction():
    assert components.format_fundamental("gewinnmarge", 0.153) == "15.3%"
    assert components.format_fundamental("umsatzwachstum", 0.02) == "2.0%"


def test_format_fundamental_crypto_pct_fields_no_multiplier():
    """Krypto *_pct-Felder (CoinGecko) sind bereits Prozent-Skala."""
    assert components.format_fundamental("ath_abstand_pct", -70.5) == "-70.5%"
    assert components.format_fundamental("aenderung_7d_pct", 3.2) == "3.2%"


def test_format_fundamental_currency_suffixed_fields():
    assert components.format_fundamental("analysten_kursziel", 123.456) == "123.46 €"
    assert components.format_fundamental("kurs_eur", 0.0234) == "0.0234 €"


def test_format_fundamental_plain_ratio_fields():
    assert components.format_fundamental("kgv", 18.456) == "18.46"
    assert components.format_fundamental("beta", 1.2) == "1.20"


def test_format_fundamental_unknown_field_falls_back_to_generic():
    assert components.format_fundamental("irgendein_neues_feld", 3.14159) == "3.14"


def test_format_fundamental_supply_fields_thousands_separated():
    assert components.format_fundamental("umlauf_supply", 19_500_000.0) == "19,500,000"
