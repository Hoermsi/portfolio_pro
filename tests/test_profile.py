import json

import pytest

from core import db
from core.profile import max_target_return, risk_profile, save_risk_profile


def test_risk_profile_defaults(tmp_db):
    p = risk_profile()
    assert p == {"risk": 5, "target_return_pct": 6.0, "retirement_year": None,
                 "monthly_contribution": 0.0}


def test_risk_profile_roundtrip(tmp_db):
    save_risk_profile(7, 10.5, 2055, 250.0)
    p = risk_profile()
    assert p == {"risk": 7, "target_return_pct": 10.5, "retirement_year": 2055,
                 "monthly_contribution": 250.0}


def test_risk_profile_monthly_contribution_clamped(tmp_db):
    """Negative oder kaputte Sparrate fällt auf 0.0 zurück."""
    db.set_meta("risk_profile", json.dumps({"risk": 5, "monthly_contribution": -100.0}))
    assert risk_profile()["monthly_contribution"] == 0.0
    db.set_meta("risk_profile", json.dumps({"risk": 5, "monthly_contribution": "abc"}))
    assert risk_profile()["monthly_contribution"] == 0.0


def test_risk_profile_invalid_json(tmp_db):
    db.set_meta("risk_profile", "{kaputt")
    assert risk_profile() == {"risk": 5, "target_return_pct": 6.0, "retirement_year": None,
                              "monthly_contribution": 0.0}


def test_risk_profile_clamps_target_return(tmp_db):
    """Zielrendite über der Risiko-Obergrenze wird schon beim Laden geclampt."""
    db.set_meta("risk_profile", json.dumps({"risk": 2, "target_return_pct": 15.0}))
    p = risk_profile()
    assert p["target_return_pct"] == pytest.approx(max_target_return(2))  # 5.0


def test_risk_profile_clamps_risk_range(tmp_db):
    db.set_meta("risk_profile", json.dumps({"risk": 99, "target_return_pct": 5.0}))
    assert risk_profile()["risk"] == 10
    db.set_meta("risk_profile", json.dumps({"risk": -3, "target_return_pct": 5.0}))
    assert risk_profile()["risk"] == 1


def test_max_target_return_mapping():
    assert max_target_return(1) == pytest.approx(3.5)
    assert max_target_return(10) == pytest.approx(17.0)
    caps = [max_target_return(r) for r in range(1, 11)]
    assert caps == sorted(caps)  # monoton steigend


def test_target_allocation_defaults_and_stored(tmp_db):
    from core.profile import target_allocation
    assert target_allocation() == {"stock": 60.0, "crypto": 20.0, "cash": 20.0}
    tmp_db.set_meta("target_allocation", json.dumps({"stock": 50, "crypto": 30, "cash": 20}))
    assert target_allocation() == {"stock": 50.0, "crypto": 30.0, "cash": 20.0}
    # kaputtes JSON -> Defaults
    tmp_db.set_meta("target_allocation", "{kaputt")
    assert target_allocation() == {"stock": 60.0, "crypto": 20.0, "cash": 20.0}


def test_target_allocation_reexport_matches(tmp_db):
    """views.settings re-exportiert dieselbe Funktion aus core.profile."""
    from core.profile import target_allocation as core_ta
    from views.settings import target_allocation as view_ta
    assert view_ta is core_ta


def test_emergency_fund_default_and_roundtrip(tmp_db):
    from core.profile import emergency_fund_eur, save_emergency_fund_eur
    assert emergency_fund_eur() == 0.0
    save_emergency_fund_eur(5000.0)
    assert emergency_fund_eur() == 5000.0


def test_emergency_fund_clamped(tmp_db):
    from core.profile import emergency_fund_eur, save_emergency_fund_eur
    save_emergency_fund_eur(-500.0)
    assert emergency_fund_eur() == 0.0
    save_emergency_fund_eur(999_999.0)
    assert emergency_fund_eur() == 100_000.0


