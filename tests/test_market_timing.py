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
    # Nur 25% der Gesamt-Gewichtung verfuegbar (fear_greed-Gewicht 25 von 100) ->
    # trotz Score 80 keine "Extreme Gier"-Aussage aus einer einzigen Quelle.
    assert result["coverage_pct"] == pytest.approx(25.0)
    assert result["classification"] == "Gier"


def test_market_temperature_full_coverage_allows_extreme_classification():
    readings = {
        "fear_greed": {"value": 95, "classification": "Extreme Greed"},
        "mayer": 2.5, "breadth": {"pct_outperforming": 90.0, "sample_size": 45, "btc_change_30d": 3.0},
        "btc_dominance": {"btc_dominance": 40, "stablecoin_dominance": 4},
        "meme": {"change_24h_pct": 10.0, "market_cap": 3e10},
        "stablecoin_dominance": {"btc_dominance": 40, "stablecoin_dominance": 4},
    }
    result = market_timing.market_temperature(readings)
    assert result["coverage_pct"] == pytest.approx(100.0)
    assert result["classification"] == "Extreme Gier"


def test_market_temperature_all_missing_returns_none_score():
    readings = {k: None for k in
               ("fear_greed", "mayer", "breadth", "btc_dominance", "meme", "stablecoin_dominance")}
    result = market_timing.market_temperature(readings)
    assert result["score"] is None
    assert result["breakdown"] == []
    assert len(result["unavailable"]) == 6
    assert result["coverage_pct"] == 0.0


def test_classify_bands():
    assert market_timing._classify(10) == "Extreme Angst"
    assert market_timing._classify(30) == "Angst"
    assert market_timing._classify(50) == "Neutral"
    assert market_timing._classify(70) == "Gier"
    assert market_timing._classify(90) == "Extreme Gier"


def test_classify_softens_extreme_below_coverage_threshold():
    assert market_timing._classify(10, coverage_pct=25.0) == "Angst"
    assert market_timing._classify(90, coverage_pct=25.0) == "Gier"
    # An der Schwelle selbst (>= 60%) bleibt die Extrem-Einordnung erlaubt.
    assert market_timing._classify(90, coverage_pct=60.0) == "Extreme Gier"


# --- Aktien-Indikatoren (Rohwerte) ---

def test_sp500_ma200_ratio_computes(monkeypatch):
    from data import stocks as stock_data
    idx = pd.date_range("2024-01-01", periods=250, freq="D")
    df = pd.DataFrame({"Close": [100.0] * 250}, index=idx)
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: df)
    assert market_timing.sp500_ma200_ratio() == pytest.approx(1.0, rel=0.01)


def test_vix_level_reads_latest_close(monkeypatch):
    from data import stocks as stock_data
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    df = pd.DataFrame({"Close": [15.0] * 29 + [22.5]}, index=idx)
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: df)
    assert market_timing.vix_level() == pytest.approx(22.5)


def test_vix_level_no_data_returns_none(monkeypatch):
    from data import stocks as stock_data
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: None)
    assert market_timing.vix_level() is None


def _ratio_pair_history(monkeypatch, sym_a, val_a, sym_b, val_b, days=250):
    """Zwei Kurshistorien mocken, je nach angefragtem Symbol - fuer breadth_ratio()
    (RSP/SPY) und risk_appetite_ratio() (HYG/IEF)."""
    from data import stocks as stock_data
    idx = pd.date_range("2024-01-01", periods=days, freq="D")
    df_a = pd.DataFrame({"Close": [val_a] * days}, index=idx)
    df_b = pd.DataFrame({"Close": [val_b] * days}, index=idx)

    def fake_get_history(symbol, *a, **k):
        return df_a if symbol == sym_a else df_b if symbol == sym_b else None

    monkeypatch.setattr(stock_data, "get_history", fake_get_history)


def test_breadth_ratio_flat_history_is_100(monkeypatch):
    """Konstantes Verhaeltnis -> aktueller Wert == eigener MA200 -> 100%."""
    _ratio_pair_history(monkeypatch, "RSP", 50.0, "SPY", 100.0)
    assert market_timing.breadth_ratio() == pytest.approx(100.0, rel=0.01)


def test_breadth_ratio_missing_series_returns_none(monkeypatch):
    from data import stocks as stock_data
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: None)
    assert market_timing.breadth_ratio() is None


def test_risk_appetite_ratio_flat_history_is_100(monkeypatch):
    _ratio_pair_history(monkeypatch, "HYG", 80.0, "IEF", 95.0)
    assert market_timing.risk_appetite_ratio() == pytest.approx(100.0, rel=0.01)


def test_risk_appetite_ratio_missing_series_returns_none(monkeypatch):
    from data import stocks as stock_data
    monkeypatch.setattr(stock_data, "get_history", lambda *a, **k: None)
    assert market_timing.risk_appetite_ratio() is None


# --- Aktien-Scorer ---

def test_score_vix_inverse_relationship():
    """Niedriger VIX = Sorglosigkeit/Gier (hoher Score), hoher VIX = Angst (niedriger Score)."""
    calm, _ = market_timing._score_vix(13.0)
    panic, _ = market_timing._score_vix(38.0)
    assert calm > panic
    assert market_timing._score_vix(None) is None


