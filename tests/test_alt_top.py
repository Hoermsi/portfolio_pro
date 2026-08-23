"""Tests für analysis/alt_top.py (deterministische Preisreihen, keine
Zufallskomponente - Muster: tests/test_cycle.py). Kein Netzzugriff:
alt_top.basket_prices() wird komplett gemockt."""
import numpy as np
import pandas as pd
import pytest

from analysis import alt_top


def _uptrend_series(n=1200, blowoff_days=200, r1=0.0010, r2=0.006,
                    start="2020-01-01") -> pd.Series:
    """Deterministisch (Muster: test_cycle._uptrend_price_series): moderates
    Wachstum, dann Blow-Off-Beschleunigung am Ende."""
    idx = pd.date_range(start, periods=n, freq="D")
    base_n = n - blowoff_days
    base = 100.0 * (1 + r1) ** np.arange(base_n)
    blow = base[-1] * (1 + r2) ** np.arange(1, blowoff_days + 1)
    return pd.Series(np.concatenate([base, blow]), index=idx)


def _crashed_series(n=1200, blowoff_days=200, crash_days=300, r1=0.0010,
                    r2=0.006, start="2020-01-01") -> pd.Series:
    """Wie oben bis zum Blow-Off-Hoch, dann ein deterministischer, monotoner
    Absturz (Muster: test_cycle._crashed_price_series)."""
    top = _uptrend_series(n=n - crash_days, blowoff_days=blowoff_days,
                          r1=r1, r2=r2, start=start)
    crash_idx = pd.date_range(top.index[-1] + pd.Timedelta(days=1), periods=crash_days, freq="D")
    crash = pd.Series(top.iloc[-1] * np.linspace(1.0, 0.2, crash_days), index=crash_idx)
    return pd.concat([top, crash])


def _hot_basket() -> dict[str, pd.Series]:
    """Alts blasen stärker ab als BTC -> hohe Ausdehnung, hohe Streuung,
    hohe Alt-Breite und hoher Trend gegen BTC."""
    basket = {"BTC": _uptrend_series(r2=0.003)}
    for i, sym in enumerate(alt_top._BASKET):
        basket[sym] = _uptrend_series(r2=0.007 + i * 0.0005)
    return basket


def _cold_basket() -> dict[str, pd.Series]:
    """Alts sind gegenüber einem stabil bleibenden BTC abgestürzt -> niedrige
    Ausdehnung, niedrige Alt-Breite, niedriger Trend gegen BTC."""
    basket = {"BTC": _uptrend_series(r1=0.0010, r2=0.0010, blowoff_days=0)}
    for sym in alt_top._BASKET:
        basket[sym] = _crashed_series(r2=0.006)
    return basket


def test_alt_top_score_bounds(monkeypatch):
    monkeypatch.setattr(alt_top, "basket_prices", _hot_basket)
    hot = alt_top.alt_top_score()
    monkeypatch.setattr(alt_top, "basket_prices", _cold_basket)
    cold = alt_top.alt_top_score()

    assert hot["score"] is not None and cold["score"] is not None
    assert 0.0 <= hot["score"] <= 100.0
    assert 0.0 <= cold["score"] <= 100.0
    assert hot["score"] > cold["score"]


def test_era_gate_excludes_short_history(monkeypatch):
    basket = _hot_basket()
    # SOL hat nur 100 Tage eigene Historie - deutlich unter _MIN_COIN_HISTORY (365).
    short = _uptrend_series(n=100, blowoff_days=20, start=basket["BTC"].index[-100])
    basket["SOL"] = short
    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)

    result = alt_top.alt_top_score()
    assert result["basket_size"] == len(alt_top._BASKET) - 1  # SOL zaehlt heute nicht mit

    px, btc = alt_top._align(basket)
    eligible = alt_top._eligible(px)
    assert not bool(eligible["SOL"].iloc[-1])
    assert bool(eligible["ETH"].iloc[-1])


def test_renormalises_when_block_unavailable(monkeypatch):
    """alt_vs_btc_trend faellt aus (Korb zu kurz fuer den eigenen 200-Tage-
    Schnitt des Verhaeltnisses) -> coverage_pct sinkt, die verbleibenden
    weight_pct-Werte summieren weiterhin auf ~100."""
    basket = _hot_basket()
    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)
    monkeypatch.setattr(alt_top, "_alt_vs_btc_trend_series",
                        lambda px, btc, eligible: pd.Series(dtype=float))

    result = alt_top.alt_top_score()
    assert "alt_vs_btc_trend" in result["unavailable"]
    assert result["coverage_pct"] < 100.0
    total_weight = sum(row["weight_pct"] for row in result["breakdown"])
    assert total_weight == pytest.approx(100.0, abs=0.5)


