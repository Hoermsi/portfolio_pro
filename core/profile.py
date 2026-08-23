"""Risikoprofil des Nutzers: Risikobereitschaft, Zielrendite, Pensionsjahr.

Eigenes core-Modul (statt views/settings), damit agents/* und analysis/* das
Profil ohne Import aus der View-Schicht laden können.
"""
import json
from datetime import datetime

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


_DEFAULT_EMERGENCY_FUND = 0.0
_EMERGENCY_FUND_MAX = 100_000.0


def emergency_fund_eur() -> float:
    """Gespeicherter Notgroschen (Mindest-Cash-Reserve) in EUR, 0 = deaktiviert."""
    raw = db.get_meta("emergency_fund")
    try:
        value = float(json.loads(raw)) if raw else _DEFAULT_EMERGENCY_FUND
    except (json.JSONDecodeError, TypeError, ValueError):
        value = _DEFAULT_EMERGENCY_FUND
    return min(_EMERGENCY_FUND_MAX, max(0.0, value))


def save_emergency_fund_eur(value: float):
    """Notgroschen persistieren, geklammert auf [0, 100000]."""
    clamped = min(_EMERGENCY_FUND_MAX, max(0.0, float(value)))
    db.set_meta("emergency_fund", json.dumps(clamped))


def emergency_fund_progress_pct(cash_balance: float | None) -> float | None:
    """Füllstand des Notgroschens in % - None, wenn kein Notgroschen aktiv ist
    oder kein Kontostand vorliegt. Nicht auf 100 gekappt (Übererfüllung sichtbar)."""
    fund = emergency_fund_eur()
    if fund <= 0 or cash_balance is None:
        return None
    return max(0.0, float(cash_balance)) / fund * 100.0


# --- ZYKLUS-POSITION: eigene Verkaufs-/Kauf-Stufen ("Deine Regel", keine Anlageberatung) ---

_DEFAULT_LADDER_SELL = [65.0, 75.0, 85.0]   # Score-Schwellen, aufsteigend (Gier -> Verkauf)
_DEFAULT_LADDER_BUY = [25.0, 18.0, 12.0]    # Score-Schwellen, absteigend (Angst -> Kauf)
LADDER_FRACTIONS_PCT = (33, 66, 100)        # kumulierter Anteil je Stufe (1/3, 2/3, alles)


def ladder_config(market: str) -> dict:
    """Eigene Score-Schwellen für die Zyklus-Position-Stufen, je Markt getrennt
    gespeichert. {"sell": [3 Werte], "buy": [3 Werte]} - fällt bei fehlendem/
    kaputtem Meta-Eintrag oder falscher Länge auf die Defaults zurück."""
    raw = db.get_meta(f"cycle_ladder_{market}")
    try:
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            values = {}
    except (json.JSONDecodeError, TypeError):
        values = {}

    def _levels(key: str, default: list[float]) -> list[float]:
        levels = values.get(key)
        if not isinstance(levels, list) or len(levels) != 3:
            return list(default)
        try:
            return [min(100.0, max(0.0, float(v))) for v in levels]
        except (TypeError, ValueError):
            return list(default)

    return {"sell": _levels("sell", _DEFAULT_LADDER_SELL),
            "buy": _levels("buy", _DEFAULT_LADDER_BUY)}


def save_ladder_config(market: str, sell: list[float], buy: list[float]):
    """Eigene Stufen-Schwellen persistieren, je Wert auf [0, 100] geklammert."""
    clamp = lambda vals: [min(100.0, max(0.0, float(v))) for v in vals]
    db.set_meta(f"cycle_ladder_{market}", json.dumps({
        "sell": clamp(sell), "buy": clamp(buy),
    }))