def test_score_sp500_ma200_direct_mapping():
    below, _ = market_timing._score_sp500_ma200(0.85)
    above, _ = market_timing._score_sp500_ma200(1.20)
    assert below < above
    assert market_timing._score_sp500_ma200(None) is None


def test_score_stock_breadth_inverse_relationship():
    """Schmale Marktbreite (RSP/SPY unter MA200) ist ein Spaetzyklus-Warnsignal -
    wie jedes Warnsignal in diesem System (100 = maximale Gier/Warnung) gibt sie
    einen HOHEN Score, breite/gesunde Fuehrung einen niedrigen."""
    narrow, _ = market_timing._score_stock_breadth(97.0)
    broad, _ = market_timing._score_stock_breadth(103.0)
    assert narrow > broad
    assert market_timing._score_stock_breadth(None) is None


def test_score_risk_appetite_direct_mapping():
    risk_off, _ = market_timing._score_risk_appetite(97.0)
    risk_on, _ = market_timing._score_risk_appetite(103.0)
    assert risk_off < risk_on
    assert market_timing._score_risk_appetite(None) is None


def test_labels_for_returns_market_specific_labels():
    crypto_labels = market_timing.labels_for("crypto")
    stock_labels = market_timing.labels_for("stock")
    assert "fear_greed" in crypto_labels
    assert "vix" in stock_labels
    assert "vix" not in crypto_labels


def test_market_temperature_stock_all_available():
    readings = {"vix": 15.0, "sp500_ma200": 1.05, "breadth": 100.0, "risk_appetite": 101.0}
    result = market_timing.market_temperature(readings, market="stock")
    assert result["score"] is not None
    assert 0 <= result["score"] <= 100
    assert len(result["breakdown"]) == 4
    assert result["unavailable"] == []
    assert sum(r["weight_pct"] for r in result["breakdown"]) == pytest.approx(100.0, abs=0.1)


def test_market_temperature_stock_renormalizes_missing():
    readings = {"vix": 15.0, "sp500_ma200": None, "breadth": None, "risk_appetite": None}
    result = market_timing.market_temperature(readings, market="stock")
    # market_temperature() rundet auf 1 Nachkommastelle - Erwartungswert mitrunden.
    assert result["score"] == pytest.approx(round(market_timing._score_vix(15.0)[0], 1))
    assert len(result["breakdown"]) == 1
    assert result["breakdown"][0]["weight_pct"] == pytest.approx(100.0)
    assert len(result["unavailable"]) == 3


def test_market_temperature_stock_all_missing_returns_none():
    readings = {"vix": None, "sp500_ma200": None, "breadth": None, "risk_appetite": None}
    result = market_timing.market_temperature(readings, market="stock")
    assert result["score"] is None
    assert result["breakdown"] == []
    assert len(result["unavailable"]) == 4


# --- DB-Migration: praefixlose Sentiment-Schluessel -> "crypto:"-Praefix ---

def test_migrate_sentiment_prefix_rewrites_legacy_keys(tmp_db):
    tmp_db.save_sentiment("overall", 55.0)  # simuliert praefixlosen Alt-Eintrag
    from core.db import _connect, _migrate_sentiment_prefix
    with _connect() as con:
        _migrate_sentiment_prefix(con)
    rows = tmp_db.list_sentiment()
    assert rows[0]["indicator"] == "crypto:overall"


def test_migrate_sentiment_prefix_is_idempotent(tmp_db):
    tmp_db.save_sentiment("crypto:overall", 60.0)
    tmp_db.save_sentiment("stock:overall", 40.0)
    from core.db import _connect, _migrate_sentiment_prefix
    with _connect() as con:
        _migrate_sentiment_prefix(con)
    rows = {r["indicator"]: r["value"] for r in tmp_db.list_sentiment()}
    assert rows == {"crypto:overall": 60.0, "stock:overall": 40.0}


# --- Zyklus-Position: eigene Stufen (active_ladder_tier) ---

def test_active_ladder_tier_sell_counts_reached_thresholds():
    thresholds = [65.0, 75.0, 85.0]
    assert market_timing.active_ladder_tier(50, thresholds, "sell") == 0
    assert market_timing.active_ladder_tier(65, thresholds, "sell") == 1   # exakt auf Schwelle
    assert market_timing.active_ladder_tier(70, thresholds, "sell") == 1
    assert market_timing.active_ladder_tier(80, thresholds, "sell") == 2
    assert market_timing.active_ladder_tier(90, thresholds, "sell") == 3


def test_active_ladder_tier_buy_counts_reached_thresholds():
    thresholds = [25.0, 18.0, 12.0]
    assert market_timing.active_ladder_tier(50, thresholds, "buy") == 0
    assert market_timing.active_ladder_tier(25, thresholds, "buy") == 1   # exakt auf Schwelle
    assert market_timing.active_ladder_tier(20, thresholds, "buy") == 1
    assert market_timing.active_ladder_tier(15, thresholds, "buy") == 2
    assert market_timing.active_ladder_tier(5, thresholds, "buy") == 3


def test_active_ladder_tier_none_score_and_empty_thresholds():
    assert market_timing.active_ladder_tier(None, [65, 75, 85], "sell") == 0
    assert market_timing.active_ladder_tier(90, [], "sell") == 0