def test_emergency_fund_broken_json_falls_back(tmp_db):
    from core.profile import emergency_fund_eur
    tmp_db.set_meta("emergency_fund", "{kaputt")
    assert emergency_fund_eur() == 0.0


def test_emergency_fund_progress_pct(tmp_db):
    from core.profile import emergency_fund_progress_pct, save_emergency_fund_eur
    # Feature deaktiviert (0) -> immer None
    assert emergency_fund_progress_pct(3000.0) is None
    save_emergency_fund_eur(6000.0)
    assert emergency_fund_progress_pct(None) is None
    assert emergency_fund_progress_pct(3000.0) == pytest.approx(50.0)
    # Uebererfuellung bleibt sichtbar (nicht gekappt)
    assert emergency_fund_progress_pct(8000.0) == pytest.approx(133.333, rel=1e-3)


def test_ladder_config_defaults(tmp_db):
    from core.profile import ladder_config
    cfg = ladder_config("crypto")
    assert cfg == {"sell": [65.0, 75.0, 85.0], "buy": [25.0, 18.0, 12.0]}


def test_ladder_config_roundtrip(tmp_db):
    from core.profile import ladder_config, save_ladder_config
    save_ladder_config("crypto", [60, 70, 80], [30, 20, 10])
    assert ladder_config("crypto") == {"sell": [60.0, 70.0, 80.0], "buy": [30.0, 20.0, 10.0]}
    # Aktien-Markt bleibt unabhaengig auf den Defaults
    from core.profile import _DEFAULT_LADDER_SELL, _DEFAULT_LADDER_BUY
    assert ladder_config("stock") == {"sell": _DEFAULT_LADDER_SELL, "buy": _DEFAULT_LADDER_BUY}


def test_ladder_config_clamps_range(tmp_db):
    from core.profile import ladder_config, save_ladder_config
    save_ladder_config("crypto", [-10, 50, 150], [200, 20, -5])
    assert ladder_config("crypto") == {"sell": [0.0, 50.0, 100.0], "buy": [100.0, 20.0, 0.0]}


def test_ladder_config_broken_json_falls_back(tmp_db):
    from core.profile import ladder_config
    tmp_db.set_meta("cycle_ladder_crypto", "{kaputt")
    assert ladder_config("crypto") == {"sell": [65.0, 75.0, 85.0], "buy": [25.0, 18.0, 12.0]}


def test_ladder_config_wrong_length_falls_back(tmp_db):
    import json
    from core.profile import ladder_config
    tmp_db.set_meta("cycle_ladder_crypto", json.dumps({"sell": [70, 80], "buy": [25, 18, 12]}))
    cfg = ladder_config("crypto")
    assert cfg["sell"] == [65.0, 75.0, 85.0]   # ungueltige Laenge -> Default
    assert cfg["buy"] == [25.0, 18.0, 12.0]


# --- Zyklus-Fortschritt (Sperrklinke) ---

def test_cycle_progress_defaults(tmp_db):
    from core.profile import cycle_progress
    assert cycle_progress("crypto") == {"reached_tier": 0, "executed_pct": 0.0, "log": [], "basis": {}, "acked_tiers": []}


def test_advance_cycle_tier_ratchets_up(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 2, note="Score 78")
    p = cycle_progress("crypto")
    assert p["reached_tier"] == 2
    assert len(p["log"]) == 1
    assert p["log"][0]["tier"] == 2


def test_advance_cycle_tier_never_falls_back(tmp_db):
    """Kernschutz: eine niedrigere Stufe (Score wieder gefallen) darf den
    einmal erreichten Stand NICHT zuruecksetzen."""
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 3)
    advance_cycle_tier("crypto", 1)  # Score gefallen, waere jetzt nur Stufe 1
    assert cycle_progress("crypto")["reached_tier"] == 3


