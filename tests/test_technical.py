import numpy as np
import pandas as pd

from analysis import risk, technical


def _synthetic_df(n=300, seed=42):
    rng = np.random.default_rng(seed)
    prices = 100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"Close": prices}, index=idx)


def test_indicators_and_score_bounds():
    df = _synthetic_df()
    s = technical.summarize(df)
    assert 0 <= s["rsi"] <= 100
    assert 0 <= s["t_score"] <= 100
    assert s["ma200"] is not None
    assert s["lower_band"] < s["upper_band"]
    assert set(s["fibs"].keys()) == {"0%", "23.6%", "38.2%", "50%", "61.8%", "100%"}
    for col in ("MA20", "MA50", "MA200", "RSI", "MACD", "MACD_Hist"):
        assert col in s["df"].columns


def test_rsi_extremes():
    up = pd.Series(np.linspace(100, 200, 100))
    down = pd.Series(np.linspace(200, 100, 100))
    assert technical.calculate_rsi(up).iloc[-1] > 90
    assert technical.calculate_rsi(down).iloc[-1] < 10


def test_asset_risk():
    df = _synthetic_df()
    r = risk.asset_risk(df)
    assert r["volatilitaet_pct"] > 0
    assert r["max_drawdown_pct"] <= 0
    assert isinstance(r["sharpe"], float)


def test_concentration():
    c = risk.concentration({"NVDA": 8000, "BTC": 1500, "ETH": 500})
    assert c["groesste_position"] == "NVDA"
    assert c["groesste_position_pct"] == 80.0
    assert c["anzahl_positionen"] == 3
    assert c["hhi"] > 0.25  # stark konzentriert


def test_correlation_matrix():
    a = _synthetic_df(seed=1)
    b = _synthetic_df(seed=2)
    corr = risk.correlation_matrix({"A": a, "B": b})
    assert corr is not None
    assert corr.loc["A", "A"] == 1.0
    assert risk.correlation_matrix({"A": a}) is None
