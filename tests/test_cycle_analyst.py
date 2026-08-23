"""Tests für agents/cycle_analyst.py (Claude-Call + alle Datenquellen gemockt,
Muster: tests/test_strategist.py)."""
import pandas as pd
import pytest

from agents import cycle_analyst
from analysis import cycle, cycle_backtest, exit_ranking
from data import news as news_data


def _fake_cycle_score():
    return {
        "score": 42.0, "regime": "Aufschwung", "regime_reason": "Testregime",
        "coverage_pct": 100.0, "unavailable": [],
        "breakdown": [
            {"key": "mvrv_z", "label": "MVRV Z-Score", "score": 40.0, "value": 1.2,
             "text": "1.20", "weight_pct": 25.0},
        ],
        "price_now": 60000.0, "drawdown_pct": -10.0,
    }


@pytest.fixture(autouse=True)
def _mock_data_sources(monkeypatch, tmp_db):
    series = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2017-12-17", "2021-11-10", "2026-08-22"]))
    monkeypatch.setattr(cycle, "cycle_score", _fake_cycle_score)
    monkeypatch.setattr(cycle, "btc_price_series", lambda: series)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: series)
    monkeypatch.setattr(cycle, "trigger_price", lambda threshold, cycle=None: 80000.0)
    monkeypatch.setattr(exit_ranking, "exit_ranking",
                        lambda: [{"symbol": "WEAK", "exit_score": 90.0, "value_eur": 100.0}])
    monkeypatch.setattr(cycle_backtest, "run_backtest",
                        lambda **k: {"available": False, "reason": "Test ohne Backtest-Daten."})
    monkeypatch.setattr(news_data, "get_news", lambda *a, **k: [{"title": "BTC steigt", "source": "Test"}])
    return tmp_db


def _fake_response(**overrides):
    base = {
        "zusammenfassung": "Markt neutral bis leicht überhitzt.",
        "zyklus_phase": "Aufschwung",
        "phase_begruendung": "Trend positiv, Score im mittleren Bereich.",
        "top_wahrscheinlichkeit": 35,
        "argumente_top": ["Punkt A"],
        "argumente_dagegen": ["Punkt B"],
        "modell_abweichung": "Keine wesentliche Abweichung.",
        "beobachten": ["ETF-Flows beobachten"],
        "unsicherheit": "Regulatorische Ereignisse nicht vorhersehbar.",
    }
    base.update(overrides)
    return base


def test_run_cycle_analysis_success(tmp_db, monkeypatch):
    fake = _fake_response()
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.02, "input": 10, "output": 5}, None))

    result = cycle_analyst.run_cycle_analysis("claude-haiku-4-5")

    assert "error" not in result
    assert result["top_wahrscheinlichkeit"] == 35
    assert result["zyklus_phase"] == "Aufschwung"
    assert result["cycle_score"]["score"] == 42.0

    runs = tmp_db.list_agent_runs()
    assert len(runs) == 1
    assert runs[0]["mode"] == "cycle"
    assert runs[0]["target"] == "ZYKLUS-KRYPTO"
    assert runs[0]["total_score"] == 35


def test_run_cycle_analysis_clamps_out_of_range_probability(tmp_db, monkeypatch):
    fake = _fake_response(top_wahrscheinlichkeit=150)
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.01}, None))
    result = cycle_analyst.run_cycle_analysis("claude-haiku-4-5")
    assert result["top_wahrscheinlichkeit"] == 100


def test_run_cycle_analysis_no_score_returns_error_without_llm_call(tmp_db, monkeypatch):
    monkeypatch.setattr(cycle, "cycle_score", lambda: {"score": None})
    called = []
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: called.append(1) or (None, {}, None))

    result = cycle_analyst.run_cycle_analysis("claude-haiku-4-5")
    assert "error" in result
    assert called == []  # kein API-Call ohne berechenbaren Score - spart Kosten
    assert tmp_db.list_agent_runs() == []


def test_run_cycle_analysis_llm_error_logs_run_when_billed(tmp_db, monkeypatch):
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (None, {"cost_usd": 0.03}, "max_tokens erreicht"))

    result = cycle_analyst.run_cycle_analysis("claude-haiku-4-5")
    assert result["error"] == "max_tokens erreicht"
    runs = tmp_db.list_agent_runs()
    assert len(runs) == 1  # trotz Fehler geloggt, da bereits abgerechnet (cost_usd > 0)
    assert runs[0]["recommendation"] == "Fehlgeschlagen"