def test_below_min_basket_returns_none_score(monkeypatch):
    basket = {"BTC": _hot_basket()["BTC"],
             "ETH": _uptrend_series(r2=0.008),
             "LINK": _uptrend_series(r2=0.008)}
    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)

    result = alt_top.alt_top_score()
    assert result["score"] is None
    # Vertrags-Keys bleiben trotzdem vorhanden (Muster cycle.cycle_score()).
    for key in ("score", "regime", "regime_reason", "breakdown", "unavailable",
               "coverage_pct", "basket_size", "alarm"):
        assert key in result


def test_walk_forward_last_equals_live(monkeypatch):
    """Der Lookahead-Waechter: der letzte Wert der Walk-Forward-Serie muss dem
    Live-Score entsprechen (Muster: test_cycle_backtest's Aequivalenz-Test)."""
    basket = _hot_basket()
    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)

    live = alt_top.alt_top_score()
    px, btc = alt_top._align(basket)
    series = alt_top.walk_forward_alt_score(px, btc)

    assert series.dropna().iloc[-1] == pytest.approx(live["score"], abs=0.05)


def test_breadth_bounds():
    basket = _hot_basket()
    px, btc = alt_top._align({k: v for k, v in basket.items() if k != "BTC"} | {"BTC": basket["BTC"]})
    eligible = alt_top._eligible(px)
    breadth = alt_top._alt_breadth_btc_series(px, btc, eligible)
    valid = breadth.dropna()
    assert not valid.empty
    assert ((valid >= 0.0) & (valid <= 100.0)).all()


@pytest.mark.parametrize("score,trend_up,coverage,expected", [
    (None, True, 100.0, "Unbekannt"),
    (50.0, True, 10.0, "Unbekannt"),
    (65.0, False, 100.0, "Alt-Distribution"),
    (75.0, True, 100.0, "Alt-Überhitzung"),
    (55.0, True, 100.0, "Erhöht"),
    (40.0, True, 100.0, "Neutral"),
    (10.0, True, 100.0, "Ausverkauf"),
])
def test_classify_alt_regime(score, trend_up, coverage, expected):
    regime, _ = alt_top._classify_alt_regime(score, trend_up, coverage)
    assert regime == expected


def test_basket_size_excludes_coin_missing_today_despite_long_history(monkeypatch):
    """Kernfix: ein Coin mit ausreichender historischer Eignung, der aber HEUTE
    keinen Kurs liefert (z.B. API-Ausfall), darf nicht als verfügbar zählen -
    die frühere Implementierung zählte ihn über die rein kumulative Eignung mit."""
    basket = _hot_basket()
    # SOL fällt in den letzten 5 Tagen aus - mehr als das ffill(limit=3)-Fenster.
    basket["SOL"] = basket["SOL"].iloc[:-5]

    px, btc = alt_top._align(basket)
    eligible = alt_top._eligible(px)
    available = alt_top._available_today(px, eligible)

    assert bool(eligible["SOL"].iloc[-1])   # historisch weiterhin "eligible"
    assert not bool(available["SOL"])       # aber heute nicht tatsächlich verfügbar

    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)
    result = alt_top.alt_top_score()
    assert result["basket_size"] == len(alt_top._BASKET) - 1  # SOL zählt nicht mehr mit


def test_score_history_matches_walk_forward_alt_score(monkeypatch):
    """score_history() ist nur ein basket_prices()/_align()-Wrapper - das
    Ergebnis muss exakt walk_forward_alt_score(px, btc) entsprechen (Muster:
    test_walk_forward_last_equals_live)."""
    basket = _hot_basket()
    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)

    via_wrapper = alt_top.score_history()
    px, btc = alt_top._align(basket)
    direct = alt_top.walk_forward_alt_score(px, btc)
    pd.testing.assert_series_equal(via_wrapper, direct)


def test_score_history_empty_without_basket(monkeypatch):
    monkeypatch.setattr(alt_top, "basket_prices", lambda: {})
    result = alt_top.score_history()
    assert result.empty


def test_limited_when_too_few_coins_available_today(monkeypatch):
    """Unter _MIN_BASKET_FOR_SIGNAL (6) aktuell verfügbaren Coins wird 'limited'
    gesetzt und 'alarm' bleibt IMMER False, auch bei sehr hohem Score."""
    basket = _hot_basket()
    # Nur noch 5 Coins liefern heute einen Kurs (über _MIN_BASKET=3, aber unter
    # der Signal-Schwelle von 6).
    for sym in alt_top._BASKET[5:]:
        basket[sym] = basket[sym].iloc[:-5]

    monkeypatch.setattr(alt_top, "basket_prices", lambda: basket)
    result = alt_top.alt_top_score()
    assert result["basket_size"] == 5
    assert result["limited"] is True
    assert result["alarm"] is False
    assert result["score"] is not None and result["score"] >= alt_top._ALARM_THRESHOLD
