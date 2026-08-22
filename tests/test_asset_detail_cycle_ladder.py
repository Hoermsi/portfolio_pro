"""Test für die Sperrklinke in views/asset_detail._render_cycle_ladder():
Fortschreiben passiert NIE mehr automatisch beim Rendern, nur noch über den
expliziten "Stufe bestätigen"-Button (Muster: tests/test_onboarding.py)."""
from streamlit.testing.v1 import AppTest


def _cycle_ladder_script():
    from views import asset_detail
    asset_detail._render_cycle_ladder("crypto", {}, {})


def test_cycle_ladder_never_auto_advances_only_confirm_button_does(tmp_db, monkeypatch):
    from analysis import cycle as cycle_mod, cycle_backtest, exit_ranking
    from core import profile

    fake_cyc = {
        "score": 90.0, "regime": "Euphorie", "regime_reason": "Testregime",
        "coverage_pct": 100.0, "unavailable": [],
        "breakdown": [{"key": "mvrv_z", "label": "MVRV Z-Score", "score": 90.0,
                      "value": 5.0, "text": "5.00", "weight_pct": 100.0}],
        "price_now": 60000.0, "drawdown_pct": -5.0,
    }
    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: fake_cyc)
    monkeypatch.setattr(cycle_mod, "trigger_price", lambda th, cycle=None: None)
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {"WEAK": 10.0})
    monkeypatch.setattr(exit_ranking, "sell_list", lambda target_pct, **k: {
        "target_pct": target_pct, "total_value_eur": 0.0, "sell_value_eur": 0.0,
        "sell_pct_actual": 0.0, "basis_value_eur": None, "already_sold_value_eur": None,
        "basis_price_unavailable": [], "to_sell": [], "keep": [],
    })
    monkeypatch.setattr(cycle_backtest, "run_backtest", lambda **k: {"available": False, "reason": "test"})

    # Default-Verkaufsschwellen [65, 75, 85] - Score 90 erreicht live Stufe 3,
    # aber ohne Klick darf NICHTS persistiert werden.
    at = AppTest.from_function(_cycle_ladder_script).run(timeout=30)
    assert not at.exception
    assert profile.cycle_progress("crypto")["reached_tier"] == 0

    at.run(timeout=30)  # zweiter Render-Durchlauf (simuliert erneutes Öffnen) - immer noch nichts
    assert profile.cycle_progress("crypto")["reached_tier"] == 0

    confirm_btn = next(b for b in at.button if b.key == "cycle_confirm_crypto")
    confirm_btn.click().run(timeout=30)
    assert not at.exception
    progress = profile.cycle_progress("crypto")
    assert progress["reached_tier"] == 3
    assert progress["basis"] == {"WEAK": 10.0}


def test_cycle_ladder_rejects_invalid_stage_order_and_keeps_old_config(tmp_db, monkeypatch):
    """A3: nicht-aufsteigende Verkaufsstufen werden beim Speichern abgelehnt,
    die zuvor gespeicherte Konfiguration bleibt unverändert."""
    from analysis import cycle as cycle_mod, cycle_backtest, exit_ranking
    from core import profile

    fake_cyc = {
        "score": 20.0, "regime": "Akkumulation", "regime_reason": "Testregime",
        "coverage_pct": 100.0, "unavailable": [],
        "breakdown": [{"key": "mvrv_z", "label": "MVRV Z-Score", "score": 20.0,
                      "value": 0.5, "text": "0.50", "weight_pct": 100.0}],
        "price_now": 40000.0, "drawdown_pct": -50.0,
    }
    monkeypatch.setattr(cycle_mod, "cycle_score", lambda: fake_cyc)
    monkeypatch.setattr(cycle_mod, "trigger_price", lambda th, cycle=None: None)
    monkeypatch.setattr(exit_ranking, "held_symbols", lambda: {})
    monkeypatch.setattr(cycle_backtest, "run_backtest", lambda **k: {"available": False, "reason": "test"})

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