def test_run_cycle_analysis_llm_error_no_log_without_cost(tmp_db, monkeypatch):
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (None, {"cost_usd": 0.0}, "Netzwerkfehler"))
    cycle_analyst.run_cycle_analysis("claude-haiku-4-5")
    assert tmp_db.list_agent_runs() == []


def test_build_prompt_contains_all_sections(tmp_db):
    prompt = cycle_analyst.build_prompt(_fake_cycle_score())
    for header in ("ZYKLUS-INDIKATOREN", "HISTORISCHER VERGLEICH", "TRIGGER-PREISE",
                  "DEPOT-KONTEXT", "SCHLAGZEILEN", "BACKTEST", "RISIKOPROFIL"):
        assert header in prompt
    assert "WEAK" in prompt  # Ausstiegs-Rangliste eingebunden
    assert "BTC steigt" in prompt  # Schlagzeile eingebunden


def test_estimate_cost_positive():
    assert cycle_analyst.estimate_cost("claude-haiku-4-5") > 0


# --- Stufen-Vorschlag (suggest_ladder_stages) ---

def _fake_ladder_response(**overrides):
    base = {
        "verkauf_stufe1": 65.0, "verkauf_stufe2": 75.0, "verkauf_stufe3": 85.0,
        "kauf_stufe1": 25.0, "kauf_stufe2": 18.0, "kauf_stufe3": 12.0,
        "begruendung": "Score aktuell im mittleren Bereich, moderate Staffelung sinnvoll.",
    }
    base.update(overrides)
    return base


def test_estimate_ladder_suggestion_cost_positive():
    assert cycle_analyst.estimate_ladder_suggestion_cost("claude-haiku-4-5") > 0


def test_build_ladder_suggestion_prompt_crypto_includes_backtest(tmp_db):
    prompt = cycle_analyst.build_ladder_suggestion_prompt(
        "crypto", _fake_cycle_score(), [65.0, 75.0, 85.0], [25.0, 18.0, 12.0])
    assert "BACKTEST DER AKTUELLEN VERKAUFS-SCHWELLEN" in prompt
    assert "MVRV Z-Score" in prompt
    assert "65 / 75 / 85" in prompt


def test_build_ladder_suggestion_prompt_stock_has_no_backtest_section():
    temp = {"score": 55.0, "coverage_pct": 100.0,
           "breakdown": [{"label": "VIX", "text": "18.0", "weight_pct": 30.0}]}
    prompt = cycle_analyst.build_ladder_suggestion_prompt(
        "stock", temp, [65.0, 75.0, 85.0], [25.0, 18.0, 12.0])
    assert "BACKTEST" not in prompt
    assert "Markt-Temperatur" in prompt


def test_suggest_ladder_stages_success(tmp_db, monkeypatch):
    fake = _fake_ladder_response()
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.005}, None))

    result = cycle_analyst.suggest_ladder_stages(
        "crypto", _fake_cycle_score(), [65.0, 75.0, 85.0], [25.0, 18.0, 12.0], "claude-haiku-4-5")

    assert "error" not in result
    assert result["sell"] == [65.0, 75.0, 85.0]
    assert result["buy"] == [25.0, 18.0, 12.0]
    assert "moderate Staffelung" in result["begruendung"]

    runs = tmp_db.list_agent_runs()
    assert len(runs) == 1
    assert runs[0]["mode"] == "ladder_suggestion"
    assert runs[0]["target"] == "LADDER-CRYPTO"


def test_suggest_ladder_stages_stock_target(tmp_db, monkeypatch):
    fake = _fake_ladder_response()
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.005}, None))
    temp = {"score": 55.0, "coverage_pct": 100.0,
           "breakdown": [{"label": "VIX", "text": "18.0", "weight_pct": 30.0}]}

    cycle_analyst.suggest_ladder_stages("stock", temp, [65.0, 75.0, 85.0], [25.0, 18.0, 12.0],
                                        "claude-haiku-4-5")
    runs = tmp_db.list_agent_runs()
    assert runs[0]["target"] == "LADDER-STOCK"


def test_suggest_ladder_stages_clamps_out_of_range(tmp_db, monkeypatch):
    fake = _fake_ladder_response(verkauf_stufe1=150.0, kauf_stufe3=-20.0)
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.005}, None))

    result = cycle_analyst.suggest_ladder_stages(
        "crypto", _fake_cycle_score(), [65.0, 75.0, 85.0], [25.0, 18.0, 12.0], "claude-haiku-4-5")
    assert result["sell"][0] == 100.0
    assert result["buy"][2] == 0.0


