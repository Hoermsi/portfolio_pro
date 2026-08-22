"""Tests für analysis/cycle.py (rein rechnerisch, Netzquellen gemockt).

Muster: tests/test_technical.py (synthetische Serien, Assertions auf
Invarianten/Grenzen) + tests/test_market_timing.py (monkeypatch auf die
Rohwert-Helfer statt auf HTTP-Ebene, da die Scoring-Logik selbst rein ist).
"""
import numpy as np
import pandas as pd
import pytest

from analysis import cycle


def _uptrend_price_series(n=1500, blowoff_days=200, r1=0.0010, r2=0.006) -> pd.Series:
    """Deterministisch (keine Zufallskomponente -> nie flaky): moderates
    Wachstum, dann eine "Blow-Off"-Beschleunigung in den letzten Tagen - wie
    ein später Bullenmarkt. Eine schlicht konstante Wachstumsrate würde NICHT
    reichen: Kurs/SMA200 & Co. sind skaleninvariant und blieben über die
    ganze Serie flach (verifiziert) - erst die Beschleunigung am Ende hebt
    die gleitenden Verhältnisse auf ein echtes historisches Hoch."""
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    base_n = n - blowoff_days
    base = 100.0 * (1 + r1) ** np.arange(base_n)
    blow = base[-1] * (1 + r2) ** np.arange(1, blowoff_days + 1)
    return pd.Series(np.concatenate([base, blow]), index=idx)


def _crashed_price_series(n=1500, blowoff_days=200, crash_days=150) -> pd.Series:
    """Wie oben (bis zum Blow-Off-Hoch), dann ein deterministischer, monotoner
    Absturz - liefert reproduzierbar 'trend_up=False' und tiefen Drawdown."""
    top = _uptrend_price_series(n=n - crash_days, blowoff_days=blowoff_days)
    crash_idx = pd.date_range(top.index[-1] + pd.Timedelta(days=1), periods=crash_days, freq="D")
    crash = pd.Series(top.iloc[-1] * np.linspace(1.0, 0.2, crash_days), index=crash_idx)
    return pd.concat([top, crash])


def _ascending_series(n=200) -> pd.Series:
    """Streng aufsteigende Testserie - der letzte Wert ist per Konstruktion
    das Maximum, also Perzentil 100."""
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.Series(np.linspace(1.0, float(n), n), index=idx)


def _series_with_percentile(pct: float, n=200) -> pd.Series:
    """Deterministische Serie, deren letzter Wert näherungsweise beim
    Perzentil `pct` (0-100) der vorangehenden n-1 Werte liegt - für Tests,
    die einen MITTLEREN (nicht extremen) Baustein-Score brauchen."""
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    history = list(np.linspace(1.0, 100.0, n - 1))
    return pd.Series(history + [float(pct)], index=idx)


# --- _percentile_of_last ---

def test_percentile_of_last_max_is_100():
    s = _ascending_series(100)
    assert cycle._percentile_of_last(s) == 100.0


def test_percentile_of_last_min_is_smallest_bucket():
    s = _ascending_series(100)[::-1]  # jetzt absteigend, letzter Wert = Minimum
    s.index = pd.date_range("2020-01-01", periods=100, freq="D")
    pct = cycle._percentile_of_last(s)
    assert pct == pytest.approx(1.0, abs=0.01)  # 1 von 100 Werten <= Minimum


def test_percentile_of_last_none_below_min_points():
    s = _ascending_series(10)
    assert cycle._percentile_of_last(s) is None


def test_percentile_of_last_none_for_none_input():
    assert cycle._percentile_of_last(None) is None


# --- Serien-Bausteine (rein aus dem Preis ableitbar) ---

def test_drawdown_series_zero_at_ath():
    price = pd.Series([100.0, 120.0, 90.0, 130.0], index=pd.date_range("2024-01-01", periods=4))
    dd = cycle._drawdown_series(price)
    assert dd.iloc[-1] == pytest.approx(0.0)  # 130 ist neues ATH
    assert dd.iloc[2] < 0  # 90 liegt unter dem bisherigen ATH von 120


def test_mayer_series_near_one_for_flat_price():
    price = pd.Series([100.0] * 250, index=pd.date_range("2024-01-01", periods=250))
    s = cycle._mayer_series(price)
    assert s.iloc[-1] == pytest.approx(1.0, abs=0.01)


# --- classify_regime ---

def test_classify_regime_unknown_without_data():
    regime, _ = cycle.classify_regime(None, True, -10.0, 100.0)
    assert regime == "Unbekannt"


def test_classify_regime_unknown_low_coverage():
    regime, _ = cycle.classify_regime(80.0, True, -5.0, 20.0)
    assert regime == "Unbekannt"


def test_classify_regime_euphoria():
    regime, _ = cycle.classify_regime(85.0, True, -5.0, 100.0)
    assert regime == "Euphorie"


def test_classify_regime_baer_on_downtrend():
    regime, _ = cycle.classify_regime(40.0, False, -30.0, 100.0)
    assert regime == "Bär"


def test_classify_regime_akkumulation_deep_drawdown_recovering():
    regime, _ = cycle.classify_regime(20.0, True, -70.0, 100.0)
    assert regime == "Akkumulation"


def test_classify_regime_distribution_topping():
    regime, _ = cycle.classify_regime(65.0, False, -15.0, 100.0)
    assert regime == "Distribution"


