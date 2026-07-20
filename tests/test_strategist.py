import json

import pytest

from agents import strategist
from core import shadow


@pytest.fixture
def strat_db(tmp_db, monkeypatch):
    prices = {("ENJ", "crypto"): 0.5, ("SOL", "crypto"): 10.0, ("BTC", "crypto"): 100.0}
    monkeypatch.setattr(shadow, "price_eur",
                        lambda sym, at: 1.0 if at == "cash" else prices.get((sym, at)))
    # init_from_real umgehen: Krypto-KI-Depot direkt setzen
    from core import db
    db.set_meta("shadow_start_crypto", json.dumps({"date": "2026-07-10",
                                                   "real_start": 1000.0, "shadow_start": 1000.0}))
    db.set_shadow_position("crypto", "ENJ", "crypto", 1000.0)   # 500 EUR
    db.set_shadow_position("crypto", "BTC", "crypto", 5.0)       # 500 EUR
    return db


def test_run_strategy_applies_recommendations(strat_db, monkeypatch):
    fake = {
        "marktausblick": "Altcoins schwach.",
        "gesamtkommentar": "ENJ abgehängt.",
        "empfehlungen": [
            {"aktion": "umschichten", "symbol": "ENJ", "asset_type": "crypto",
             "ziel_symbol": "SOL", "ziel_asset_type": "crypto", "anteil_pct": 100,
             "begruendung": "ENJ raus, SOL rein"},
            {"aktion": "halten", "symbol": "BTC", "asset_type": "crypto",
             "anteil_pct": 100, "begruendung": "Kern halten"},
        ],
    }
    monkeypatch.setattr(strategist, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.01, "input": 1, "output": 1}, None))

    result = strategist.run_strategy("crypto", "claude-haiku-4-5")
    assert "error" not in result
    assert len(result["empfehlungen"]) == 2

    # ENJ komplett weg, SOL da
    assert strat_db.get_shadow_quantity("crypto", "ENJ", "crypto") == 0.0
    assert strat_db.get_shadow_quantity("crypto", "SOL", "crypto") > 0
    assert strat_db.get_shadow_quantity("crypto", "BTC", "crypto") == 5.0

    # Empfehlungen + Log + Agent-Run wurden protokolliert (im richtigen Scope)
    assert len(strat_db.list_recommendations("crypto")) == 2
    assert any(r["aktion"] == "umschichten" for r in strat_db.list_shadow_log("crypto"))
    runs = strat_db.list_agent_runs()
    assert runs and runs[0]["mode"] == "strategy"
    assert runs[0]["target"] == "SHADOW-KRYPTO"


def test_run_strategy_without_init(tmp_db, monkeypatch):
    monkeypatch.setattr(strategist, "run_json_agent",
                        lambda *a, **k: ({}, {}, None))
    result = strategist.run_strategy("stock", "claude-haiku-4-5")
    assert "error" in result


def test_run_strategy_error_with_cost_is_not_lost(strat_db, monkeypatch):
    """Auch bei Fehlschlag (z.B. max_tokens) wurde bereits abgerechnet -
    das muss im Ergebnis und in agent_runs sichtbar bleiben, nicht stillschweigend
    verworfen werden."""
    monkeypatch.setattr(
        strategist, "run_json_agent",
        lambda *a, **k: (None, {"cost_usd": 0.037, "input": 3000, "output": 10}, "Antwort abgeschnitten (max_tokens erreicht)."),
    )

    result = strategist.run_strategy("crypto", "claude-opus-4-8")
    assert result["error"] == "Antwort abgeschnitten (max_tokens erreicht)."
    assert result["total_cost_usd"] == pytest.approx(0.037)
    assert result["usage"]["cost_usd"] == pytest.approx(0.037)

    runs = strat_db.list_agent_runs()
    assert runs and runs[0]["cost_usd"] == pytest.approx(0.037)
    assert runs[0]["recommendation"] == "Fehlgeschlagen"
    assert runs[0]["target"] == "SHADOW-KRYPTO"