def test_suggest_ladder_stages_error_logs_when_billed(tmp_db, monkeypatch):
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (None, {"cost_usd": 0.01}, "max_tokens erreicht"))
    result = cycle_analyst.suggest_ladder_stages(
        "crypto", _fake_cycle_score(), [65.0, 75.0, 85.0], [25.0, 18.0, 12.0], "claude-haiku-4-5")
    assert result["error"] == "max_tokens erreicht"
    runs = tmp_db.list_agent_runs()
    assert len(runs) == 1
    assert runs[0]["recommendation"] == "Fehlgeschlagen"


def test_suggest_ladder_stages_error_no_log_without_cost(tmp_db, monkeypatch):
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (None, {"cost_usd": 0.0}, "Netzwerkfehler"))
    cycle_analyst.suggest_ladder_stages(
        "crypto", _fake_cycle_score(), [65.0, 75.0, 85.0], [25.0, 18.0, 12.0], "claude-haiku-4-5")
    assert tmp_db.list_agent_runs() == []


# --- A4: Depot-Block-Text richtet sich nach tatsächlichem BTC-Bestand ---

def test_depot_block_mentions_btc_when_held(tmp_db, monkeypatch):
    monkeypatch.setattr(exit_ranking, "exit_ranking",
                        lambda: [{"symbol": "BTC", "exit_score": 10.0, "value_eur": 500.0},
                                {"symbol": "WEAK", "exit_score": 90.0, "value_eur": 100.0}])
    text = cycle_analyst._depot_block()
    assert "BTC ist Teil des Bestands." in text
    assert "kein BTC im Bestand" not in text


def test_depot_block_notes_no_btc_when_absent(tmp_db, monkeypatch):
    monkeypatch.setattr(exit_ranking, "exit_ranking",
                        lambda: [{"symbol": "WEAK", "exit_score": 90.0, "value_eur": 100.0}])
    text = cycle_analyst._depot_block()
    assert "reines Alt-Depot, kein BTC im Bestand." in text


# --- Markt-Temperatur- und Altcoin-Block im Prompt ---

def _fake_temp():
    return {
        "score": 55.0, "classification": "Neutral", "coverage_pct": 100.0,
        "breakdown": [{"key": "fear_greed", "label": "Fear & Greed Index",
                      "text": "60/100", "weight_pct": 25.0}],
    }


def _fake_alt():
    return {
        "score": 72.0, "regime": "Alt-Überhitzung", "basket_size": 9, "limited": False,
        "breakdown": [{"key": "alt_extension", "label": "Alt-Ausdehnung",
                      "text": "1.30", "weight_pct": 35.0}],
    }


def test_build_prompt_contains_market_temp_and_alt_blocks(tmp_db):
    prompt = cycle_analyst.build_prompt(_fake_cycle_score(), _fake_temp(), _fake_alt())
    assert "MARKT-TEMPERATUR" in prompt
    assert "ALTCOIN-ÜBERHITZUNG" in prompt
    assert "Fear & Greed Index" in prompt
    assert "Alt-Ausdehnung" in prompt
    assert "Alt-Überhitzung" in prompt


def test_build_prompt_without_temp_alt_says_not_supplied(tmp_db):
    prompt = cycle_analyst.build_prompt(_fake_cycle_score())
    assert "nicht mitgegeben" in prompt


def test_run_cycle_analysis_with_supplied_cycle_skips_recompute(tmp_db, monkeypatch):
    """Wird `cycle` übergeben, darf cycle.cycle_score() NICHT nochmal aufgerufen
    werden - das würde den teuren Netz-Roundtrip verdoppeln."""
    called = []
    monkeypatch.setattr(cycle, "cycle_score", lambda: called.append(1) or _fake_cycle_score())
    fake = _fake_response()
    monkeypatch.setattr(cycle_analyst, "run_json_agent",
                        lambda *a, **k: (fake, {"cost_usd": 0.02}, None))

    result = cycle_analyst.run_cycle_analysis(
        "claude-haiku-4-5", cycle=_fake_cycle_score(), temp=_fake_temp(), alt=_fake_alt())

    assert "error" not in result
    assert called == []  # cycle_score() wurde NICHT erneut aufgerufen