# --- cycle_score() Integration (alle Netzquellen gemockt) ---

def test_cycle_score_none_without_any_source(monkeypatch):
    monkeypatch.setattr(cycle, "btc_price_series", lambda: None)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: None)
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: None)

    result = cycle.cycle_score()
    assert result["score"] is None
    assert result["regime"] == "Unbekannt"
    assert result["breakdown"] == []
    assert result["coverage_pct"] == 0.0


def test_cycle_score_hot_market_scores_high_and_full_coverage(monkeypatch):
    price = _uptrend_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: _ascending_series(200))
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: _ascending_series(200))

    result = cycle.cycle_score()
    assert result["score"] is not None
    assert result["coverage_pct"] == 100.0
    assert len(result["breakdown"]) == 7
    assert result["unavailable"] == []
    # On-Chain + Sentiment sind künstlich aufs Maximum gesetzt (Perzentil 100),
    # Preis nahe ATH -> insgesamt hoher Score.
    assert result["score"] > 60
    assert result["regime"] in ("Später Bulle", "Euphorie")
    assert result["drawdown_pct"] == pytest.approx(0.0, abs=1.0)


def test_cycle_score_cold_market_scores_low(monkeypatch):
    price = _crashed_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    # On-Chain/Sentiment künstlich aufs Minimum gesetzt (Serie steigt, letzter
    # Wert der für den Score verwendeten Reihe ist bewusst der niedrigste).
    cold_series = _ascending_series(200)[::-1]
    cold_series.index = pd.date_range("2020-01-01", periods=200, freq="D")
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: cold_series)
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: cold_series)

    result = cycle.cycle_score()
    assert result["score"] is not None
    assert result["score"] < 40
    assert result["regime"] == "Bär"


def test_cycle_score_missing_onchain_renormalizes(monkeypatch):
    """Fällt eine Quelle aus, wird sie aus 'unavailable' gemeldet und der Rest
    renormiert (weight_pct summiert weiter auf 100) statt den Score künstlich
    nach unten zu ziehen."""
    price = _uptrend_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: None)
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: _ascending_series(200))

    result = cycle.cycle_score()
    assert set(result["unavailable"]) == {"mvrv_z", "puell"}
    assert result["coverage_pct"] == pytest.approx(60.0)  # 100 - 29 - 11
    total_weight = sum(row["weight_pct"] for row in result["breakdown"])
    assert total_weight == pytest.approx(100.0, abs=0.5)


# --- trigger_price() ---

def test_trigger_price_returns_now_when_already_reached(monkeypatch):
    price = _uptrend_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: _ascending_series(200))
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: _ascending_series(200))

    result = cycle.cycle_score()
    trigger = cycle.trigger_price(threshold=1.0, cycle=result)  # trivial erreichbar
    assert trigger == pytest.approx(result["price_now"])


def _cyclical_price_series(n=1500, cut=80) -> pd.Series:
    """Exponentieller Trend + überlagerte Jahres-Welle (sin) - im Unterschied
    zum reinen Blow-Off-Fixture oben hat diese Serie echte Auf- und Abwärts-
    phasen VOR dem aktuellen Rand, sodass die preisabhängigen Bausteine an
    einem gewählten Schnittpunkt einen MITTLEREN (nicht extremen) Perzentil-
    wert haben - nötig, um trigger_price() sinnvoll zu testen (muss Raum nach
    oben UND unten haben). `cut` (rückwärts vom Serienende) wurde einmalig so
    gewählt, dass alle Bausteine deutlich unter 100 liegen."""
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    t = np.arange(n)
    trend = 100.0 * (1.0015 ** t)
    wave = 1 + 0.35 * np.sin(2 * np.pi * t / 365.0)
    return pd.Series(trend * wave, index=idx).iloc[: n - cut]


def test_trigger_price_above_current_for_higher_threshold(monkeypatch):
    """On-Chain/Sentiment (nicht preisabhängig) auf einem mittleren Perzentil
    eingefroren, Preis-Serie an einem Punkt mit Luft nach oben - der Score
    muss unter der Schwelle liegen und über einen höheren Preis erreichbar
    sein."""
    price = _cyclical_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: _series_with_percentile(50, 200))
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: _series_with_percentile(50, 200))

    result = cycle.cycle_score()
    assert result["score"] < 55
    trigger = cycle.trigger_price(threshold=55.0, cycle=result)
    assert trigger is not None
    assert trigger > result["price_now"]


def test_trigger_price_none_when_unreachable(monkeypatch):
    """Zu viel Gewicht liegt auf eingefrorenen (nicht preisabhängigen)
    Bausteinen, die künstlich auf dem Minimum gehalten werden - selbst ein
    50x-Preis darf die 99 dann nicht erreichen."""
    price = _uptrend_price_series()
    monkeypatch.setattr(cycle, "btc_price_series", lambda: price)
    low_series = _ascending_series(200)[::-1]
    low_series.index = pd.date_range("2020-01-01", periods=200, freq="D")
    monkeypatch.setattr(cycle, "_onchain_series", lambda metric: low_series)
    monkeypatch.setattr(cycle, "_fear_greed_30d_series", lambda: low_series)

    result = cycle.cycle_score()
    trigger = cycle.trigger_price(threshold=99.0, cycle=result)
    assert trigger is None