def test_run_strategy_error_without_cost_skips_log(strat_db, monkeypatch):
    """Fehler vor jeder Abrechnung (z.B. fehlender API-Key) sollen keinen
    Kosten-Log-Eintrag erzeugen, da nichts abgerechnet wurde."""
    monkeypatch.setattr(strategist, "run_json_agent",
                        lambda *a, **k: (None, {}, "ANTHROPIC_API_KEY fehlt in der .env"))

    result = strategist.run_strategy("crypto", "claude-haiku-4-5")
    assert result["error"] == "ANTHROPIC_API_KEY fehlt in der .env"
    assert result["total_cost_usd"] == 0.0
    assert strat_db.list_agent_runs() == []


def test_schema_restricts_asset_types():
    crypto_schema = strategist._schema_for("crypto")
    stock_schema = strategist._schema_for("stock")
    c_enum = crypto_schema["properties"]["empfehlungen"]["items"]["properties"]["asset_type"]["enum"]
    s_enum = stock_schema["properties"]["empfehlungen"]["items"]["properties"]["asset_type"]["enum"]
    assert c_enum == ["crypto", "cash"] and "stock" not in c_enum
    assert s_enum == ["stock", "cash"] and "crypto" not in s_enum


def test_target_and_mandate_per_scope():
    assert strategist._TARGETS["crypto"] == "SHADOW-KRYPTO"
    assert strategist._TARGETS["stock"] == "SHADOW-AKTIEN"
    assert "KRYPTO" in strategist._system_for("crypto").upper()
    assert "AKTIEN" in strategist._system_for("stock").upper()


def test_estimate_cost_positive():
    assert strategist.estimate_cost("claude-haiku-4-5") > 0
    assert strategist.estimate_cost("claude-opus-4-8") > strategist.estimate_cost("claude-haiku-4-5")


def test_build_prompt_contains_risk_profile(strat_db, monkeypatch):
    """Das Risikoprofil des Nutzers muss im Strategen-Prompt ankommen."""
    strat_db.set_meta("risk_profile",
                      json.dumps({"risk": 7, "target_return_pct": 12.0, "retirement_year": 2055}))
    monkeypatch.setattr(strategist, "portfolio_summary",
                        lambda: {"krypto": [], "aktien": [], "gesamt_eur": 0.0})
    monkeypatch.setattr(strategist, "_position_metrics", lambda *a: {})
    prompt = strategist.build_prompt("crypto")
    assert "RISIKOPROFIL DES NUTZERS" in prompt
    assert "Risikobereitschaft 7/10" in prompt
    assert "Zielrendite 12.0% p.a." in prompt
    assert "2055" in prompt


def test_build_prompt_contains_cash_block(strat_db, monkeypatch):
    """Cash & Zielallokation des Nutzers muessen im Strategen-Prompt ankommen."""
    monkeypatch.setattr(strategist, "portfolio_summary",
                        lambda: {"krypto": [], "aktien": [], "gesamt_eur": 0.0})
    monkeypatch.setattr(strategist, "_position_metrics", lambda *a: {})
    # cash_allocation_prompt nutzt dossier.portfolio_summary
    from agents import dossier
    monkeypatch.setattr(dossier, "portfolio_summary",
                        lambda: {"aktien_summe_eur": 0, "krypto_summe_eur": 0, "cash_eur": 5000.0,
                                 "gesamt_eur": 5000.0, "aktien": [], "krypto": [],
                                 "allokation": {"aktien_pct": 0.0, "krypto_pct": 0.0, "cash_pct": 100.0}})
    prompt = strategist.build_prompt("crypto")
    assert "CASH & ZIELALLOKATION" in prompt
    assert "BANKGUTHABEN" in prompt
    assert "nicht Teil des KI-Depots" in prompt


def test_system_prompt_mentions_risk_profile():
    assert "Risikoprofil" in strategist._system_for("crypto")
    assert "keine Anlageberatung" in strategist._system_for("crypto")
