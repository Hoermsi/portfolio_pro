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
