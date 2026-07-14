"""Senior Asset Manager: orchestriert die Spezialisten und fasst zusammen."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from agents import dossier as dossier_mod
from agents.base import SENIOR_SCHEMA, run_json_agent
from agents.specialists import SPECIALISTS, run_specialist
from core import db
from core.models import AgentReport

_SENIOR_SYSTEM = (
    "Du bist ein erfahrener Senior Asset Manager. Dir liegen die Berichte deines "
    "Analyse-Teams vor (Technik, Fundamental, News/Sentiment, Risiko). Wäge die "
    "Berichte gegeneinander ab - du musst ihnen nicht folgen, aber Abweichungen "
    "begründen. Antworte nüchtern, konkret und auf Deutsch. Du gibst keine "
    "Anlageberatung, sondern eine professionelle Einschätzung. "
    "gesamtscore: 0 = klarer Verkauf, 100 = klarer Kauf."
)


def _reports_block(reports: dict[str, AgentReport]) -> str:
    lines = []
    for key, r in reports.items():
        spec = SPECIALISTS[key]
        lines.append(f"### Bericht {spec['name']}")
        if r.error:
            lines.append(f"(Ausfall: {r.error})")
        else:
            lines.append(f"Score: {r.score}/100 | Urteil: {r.urteil}")
            lines.append(r.zusammenfassung)
            lines += [f"- {p}" for p in r.punkte]
        lines.append("")
    return "\n".join(lines)


def run_asset_analysis(symbol: str, asset_type: str, specialist_model: str,
                       senior_model: str, progress_cb=None) -> dict:
    """Voll-Analyse eines Einzelwerts: 4 Spezialisten parallel + Senior-Synthese.

    progress_cb(text) wird für Statusmeldungen aufgerufen (optional).
    """
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    progress("Sammle Marktdaten ...")
    d = dossier_mod.build_asset_dossier(symbol, asset_type)
    if d is None:
        return {"error": f"Keine Kursdaten für {symbol} gefunden."}
    prompt_block = dossier_mod.dossier_prompt(d)
    task = f"Analysiere {d['name']} ({symbol}) aus Sicht deines Fachgebiets."

    progress("Spezialisten analysieren (parallel) ...")
    reports: dict[str, AgentReport] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            key: pool.submit(run_specialist, key, prompt_block, specialist_model, task)
            for key in SPECIALISTS
        }
        for key, fut in futures.items():
            reports[key] = fut.result()

    progress("Senior Asset Manager erstellt Gesamturteil ...")
    senior_prompt = (
        f"Erstelle dein Gesamturteil zu {d['name']} ({symbol}).\n\n"
        f"== DATENLAGE ==\n{prompt_block}\n\n"
        f"== BERICHTE DEINES TEAMS ==\n{_reports_block(reports)}"
    )
    senior, senior_usage, senior_err = run_json_agent(
        _SENIOR_SYSTEM, senior_prompt, senior_model, SENIOR_SCHEMA
    )

    total_cost = sum(r.usage.get("cost_usd", 0) for r in reports.values())
    total_cost += senior_usage.get("cost_usd", 0)

    result = {
        "mode": "asset",
        "target": symbol,
        "name": d["name"],
        "asset_type": asset_type,
        "specialists": {k: asdict(r) for k, r in reports.items()},
        "senior": senior,
        "senior_error": senior_err,
        "senior_usage": senior_usage,
        "total_cost_usd": total_cost,
    }
    db.log_agent_run(
        target=symbol, mode="asset",
        total_score=(senior or {}).get("gesamtscore"),
        recommendation=(senior or {}).get("empfehlung", ""),
        cost_usd=total_cost, report=result,
    )
    return result


_SCOPE_TARGETS = {"all": "PORTFOLIO", "stock": "PORTFOLIO-AKTIEN", "crypto": "PORTFOLIO-KRYPTO"}

_SCOPE_TASKS = {
    "all": ("Analysiere das Gesamtportfolio: Diversifikation, Klumpenrisiken, "
            "Korrelationen, Verhältnis Aktien/Krypto/Cash und Höhe der "
            "Liquiditätsquote (Cash-Anteil)."),
    "stock": ("Analysiere das Aktien-Portfolio: Diversifikation über Sektoren, "
              "Klumpenrisiken, Korrelationen der Positionen."),
    "crypto": ("Analysiere das Krypto-Portfolio: Diversifikation, Klumpenrisiken, "
               "Korrelationen, Anteil von Bitcoin/Ethereum vs. kleineren Coins."),
}


def run_portfolio_review(scope: str, specialist_model: str, senior_model: str,
                         progress_cb=None) -> dict:
    """Portfolio-Review: Risiko-Manager + Senior.

    scope: 'all' = Gesamtportfolio, 'stock' = nur Aktien, 'crypto' = nur Krypto.
    """
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    progress("Bewerte alle Positionen ...")
    d = dossier_mod.build_portfolio_dossier(scope)
    scope_name = d["scope_name"]
    if d["scope_value_eur"] <= 0:
        return {"error": f"{scope_name} ist leer oder keine Kurse verfügbar."}
    prompt_block = dossier_mod.portfolio_prompt(d)

    progress(f"Risiko-Manager prüft das {scope_name} ...")
    risk_report = run_specialist("risiko", prompt_block, specialist_model, _SCOPE_TASKS[scope])

    progress("Senior Asset Manager erstellt Portfolio-Urteil ...")
    senior_prompt = (
        f"Erstelle dein Gesamturteil zum {scope_name}: Ist die Zusammensetzung "
        "sinnvoll? Wo liegen die größten Risiken? Was wäre zu priorisieren? "
        "'empfehlung' bezieht sich hier auf die Gesamtausrichtung "
        "(z.B. Halten = Zusammensetzung passt).\n\n"
        f"== {scope_name.upper()} ==\n{prompt_block}\n\n"
        f"== BERICHT RISIKO-MANAGER ==\n{_reports_block({'risiko': risk_report})}"
    )
    senior, senior_usage, senior_err = run_json_agent(
        _SENIOR_SYSTEM, senior_prompt, senior_model, SENIOR_SCHEMA
    )

    total_cost = risk_report.usage.get("cost_usd", 0) + senior_usage.get("cost_usd", 0)
    result = {
        "mode": "portfolio",
        "scope": scope,
        "target": _SCOPE_TARGETS[scope],
        "name": scope_name,
        "specialists": {"risiko": asdict(risk_report)},
        "senior": senior,
        "senior_error": senior_err,
        "senior_usage": senior_usage,
        "total_cost_usd": total_cost,
    }
    db.log_agent_run(
        target=_SCOPE_TARGETS[scope], mode="portfolio",
        total_score=(senior or {}).get("gesamtscore"),
        recommendation=(senior or {}).get("empfehlung", ""),
        cost_usd=total_cost, report=result,
    )
    return result


def estimate_cost(mode: str, specialist_model: str, senior_model: str) -> float:
    """Grobe Kostenschätzung pro Analyse (USD), vor dem Start angezeigt."""
    from core.config import CLAUDE_PRICING
    sp_in, sp_out = CLAUDE_PRICING.get(specialist_model, (5.0, 25.0))
    se_in, se_out = CLAUDE_PRICING.get(senior_model, (5.0, 25.0))
    n_specialists = 4 if mode == "asset" else 1
    # Erfahrungswerte: ~2500 In / ~600 Out je Spezialist, ~4500 In / ~900 Out Senior
    cost = n_specialists * (2500 / 1e6 * sp_in + 600 / 1e6 * sp_out)
    cost += 4500 / 1e6 * se_in + 900 / 1e6 * se_out
    return cost