def test_advance_cycle_tier_no_log_entry_without_change(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 2)
    advance_cycle_tier("crypto", 2)  # gleiche Stufe erneut - kein Fortschritt
    advance_cycle_tier("crypto", 1)  # niedriger - kein Fortschritt
    assert len(cycle_progress("crypto")["log"]) == 1


def test_cycle_progress_markets_are_independent(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 3)
    assert cycle_progress("stock")["reached_tier"] == 0


def test_mark_cycle_executed_clamps_and_logs(tmp_db):
    from core.profile import cycle_progress, mark_cycle_executed
    mark_cycle_executed("crypto", 150.0, note="ueberzogen")  # ueber 100 geklammert
    p = cycle_progress("crypto")
    assert p["executed_pct"] == 100.0
    assert p["log"][0]["executed_pct"] == 100.0

    mark_cycle_executed("crypto", -10.0)  # unter 0 geklammert
    assert cycle_progress("crypto")["executed_pct"] == 0.0


def test_reset_cycle_progress_clears_ratchet_but_keeps_log(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress, mark_cycle_executed, reset_cycle_progress
    advance_cycle_tier("crypto", 3)
    mark_cycle_executed("crypto", 100.0)
    reset_cycle_progress("crypto", note="Neuer Zyklus nach Tief")

    p = cycle_progress("crypto")
    assert p["reached_tier"] == 0
    assert p["executed_pct"] == 0.0
    assert len(p["log"]) == 3  # advance + mark + reset, nichts geloescht
    assert p["log"][0].get("reset") is True


def test_cycle_progress_broken_json_falls_back(tmp_db):
    from core.profile import cycle_progress
    tmp_db.set_meta("cycle_progress_crypto", "{kaputt")
    assert cycle_progress("crypto") == {"reached_tier": 0, "executed_pct": 0.0, "log": [], "basis": {}, "acked_tiers": []}


def test_cycle_progress_log_capped(tmp_db):
    from core.profile import cycle_progress, mark_cycle_executed
    # mark_cycle_executed loggt bei JEDEM Aufruf (kein Ratchet-Gate wie bei
    # advance_cycle_tier) - einfachster Weg, viele Eintraege zu erzeugen.
    for i in range(60):
        mark_cycle_executed("crypto", float(i % 100))
    assert len(cycle_progress("crypto")["log"]) == 50


# --- Zyklus-Basis (A1) ---

def test_advance_cycle_tier_snapshots_basis_on_first_transition(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 1, basis={"btc": 0.5, "eth": 2.0})
    p = cycle_progress("crypto")
    assert p["basis"] == {"BTC": 0.5, "ETH": 2.0}


def test_advance_cycle_tier_keeps_basis_on_later_tiers(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 1, basis={"BTC": 0.5})
    # Depot hat sich seither veraendert - die Basis darf NICHT ueberschrieben werden.
    advance_cycle_tier("crypto", 2, basis={"BTC": 0.3, "SOL": 10.0})
    p = cycle_progress("crypto")
    assert p["basis"] == {"BTC": 0.5}


def test_advance_cycle_tier_without_basis_stays_empty(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 1)
    assert cycle_progress("crypto")["basis"] == {}


def test_reset_cycle_progress_clears_basis(tmp_db):
    from core.profile import advance_cycle_tier, cycle_progress, reset_cycle_progress
    advance_cycle_tier("crypto", 1, basis={"BTC": 0.5})
    reset_cycle_progress("crypto")
    assert cycle_progress("crypto")["basis"] == {}


def test_cycle_progress_basis_broken_data_falls_back_to_empty(tmp_db):
    from core.profile import cycle_progress
    tmp_db.set_meta("cycle_progress_crypto", json.dumps(
        {"reached_tier": 1, "executed_pct": 0.0, "log": [], "basis": "kaputt"}))
    assert cycle_progress("crypto")["basis"] == {}


def test_cycle_progress_basis_drops_non_positive_entries(tmp_db):
    from core.profile import cycle_progress
    tmp_db.set_meta("cycle_progress_crypto", json.dumps(
        {"reached_tier": 1, "executed_pct": 0.0, "log": [],
         "basis": {"BTC": 0.5, "ETH": 0.0, "SOL": -1.0, "AVAX": "nicht_numerisch"}}))
    assert cycle_progress("crypto")["basis"] == {"BTC": 0.5}


# --- Stufen-Validierung (A3) ---

def test_validate_ladder_stages_valid_input_returns_no_problems():
    from core.profile import validate_ladder_stages
    assert validate_ladder_stages([65, 75, 85], [25, 18, 12]) == []


def test_validate_ladder_stages_sell_not_ascending():
    from core.profile import validate_ladder_stages
    problems = validate_ladder_stages([85, 75, 65], [25, 18, 12])
    assert any("aufsteigend" in p for p in problems)


def test_validate_ladder_stages_buy_not_descending():
    from core.profile import validate_ladder_stages
    problems = validate_ladder_stages([65, 75, 85], [12, 18, 25])
    assert any("absteigend" in p for p in problems)


def test_validate_ladder_stages_overlap():
    from core.profile import validate_ladder_stages
    problems = validate_ladder_stages([50, 60, 70], [80, 40, 30])
    assert any("überschneiden" in p for p in problems)


def test_validate_ladder_stages_equal_adjacent_values_is_invalid():
    from core.profile import validate_ladder_stages
    problems = validate_ladder_stages([65, 65, 85], [25, 18, 12])
    assert any("aufsteigend" in p for p in problems)


# --- Stufen-Quittierung (ack_cycle_tier) ---

def test_ack_cycle_tier_sets_when_reached(tmp_db):
    from core.profile import advance_cycle_tier, ack_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 2)
    ack_cycle_tier("crypto", 1)
    assert cycle_progress("crypto")["acked_tiers"] == [1]


