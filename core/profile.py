"""Risikoprofil des Nutzers: Risikobereitschaft, Zielrendite, Pensionsjahr.

Eigenes core-Modul (statt views/settings), damit agents/* und analysis/* das
Profil ohne Import aus der View-Schicht laden können.
"""
import json

from core import db

_DEFAULT_RISK_PROFILE = {"risk": 5, "target_return_pct": 6.0, "retirement_year": None,
                         "monthly_contribution": 0.0}

_DEFAULT_TARGETS = {"stock": 60, "crypto": 20, "cash": 20}


def target_allocation() -> dict[str, float]:
    """Gespeicherte Zielallokation (%) mit sicheren Standardwerten laden.

    Liegt in core (statt in der View-Schicht), damit auch agents/* und analysis/*
    sie ohne Streamlit-Import nutzen können.
    """
    raw = db.get_meta("target_allocation")
    try:
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            values = {}
    except (json.JSONDecodeError, TypeError):
        values = {}
    return {key: float(values.get(key, default)) for key, default in _DEFAULT_TARGETS.items()}


def max_target_return(risk: int) -> float:
    """Obergrenze der Zielrendite p.a. in % je Risikostufe: 2% + 1,5% pro Stufe.

    Stufe 1 ≈ Tagesgeld-/Anleihen-Niveau (3,5%), Stufe 6-7 ≈ historische
    Aktienmarktrendite (~11-12,5%), Stufe 10 = 17% (aggressiv/Krypto-lastig).
    """
    return 2.0 + risk * 1.5


def risk_profile() -> dict:
    """Gespeichertes Risikoprofil mit sicheren Standardwerten laden.

    Clampt Risiko (1-10) und Zielrendite (0 bis max_target_return) bereits hier,
    damit jeder Konsument (Dashboard, Agents) gegen inkonsistent gespeicherte
    Werte abgesichert ist - nicht nur die Settings-UI.
    """
    raw = db.get_meta("risk_profile")
    try:
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            values = {}
    except (json.JSONDecodeError, TypeError):
        values = {}
    out = dict(_DEFAULT_RISK_PROFILE)
    out.update({k: values[k] for k in _DEFAULT_RISK_PROFILE if k in values})
    try:
        out["risk"] = min(10, max(1, int(out["risk"])))
    except (TypeError, ValueError):
        out["risk"] = _DEFAULT_RISK_PROFILE["risk"]
    try:
        out["target_return_pct"] = min(max_target_return(out["risk"]),
                                       max(0.0, float(out["target_return_pct"])))
    except (TypeError, ValueError):
        out["target_return_pct"] = _DEFAULT_RISK_PROFILE["target_return_pct"]
    if out["retirement_year"] is not None:
        try:
            out["retirement_year"] = int(out["retirement_year"])
        except (TypeError, ValueError):
            out["retirement_year"] = None
    try:
        out["monthly_contribution"] = max(0.0, float(out["monthly_contribution"]))
    except (TypeError, ValueError):
        out["monthly_contribution"] = 0.0
    return out


def save_risk_profile(risk: int, target_return_pct: float, retirement_year: int | None,
                      monthly_contribution: float = 0.0):
    """Risikoprofil als einzelnen Meta-Key persistieren."""
    db.set_meta("risk_profile", json.dumps({
        "risk": int(risk),
        "target_return_pct": float(target_return_pct),
        "retirement_year": int(retirement_year) if retirement_year else None,
        "monthly_contribution": max(0.0, float(monthly_contribution)),
    }))
