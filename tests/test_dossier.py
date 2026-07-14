from agents.dossier import SCOPES, portfolio_prompt

_SUMMARY = {
    "aktien": [{"symbol": "NVDA", "wert_eur": 1000.0}],
    "krypto": [{"symbol": "BTC", "wert_eur": 500.0}],
    "aktien_summe_eur": 1000.0,
    "krypto_summe_eur": 500.0,
    "cash_eur": 500.0,
    "gesamt_eur": 2000.0,
    "allokation": {"aktien_pct": 50.0, "krypto_pct": 25.0, "cash_pct": 25.0},
}


def _dossier(scope, value):
    return {
        "scope": scope,
        "scope_name": SCOPES[scope]["name"],
        "scope_value_eur": value,
        "summary": _SUMMARY,
        "konzentration": {"anzahl_positionen": 1},
        "korrelation": None,
    }


def test_prompt_scope_all_contains_both_classes(tmp_db):
    text = portfolio_prompt(_dossier("all", 1500.0))
    assert "GESAMTPORTFOLIO" in text
    assert "NVDA" in text and "BTC" in text
    assert "Allokation" in text


def test_prompt_scope_all_contains_cash(tmp_db):
    """Der Gesamtportfolio-Prompt weist den Bank-Cash-Bestand und Cash-Anteil aus."""
    text = portfolio_prompt(_dossier("all", 1500.0))
    assert "Cash" in text
    assert "500.00 EUR" in text
    assert "25.0% Cash" in text


def test_prompt_scope_stock_excludes_crypto(tmp_db):
    text = portfolio_prompt(_dossier("stock", 1000.0))
    assert "AKTIEN-PORTFOLIO" in text
    assert "NVDA" in text
    assert "BTC" not in text
    assert "NUR der Aktien-Teil" in text


def test_prompt_scope_crypto_excludes_stocks(tmp_db):
    text = portfolio_prompt(_dossier("crypto", 500.0))
    assert "KRYPTO-PORTFOLIO" in text
    assert "BTC" in text
    assert "NVDA" not in text


def test_prompt_contains_risk_profile(tmp_db):
    """Jeder Portfolio-Review-Prompt trägt das Risikoprofil des Nutzers mit."""
    import json
    tmp_db.set_meta("risk_profile",
                    json.dumps({"risk": 3, "target_return_pct": 5.0, "retirement_year": 2060}))
    text = portfolio_prompt(_dossier("all", 1500.0))
    assert "RISIKOPROFIL DES NUTZERS" in text
    assert "Risikobereitschaft 3/10" in text
    assert "keine Anlageberatung" in text