def validate_ladder_stages(sell: list[float], buy: list[float]) -> list[str]:
    """Prüft die Stufen-Reihenfolge, BEVOR gespeichert wird - leer = gültig.
    Streng aufsteigend/absteigend (gleiche Nachbarwerte machen eine Stufe
    unerreichbar) und keine Überlappung von Kauf-/Verkaufszone: sonst baut
    render_ladder_gauge() nicht-monotone Farbbänder (edges müssen aufsteigend
    sein) und die "Stufe 1/2/3"-Beschriftung neben dem Barometer entspricht
    nicht mehr dem, was der sortierte Gauge tatsächlich zeigt."""
    problems = []
    if len(sell) != 3 or len(buy) != 3:
        return ["Es werden genau 3 Verkaufs- und 3 Kauf-Stufen erwartet."]
    if not (sell[0] < sell[1] < sell[2]):
        problems.append("Verkaufs-Stufen müssen streng aufsteigend sein (Stufe 1 < 2 < 3).")
    if not (buy[0] > buy[1] > buy[2]):
        problems.append("Kauf-Stufen müssen streng absteigend sein (Stufe 1 > 2 > 3).")
    if max(buy) >= min(sell):
        problems.append(f"Kauf- und Verkaufs-Zone überschneiden sich (höchste Kauf-Schwelle "
                        f"{max(buy):.0f} muss unter der niedrigsten Verkaufs-Schwelle "
                        f"{min(sell):.0f} liegen).")
    return problems


# --- ZYKLUS-FORTSCHRITT: Sperrklinke gegen die zustandslose Leiter ---
#
# market_timing.active_ladder_tier() ist bewusst rein (kein Gedächtnis): fällt
# der Score von 85 zurück auf 60, sackt die "aktive Stufe" von 3 auf 0 - ohne
# zu wissen, ob der Nutzer inzwischen verkauft hat. Diese Sperrklinke hält den
# höchsten je erreichten Stand fest, bis der Nutzer selbst zurücksetzt (z.B.
# nach einem neuen Zyklus-Tief, wenn wieder aufgebaut wird) - "reached_tier"
# fällt NIE automatisch zurück.

_DEFAULT_CYCLE_PROGRESS = {"reached_tier": 0, "executed_pct": 0.0, "log": [], "basis": {},
                           "acked_tiers": []}
_MAX_LOG_ENTRIES = 50


def cycle_progress(market: str) -> dict:
    """{"reached_tier": 0-3, "executed_pct": 0-100, "log": [{"at","tier",
    "executed_pct","note"}, ...], "basis": {"SYMBOL": menge, ...},
    "acked_tiers": [1,2,...]} - sichere Standardwerte bei fehlendem/kaputtem
    Meta-Eintrag. `basis` ist der beim ersten Erreichen von Stufe 1
    eingefrorene Ausgangsbestand des aktuellen Zyklus (siehe advance_cycle_tier()) -
    Grundlage für exit_ranking.sell_list()'s Restmengen-Berechnung in
    späteren Stufen. `acked_tiers`: welche bestätigten Stufen der Nutzer als
    "gelesen und durchgeführt" abgehakt hat - getrennt von reached_tier, das
    nur aussagt, DASS die Stufe erreicht wurde (siehe ack_cycle_tier())."""
    raw = db.get_meta(f"cycle_progress_{market}")
    try:
        values = json.loads(raw) if raw else {}
        if not isinstance(values, dict):
            values = {}
    except (json.JSONDecodeError, TypeError):
        values = {}
    out = dict(_DEFAULT_CYCLE_PROGRESS)
    out.update({k: values[k] for k in _DEFAULT_CYCLE_PROGRESS if k in values})
    try:
        out["reached_tier"] = min(3, max(0, int(out["reached_tier"])))
    except (TypeError, ValueError):
        out["reached_tier"] = 0
    try:
        out["executed_pct"] = min(100.0, max(0.0, float(out["executed_pct"])))
    except (TypeError, ValueError):
        out["executed_pct"] = 0.0
    if not isinstance(out["log"], list):
        out["log"] = []
    if isinstance(out["basis"], dict):
        cleaned = {}
        for sym, qty in out["basis"].items():
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                cleaned[str(sym).upper()] = qty
        out["basis"] = cleaned
    else:
        out["basis"] = {}
    if isinstance(out["acked_tiers"], list):
        acked = set()
        for t in out["acked_tiers"]:
            try:
                t = int(t)
            except (TypeError, ValueError):
                continue
            if 1 <= t <= 3:
                acked.add(t)
        out["acked_tiers"] = sorted(acked)
    else:
        out["acked_tiers"] = []
    return out