def test_ack_cycle_tier_noop_when_not_reached(tmp_db):
    from core.profile import advance_cycle_tier, ack_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 1)
    ack_cycle_tier("crypto", 2)  # Stufe 2 noch nicht erreicht
    assert cycle_progress("crypto")["acked_tiers"] == []


def test_ack_cycle_tier_dedupes(tmp_db):
    from core.profile import advance_cycle_tier, ack_cycle_tier, cycle_progress
    advance_cycle_tier("crypto", 2)
    ack_cycle_tier("crypto", 1)
    ack_cycle_tier("crypto", 1)
    ack_cycle_tier("crypto", 2)
    assert cycle_progress("crypto")["acked_tiers"] == [1, 2]


def test_reset_cycle_progress_clears_acked_tiers(tmp_db):
    from core.profile import advance_cycle_tier, ack_cycle_tier, cycle_progress, reset_cycle_progress
    advance_cycle_tier("crypto", 2)
    ack_cycle_tier("crypto", 1)
    reset_cycle_progress("crypto")
    assert cycle_progress("crypto")["acked_tiers"] == []


def test_cycle_progress_acked_tiers_broken_data_falls_back_to_empty(tmp_db):
    import json
    from core.profile import cycle_progress
    tmp_db.set_meta("cycle_progress_crypto", json.dumps(
        {"reached_tier": 2, "executed_pct": 0.0, "log": [], "basis": {},
         "acked_tiers": "kaputt"}))
    assert cycle_progress("crypto")["acked_tiers"] == []


def test_cycle_progress_acked_tiers_drops_out_of_range_entries(tmp_db):
    import json
    from core.profile import cycle_progress
    tmp_db.set_meta("cycle_progress_crypto", json.dumps(
        {"reached_tier": 3, "executed_pct": 0.0, "log": [], "basis": {},
         "acked_tiers": [1, 4, 0, "2", 2]}))
    assert cycle_progress("crypto")["acked_tiers"] == [1, 2]
