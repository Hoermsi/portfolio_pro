"""Tests für die Sperrklinke/Bestätigung/Quittierung in
views/asset_detail._render_cycle_ladder(): Fortschreiben passiert NIE
automatisch beim Rendern, nur über den expliziten "Stufe bestätigen"-Button;
Bestätigung ist unter 60% Abdeckung gesperrt und verlangt bei Mehrfachsprüngen
eine Zusatz-Bestätigung; abgehakte Stufen bleiben bis zum Reset sichtbar
(Muster: tests/test_onboarding.py)."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def _no_cycle_history_network(monkeypatch):
    """_render_cycle_ladder ruft bei Krypto jetzt auch cycle_backtest.score_history()
    für den neuen Verlaufs-Chart auf - ohne Mock würde jeder Test hier einen
    echten Netzwerk-Call auslösen (score_history -> cycle.btc_price_series())."""
    from analysis import cycle_backtest
    monkeypatch.setattr(cycle_backtest, "score_history", lambda: pd.Series(dtype=float))


def _cycle_ladder_script():
    from views import asset_detail
    asset_detail._render_cycle_ladder("crypto", {}, {})


_EMPTY_SELL_LIST = {
    "target_pct": 0.0, "total_value_eur": 0.0, "sell_value_eur": 0.0,
    "sell_pct_actual": 0.0, "basis_value_eur": None, "already_sold_value_eur": None,
    "basis_price_unavailable": [], "to_sell": [], "keep": [],
}


def _fake_cyc(score: float, coverage_pct: float = 100.0) -> dict:
    return {
        "score": score, "regime": "Testregime", "regime_reason": "Testregime",
        "coverage_pct": coverage_pct, "unavailable": [],
        "breakdown": [{"key": "mvrv_z", "label": "MVRV Z-Score", "score": score,
                      "value": 1.0, "text": "1.00", "weight_pct": 100.0}],
        "price_now": 60000.0, "drawdown_pct": -5.0,
    }


def test_cycle_ladder_never_auto_advances_only_confirm_button_does(tmp_db, monkeypatch):
    from analysis import cycle as cycle_mod, exit_ranking
    from core import profile

    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(90.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {"WEAK": 10.0})
    monkeypatch.setattr(exit_ranking, "sell_list", lambda target_pct, **k: _EMPTY_SELL_LIST)

    # Default-Verkaufsschwellen [65, 75, 85] - Score 90 erreicht live Stufe 3,
    # aber ohne Klick darf NICHTS persistiert werden.
    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception
    assert profile.cycle_progress("crypto")["reached_tier"] == 0

    at.run(timeout=30)  # zweiter Render-Durchlauf (simuliert erneutes Öffnen) - immer noch nichts
    assert profile.cycle_progress("crypto")["reached_tier"] == 0

    # Mehrfachsprung (Stufe 0 -> 3): Bestaetigen-Button ist erst nach der
    # Zusatz-Checkbox aktiv.
    confirm_btn = next(b for b in at.button if b.key == "cycle_confirm_crypto")
    assert confirm_btn.disabled

    jump_ack = next(c for c in at.checkbox if c.key == "cycle_confirm_jump_ack_crypto")
    jump_ack.set_value(True).run(timeout=30)
    confirm_btn = next(b for b in at.button if b.key == "cycle_confirm_crypto")
    assert not confirm_btn.disabled

    confirm_btn.click().run(timeout=30)
    assert not at.exception
    progress = profile.cycle_progress("crypto")
    assert progress["reached_tier"] == 3
    assert progress["basis"] == {"WEAK": 10.0}


def test_cycle_ladder_confirm_locked_below_coverage_threshold(tmp_db, monkeypatch):
    """Bei Datenabdeckung unter 60% gibt es GAR KEINEN Bestätigen-Button -
    ein dünn abgedeckter Score darf keine Stufe festschreiben können."""
    from analysis import cycle as cycle_mod, exit_ranking
    from core import profile

    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(90.0, coverage_pct=45.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})

    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception
    assert not any(b.key == "cycle_confirm_crypto" for b in at.button)
    assert profile.cycle_progress("crypto")["reached_tier"] == 0


def test_cycle_ladder_single_tier_jump_needs_no_extra_checkbox(tmp_db, monkeypatch):
    """Ein regulärer Ein-Stufen-Schritt (0 -> 1) braucht KEINE Zusatz-Checkbox -
    die gilt nur für übersprungene Zwischenstufen."""
    from analysis import cycle as cycle_mod, exit_ranking
    from core import profile

    # Default-Verkaufsschwellen [65, 75, 85] - Score 70 erreicht nur Stufe 1.
    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(70.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})

    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not any(c.key == "cycle_confirm_jump_ack_crypto" for c in at.checkbox)
    confirm_btn = next(b for b in at.button if b.key == "cycle_confirm_crypto")
    assert not confirm_btn.disabled

    confirm_btn.click().run(timeout=30)
    assert profile.cycle_progress("crypto")["reached_tier"] == 1


def test_cycle_ladder_ack_button_marks_tier_acked(tmp_db, monkeypatch):
    from analysis import cycle as cycle_mod, exit_ranking
    from core import profile

    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(50.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})
    monkeypatch.setattr(exit_ranking, "sell_list", lambda target_pct, **k: _EMPTY_SELL_LIST)
    profile.advance_cycle_tier("crypto", 1, note="Test")

    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception

    ack_btn = next(b for b in at.button if b.key == "cycle_ack_crypto_1")
    ack_btn.click().run(timeout=30)
    assert not at.exception
    assert profile.cycle_progress("crypto")["acked_tiers"] == [1]


def test_cycle_ladder_renders_history_chart_without_crashing(tmp_db, monkeypatch):
    """Smoke-Test für den neuen Verlaufs-Chart (analysis.cycle_backtest.
    score_history() -> ui.components.render_score_history_chart()) - mit
    echten (nicht-leeren) Score-Daten statt der leeren Autouse-Mock-Serie."""
    from analysis import cycle as cycle_mod, cycle_backtest, exit_ranking

    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(50.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})
    idx = pd.date_range("2018-01-01", periods=800, freq="D")
    monkeypatch.setattr(cycle_backtest, "score_history",
                        lambda: pd.Series(range(800), index=idx, dtype=float) % 100.0)

    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception


def test_cycle_ladder_rejects_invalid_stage_order_and_keeps_old_config(tmp_db, monkeypatch):
    """A3: nicht-aufsteigende Verkaufsstufen werden beim Speichern abgelehnt,
    die zuvor gespeicherte Konfiguration bleibt unverändert."""
    from analysis import cycle as cycle_mod, exit_ranking
    from core import profile

    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: _fake_cyc(20.0))
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})

    profile.save_ladder_config("crypto", [65.0, 75.0, 85.0], [25.0, 18.0, 12.0])

    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception

    sell1 = next(w for w in at.number_input if w.key == "ladder_sell1_crypto")
    sell1.set_value(95.0).run(timeout=30)  # macht Verkauf-Reihenfolge ungueltig (95 > 75)

    save_btn = next(b for b in at.button if b.key == "ladder_save_crypto")
    save_btn.click().run(timeout=30)

    assert not at.exception
    assert any("aufsteigend" in e.value for e in at.error)
    # Alte, gueltige Konfiguration bleibt unveraendert gespeichert.
    assert profile.ladder_config("crypto")["sell"] == [65.0, 75.0, 85.0]