def _save_cycle_progress(market: str, progress: dict):
    db.set_meta(f"cycle_progress_{market}", json.dumps(progress))


def advance_cycle_tier(market: str, tier: int, note: str = "",
                       basis: dict[str, float] | None = None) -> dict:
    """Hebt reached_tier auf max(bisheriger Stand, tier) an - fällt nie von
    selbst zurück, auch wenn der Score später wieder sinkt. Kein Effekt (aber
    kein Fehler), wenn `tier` den bisherigen Stand nicht überschreitet.

    Beim ERSTEN Übergang von Stufe 0 auf >=1 wird `basis` (Symbol->Menge,
    typischerweise exit_ranking.held_symbols()) als Ausgangsbestand des
    aktuellen Zyklus eingefroren - Grundlage für exit_ranking.sell_list()'s
    Restmengen-Berechnung in Folgestufen. Spätere Übergänge (1->2, 2->3)
    überschreiben eine einmal gesetzte Basis NICHT."""
    progress = cycle_progress(market)
    tier = min(3, max(0, int(tier)))
    if tier > progress["reached_tier"]:
        if progress["reached_tier"] == 0 and not progress["basis"] and basis:
            progress["basis"] = {str(k).upper(): float(v) for k, v in basis.items() if v}
        progress["reached_tier"] = tier
        progress["log"] = ([{"at": _now(), "tier": tier, "note": note}] + progress["log"])[:_MAX_LOG_ENTRIES]
        _save_cycle_progress(market, progress)
    return progress


def ack_cycle_tier(market: str, tier: int, note: str = "") -> dict:
    """Markiert eine bereits bestätigte Stufe als abgehakt (gelesen UND
    durchgeführt). Getrennt von advance_cycle_tier(): dort geht es darum, DASS
    die Stufe erreicht ist (Sperrklinke, fällt nie zurück), hier darum, dass
    der Nutzer tatsächlich gehandelt hat - beide Aussagen können auseinander-
    fallen (bestätigt, aber noch nicht verkauft). Wirkungslos (kein Fehler),
    wenn `tier` die Sperrklinke noch nicht erreicht hat."""
    progress = cycle_progress(market)
    tier = int(tier)
    if 1 <= tier <= progress["reached_tier"] and tier not in progress["acked_tiers"]:
        progress["acked_tiers"] = sorted(set(progress["acked_tiers"]) | {tier})
        progress["log"] = ([{"at": _now(), "acked_tier": tier, "note": note}]
                           + progress["log"])[:_MAX_LOG_ENTRIES]
        _save_cycle_progress(market, progress)
    return progress


def mark_cycle_executed(market: str, executed_pct: float, note: str = "") -> dict:
    """Trägt ein, wie viel Prozent der aktuellen Verkaufsstufe der Nutzer
    tatsächlich ausgeführt hat - rein informativ (wie das ganze Zyklus-
    Feature), die App verkauft nie selbst."""
    progress = cycle_progress(market)
    progress["executed_pct"] = min(100.0, max(0.0, float(executed_pct)))
    progress["log"] = ([{"at": _now(), "executed_pct": progress["executed_pct"], "note": note}]
                       + progress["log"])[:_MAX_LOG_ENTRIES]
    _save_cycle_progress(market, progress)
    return progress


def reset_cycle_progress(market: str, note: str = "Neuer Zyklus") -> dict:
    """Setzt reached_tier/executed_pct explizit zurück - die einzige Möglich-
    keit, die Sperrklinke zu lösen (z.B. nach einem Zyklus-Tief, wenn wieder
    aufgebaut wird). Muss der Nutzer bewusst auslösen, passiert nie automatisch.
    Das Log bleibt erhalten (Reset wird selbst als Eintrag angehängt), damit
    die Historie über mehrere Zyklen hinweg nachvollziehbar bleibt."""
    progress = cycle_progress(market)
    progress["reached_tier"] = 0
    progress["executed_pct"] = 0.0
    progress["basis"] = {}
    progress["acked_tiers"] = []
    progress["log"] = ([{"at": _now(), "reset": True, "note": note}] + progress["log"])[:_MAX_LOG_ENTRIES]
    _save_cycle_progress(market, progress)
    return progress


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
