"""Tests für analysis/market_timing.py (rein rechnerisch, kein Netzzugriff)."""
import pandas as pd
import pytest

from analysis import market_timing


def test_mayer_multiple_computes_ratio(monkeypatch):
    from data import stocks as stock_data
    idx = pd.date_range("2024-01-01", periods=250, freq="D")
    # Konstanter Kurs 100 -> MA200 konvergiert gegen 100 -> Multiple ~1.0
    df = pd.DataFrame({"Close": [100.0] * 250}, index=idx)
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: df)
    multiple = market_timing.mayer_multiple()
    assert multiple == pytest.approx(1.0, rel=0.01)


def test_mayer_multiple_no_data_returns_none(monkeypatch):
    from data import stocks as stock_data
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: None)
    assert market_timing.mayer_multiple() is None


def test_score_fear_greed_none_when_missing():
    assert market_timing._score_fear_greed(None) is None


def test_score_fear_greed_passes_value_through():
    score, text = market_timing._score_fear_greed({"value": 72, "classification": "Greed"})
    assert score == 72.0
    assert "Greed" in text


def test_score_mayer_extremes():
    low, _ = market_timing._score_mayer(0.5)
    mid, _ = market_timing._score_mayer(1.2)
    high, _ = market_timing._score_mayer(2.6)
    assert low < 20
    assert 40 <= mid <= 60
    assert high == 100.0
    assert market_timing._score_mayer(None) is None


def test_score_btc_dominance_inverse_relationship():
    """Niedrige Dominanz = Alt-Gier (hoher Score), hohe Dominanz = niedriger Score."""
    low_dom, _ = market_timing._score_btc_dominance({"btc_dominance": 38})
    high_dom, _ = market_timing._score_btc_dominance({"btc_dominance": 68})
    assert low_dom > high_dom
    assert market_timing._score_btc_dominance(None) is None
    assert market_timing._score_btc_dominance({"btc_dominance": None}) is None


def test_score_stablecoin_dominance_inverse_relationship():
    """Hohe Stablecoin-Dominanz = Angst (niedriger Score)."""
    low_stable, _ = market_timing._score_stablecoin_dominance({"stablecoin_dominance": 3})
    high_stable, _ = market_timing._score_stablecoin_dominance({"stablecoin_dominance": 11})
    assert low_stable > high_stable


def test_score_meme_momentum_direction():
    up, _ = market_timing._score_meme({"change_24h_pct": 10, "market_cap": 3e10})
    down, _ = market_timing._score_meme({"change_24h_pct": -10, "market_cap": 3e10})
    assert up > down


def test_score_breadth_direct_mapping():
    score, text = market_timing._score_breadth(
        {"pct_outperforming": 75.0, "sample_size": 48, "btc_change_30d": 5.0})
    assert score == 75.0
    assert "75" in text


def test_market_temperature_all_available():
    readings = {
        "fear_greed": {"value": 72, "classification": "Greed"},
        "mayer": 1.2,
        "breadth": {"pct_outperforming": 60.0, "sample_size": 45, "btc_change_30d": 3.0},
        "btc_dominance": {"btc_dominance": 50, "stablecoin_dominance": 6},
        "meme": {"change_24h_pct": 2.0, "market_cap": 3e10},
        "stablecoin_dominance": {"btc_dominance": 50, "stablecoin_dominance": 6},
    }
    result = market_timing.market_temperature(readings)
    assert result["score"] is not None
    assert 0 <= result["score"] <= 100
    assert len(result["breakdown"]) == 6
    assert result["unavailable"] == []
    # Renormierte Gewichte summieren auf 100
    assert sum(r["weight_pct"] for r in result["breakdown"]) == pytest.approx(100.0, abs=0.1)


def test_market_temperature_renormalizes_missing_indicators():
    """Fehlende Quellen duerfen den Score nicht systematisch verzerren -
    die verfuegbaren Gewichte muessen bei Ausfall auf 100% aufsummieren."""
    readings = {
        "fear_greed": {"value": 80, "classification": "Greed"},
        "mayer": None,
        "breadth": None,
        "btc_dominance": None,
        "meme": None,
        "stablecoin_dominance": None,
    }
    result = market_timing.market_temperature(readings)
    assert result["score"] == pytest.approx(80.0)  # einzige Quelle -> 100% Gewicht
    assert len(result["breakdown"]) == 1
    assert result["breakdown"][0]["weight_pct"] == pytest.approx(100.0)
    assert len(result["unavailable"]) == 5


def test_market_temperature_all_missing_returns_none_score():
    readings = {k: None for k in
               ("fear_greed", "mayer", "breadth", "btc_dominance", "meme", "stablecoin_dominance")}
    result = market_timing.market_temperature(readings)
    assert result["score"] is None
    assert result["breakdown"] == []
    assert len(result["unavailable"]) == 6


def test_classify_bands():
    assert market_timing._classify(10) == "Extreme Angst"
    assert market_timing._classify(30) == "Angst"
    assert market_timing._classify(50) == "Neutral"
    assert market_timing._classify(70) == "Gier"
    assert market_timing._classify(90) == "Extreme Gier"
