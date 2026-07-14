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
