"""Portfolio-Stratege: gibt konkrete, ausführbare Handlungsanweisungen und
managt damit die Schatten-Depots - getrennt je Mandat (Krypto-KI / Aktien-KI).
Erinnert sich an alle eigenen früheren Empfehlungen des jeweiligen Depots und
deren Entwicklung seit dem Aufruf (Erfolgskontrolle).
"""
import json

from agents.base import run_json_agent
from agents.dossier import risk_profile_prompt
from core import db, shadow
from core.config import CLAUDE_PRICING
from core.portfolio import portfolio_summary
from data import crypto as crypto_data
from data import stocks as stock_data

_TARGETS = {"crypto": "SHADOW-KRYPTO", "stock": "SHADOW-AKTIEN"}

_MANDATES = {
    "crypto": ("Dein Mandat: ein REINES KRYPTO-Depot. Du darfst ausschließlich "
               "Kryptowährungen und Cash (EUR) handeln - keine Aktien."),
    "stock": ("Dein Mandat: ein REINES AKTIEN-Depot. Du darfst ausschließlich "
              "Aktien und Cash (EUR) handeln - keine Kryptowährungen."),
}


def _schema_for(scope: str) -> dict:
    """Structured-Output-Schema mit auf das Mandat eingeschränkten Asset-Typen."""
    types = [scope, "cash"]
    return {
        "type": "object",
        "properties": {
            "marktausblick": {"type": "string",
                              "description": "2-4 Sätze zur aktuellen Marktlage im Mandatsbereich"},
            "gesamtkommentar": {"type": "string",
                                "description": "Einschätzung zum Zustand des KI-Depots"},
            "empfehlungen": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "aktion": {"type": "string",
                                   "enum": ["kaufen", "verkaufen", "umschichten", "halten"]},
                        "symbol": {"type": "string", "description": "betroffenes Symbol, z.B. ENJ"},
                        "asset_type": {"type": "string", "enum": types},
                        "ziel_symbol": {"type": "string",
                                        "description": "nur bei umschichten: Zielsymbol, sonst leer"},
                        "ziel_asset_type": {"type": "string", "enum": types + [""]},
                        "anteil_pct": {"type": "number",
                                       "description": "Anteil der Position (verkaufen/umschichten) "
                                                      "bzw. des Cash (kaufen), 0-100"},
                        "begruendung": {"type": "string",
                                        "description": "konkrete Begründung auf Deutsch"},
                    },
                    "required": ["aktion", "symbol", "asset_type", "anteil_pct", "begruendung"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["marktausblick", "gesamtkommentar", "empfehlungen"],
        "additionalProperties": False,
    }


def _system_for(scope: str) -> str:
    return (
        "Du bist ein erfahrener, nüchterner Portfolio-Manager, der ein virtuelles "
        "Experiment-Depot aktiv steuert. " + _MANDATES[scope] + " Dein Ziel: das "
        "KI-Depot soll den entsprechenden Teil des echten Portfolios des Nutzers "
        "über die Zeit schlagen. Du gibst KONKRETE, sofort ausführbare Anweisungen "
        "- immer mit Symbol, Aktion und Anteil (in %). Beispiel: 'ENJ ist im "
        "aktuellen Zyklus abgehängt; umschichten des gesamten ENJ-Bestands (100%) "
        "in SOL'.\n"
        "Regeln:\n"
        "- 'umschichten' braucht ziel_symbol + ziel_asset_type. 'verkaufen' erzeugt "
        "Cash, 'kaufen' nutzt vorhandenes Cash (anteil_pct = Anteil des Cash).\n"
        "- Empfiehl nur Käufe, wenn Cash vorhanden ist. Prüfe die Bestände.\n"
        "- Jeder virtuelle Trade kostet 0,25% (Gebühr/Slippage) - handle also nur bei "
        "echter Überzeugung, nicht übermäßig.\n"
        "- Lerne aus deiner Empfehlungs-Historie: Wenn frühere Calls schlecht liefen, "
        "korrigiere. Wiederhole keine bereits umgesetzten Umschichtungen.\n"
        "- Wenn nichts zu tun ist, gib für Kernpositionen 'halten' aus. Es ist völlig "
        "in Ordnung, wenige oder keine Trades zu empfehlen.\n"
        "- Berücksichtige das im Prompt genannte Risikoprofil des Nutzers: bei niedriger "
        "Risikobereitschaft konservativer agieren (größere Cash-Quote, etablierte Werte), "
        "bei hoher darfst du offensiver umschichten.\n"
        "- Dies ist keine Anlageberatung, sondern das Management eines Testdepots."
    )


def _position_metrics(symbol: str, asset_type: str) -> dict:
    """Kompakte Kennzahlen je Position (best effort, gecacht)."""
    if asset_type == "crypto":
        md = crypto_data.get_market_data(symbol)
        return {k: md.get(k) for k in
                ("rang", "aenderung_7d_pct", "aenderung_30d_pct", "ath_abstand_pct")
                if md.get(k) is not None}
    if asset_type == "stock":
        f = stock_data.get_fundamentals(symbol)
        return {k: f.get(k) for k in ("kgv", "sektor", "analysten_empfehlung")
                if f.get(k) is not None}
    return {}


def _shadow_block(scope: str, max_detail: int = 8) -> str:
    vals = shadow.valued_shadow(scope)
    total = shadow.total_value(scope, vals)
    vals_sorted = sorted(vals, key=lambda v: -(v["value_eur"] or 0))
    lines = [f"DEIN KI-DEPOT ({shadow.SCOPES[scope]}): {total:,.2f} EUR gesamt"]
    for i, v in enumerate(vals_sorted):
        weight = (v["value_eur"] or 0) / total * 100 if total else 0
        row = (f"- {v['symbol']} ({v['asset_type']}): {v['quantity']:.6g} Stück, "
               f"{v['value_eur'] or 0:,.2f} EUR ({weight:.1f}%)")
        if v["asset_type"] != "cash" and i < max_detail:
            metrics = _position_metrics(v["symbol"], v["asset_type"])
            if metrics:
                row += " | " + ", ".join(f"{k}={val}" for k, val in metrics.items())
        lines.append(row)
    return "\n".join(lines)


def _history_block(scope: str, limit: int = 15) -> str:
    """Frühere Empfehlungen des Depots mit Entwicklung seit dem Call."""
    recs = [r for r in db.list_recommendations(scope, limit) if r["angewendet"]]
    if not recs:
        return "Noch keine früheren Empfehlungen."
    lines = []
    for r in recs:
        sym, ziel = r["symbol"], r["ziel_symbol"]
        parts = [f"{r['created_at'][:10]}: {r['aktion']} {sym or ''}"]
        if ziel:
            parts.append(f"-> {ziel} ({r['anteil_pct']:.0f}%)")

        def _perf(s, at, ref):
            if not s or not ref:
                return None
            now = shadow.price_eur(s, at or scope)
            return (now / ref - 1) * 100 if now and ref else None

        p_from = _perf(sym, r["asset_type"], r["kurs_symbol_eur"])
        p_to = _perf(ziel, r["ziel_asset_type"], r["kurs_ziel_eur"])
        seit = []
        if p_from is not None:
            seit.append(f"{sym} seither {p_from:+.1f}%")
        if p_to is not None:
            seit.append(f"{ziel} seither {p_to:+.1f}%")
        if seit:
            parts.append("[" + ", ".join(seit) + "]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _performance_block(scope: str) -> str:
    info = shadow.start_info(scope)
    comp = shadow.comparison_df(scope)
    lines = []
    if info:
        lines.append(f"Experiment-Start: {info['date']} bei {info['real_start']:,.2f} EUR "
                     "(beide Depots gleich).")
    if comp is not None and len(comp) >= 1:
        last = comp.iloc[-1]
        real_idx = last.get("Echt")
        ki_idx = last.get("KI")
        if real_idx is not None and ki_idx is not None:
            lines.append(f"Aktueller Indexstand (Start=100): Echt {real_idx:.1f} | "
                         f"KI {ki_idx:.1f} | Outperformance KI: {ki_idx - real_idx:+.1f} Punkte")
    return "\n".join(lines) or "Noch keine Verlaufsdaten."


def build_prompt(scope: str) -> str:
    real = portfolio_summary()
    real_key = "krypto" if scope == "crypto" else "aktien"
    return (
        "== DEIN KI-DEPOT ==\n" + _shadow_block(scope) + "\n\n"
        f"== ECHTES {real_key.upper()}-PORTFOLIO DES NUTZERS (Vergleichsmaßstab) ==\n"
        + json.dumps(real[real_key], ensure_ascii=False, indent=1) + "\n\n"
        "== DEINE FRÜHEREN EMPFEHLUNGEN & ERGEBNISSE ==\n" + _history_block(scope) + "\n\n"
        "== PERFORMANCE-STAND ==\n" + _performance_block(scope) + "\n\n"
        "== RISIKOPROFIL DES NUTZERS ==\n" + risk_profile_prompt() + "\n\n"
        "Gib jetzt deine aktualisierten Handlungsanweisungen für dein KI-Depot."
    )


def estimate_cost(model: str) -> float:
    p_in, p_out = CLAUDE_PRICING.get(model, (5.0, 25.0))
    return 6000 / 1e6 * p_in + 1500 / 1e6 * p_out


def run_strategy(scope: str, model: str, progress_cb=None) -> dict:
    """Holt neue KI-Anweisungen für ein Scope-Depot und wendet sie an."""
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    if scope not in shadow.SCOPES:
        return {"error": f"Unbekannter Scope: {scope}"}
    if not shadow.exists(scope):
        return {"error": f"{shadow.SCOPES[scope]}-Depot ist noch nicht initialisiert."}

    progress("Analysiere Depot & Marktlage ...")
    prompt = build_prompt(scope)

    progress("Portfolio-Stratege erstellt Empfehlungen ...")
    parsed, usage, err = run_json_agent(_system_for(scope), prompt, model, _schema_for(scope))
    if err or parsed is None:
        cost = (usage or {}).get("cost_usd", 0.0)
        error_result = {
            "error": err or "Keine verwertbare Antwort.",
            "usage": usage or {},
            "total_cost_usd": cost,
        }
        if cost > 0:
            # Anthropic hat trotz Fehler (z.B. max_tokens) bereits abgerechnet -
            # das muss in agent_runs und im Session-Kostenzähler sichtbar bleiben.
            db.log_agent_run(
                target=_TARGETS[scope], mode="strategy", total_score=None,
                recommendation="Fehlgeschlagen", cost_usd=cost, report=error_result,
            )
        return error_result

    empfehlungen = parsed.get("empfehlungen", []) or []
    cost = usage.get("cost_usd", 0.0)

    result = {
        "mode": "strategy",
        "scope": scope,
        "marktausblick": parsed.get("marktausblick", ""),
        "gesamtkommentar": parsed.get("gesamtkommentar", ""),
        "empfehlungen": empfehlungen,
        "usage": usage,
        "total_cost_usd": cost,
    }
    run_id = db.log_agent_run(
        target=_TARGETS[scope], mode="strategy", total_score=None,
        recommendation=f"{len(empfehlungen)} Empfehlungen", cost_usd=cost, report=result,
    )

    progress("Wende Empfehlungen aufs KI-Depot an ...")
    outcomes = shadow.apply_recommendations(scope, empfehlungen, run_id=run_id)
    result["outcomes"] = outcomes
    result["run_id"] = run_id
    return result
