"""Smoke-Test für views/asset_detail._render_alt_top() mit dem neuen
Verlaufs-Chart (alt_top.score_history()) - stellt sicher, dass der neue
Aufruf weder bei leerer noch bei gefüllter Historie crasht (Muster:
tests/test_asset_detail_cycle_ladder.py)."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from analysis import alt_top


def _alt_top_script():
    from views import asset_detail
    asset_detail._render_alt_top("crypto")


_FAKE_ALT = {
    "score": 55.0, "regime": "Erhöht", "regime_reason": "Testregime",
    "breakdown": [{"key": "alt_extension", "label": "Alt-Ausdehnung", "score": 55.0,
                  "value": 1.1, "text": "1.10", "weight_pct": 35.0}],
    "unavailable": [], "coverage_pct": 100.0, "basket_size": 10, "limited": False,
    "alarm": False,
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(alt_top, "alt_top_score", lambda: _FAKE_ALT)


def test_render_alt_top_with_history_data(monkeypatch):
    idx = pd.date_range("2018-01-01", periods=1200, freq="D")
    monkeypatch.setattr(alt_top, "score_history",
                        lambda: pd.Series(range(1200), index=idx, dtype=float) % 100.0)

    at = AppTest.from_function(_alt_top_script).run(timeout=30)
    assert not at.exception


def test_render_alt_top_with_empty_history(monkeypatch):
    monkeypatch.setattr(alt_top, "score_history", lambda: pd.Series(dtype=float))

    at = AppTest.from_function(_alt_top_script).run(timeout=30)
    assert not at.exception
