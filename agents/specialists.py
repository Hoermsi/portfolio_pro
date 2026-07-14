"""Die vier Spezialisten-Agenten des Analyse-Teams."""
from agents.base import ANALYST_SCHEMA, run_json_agent
from core.models import AgentReport

_COMMON = (
    "Du bist Teil eines Analyse-Teams eines Asset Managers. Du gibst keine "
    "Anlageberatung, sondern eine sachliche fachliche Einschätzung auf Deutsch. "
    "Bewerte NUR aus deinem Fachgebiet heraus und stütze dich ausschließlich auf "
    "die gelieferten Daten - erfinde keine Zahlen. "
    "score: 0 = sehr negativ/riskant, 100 = sehr positiv."
)

SPECIALISTS = {
    "technik": {
        "name": "Technik-Analyst",
        "emoji": "📈",
        "system": _COMMON + (
            " Fachgebiet: Technische Analyse (RSI, gleitende Durchschnitte, "
            "Bollinger-Bänder, MACD, Fibonacci-Levels, Trendstruktur). Beurteile "
            "Trend, Momentum und ob das aktuelle Niveau eher Einstieg oder Ausstieg nahelegt."
        ),
    },
    "fundamental": {
        "name": "Fundamental-Analyst",
        "emoji": "🏛️",
        "system": _COMMON + (
            " Fachgebiet: Fundamentalanalyse. Bei Aktien: Bewertung (KGV), Margen, "
            "Wachstum, Verschuldung, Sektor. Bei Kryptowährungen: Marktkapitalisierung, "
            "Rang, Supply-Struktur, Abstand zum Allzeithoch, Marktstellung. "
            "Beurteile, ob der Wert fundamental attraktiv bepreist ist."
        ),
    },
    "news": {
        "name": "News- & Sentiment-Analyst",
        "emoji": "📰",
        "system": _COMMON + (
            " Fachgebiet: Nachrichtenlage und Marktstimmung. Bewerte die gelieferten "
            "Schlagzeilen: Gibt es kursrelevante Ereignisse, Risiken oder Katalysatoren? "
            "Wenn die Nachrichtenlage dünn ist, sage das ehrlich und bleibe neutral."
        ),
    },
    "risiko": {
        "name": "Risiko-Manager",
        "emoji": "🛡️",
        "system": _COMMON + (
            " Fachgebiet: Risikomanagement. Bewerte Volatilität, Max Drawdown, Sharpe "
            "Ratio sowie - falls vorhanden - die Positionsgröße im Portfolio-Kontext "
            "(Klumpenrisiko). Hier bedeutet ein hoher score: Risiko gut vertretbar; "
            "ein niedriger score: Risiko hoch oder Position zu groß."
        ),
    },
}


def run_specialist(key: str, prompt_block: str, model: str, task: str) -> AgentReport:
    spec = SPECIALISTS[key]
    user_prompt = f"{task}\n\n{prompt_block}"
    parsed, usage, err = run_json_agent(spec["system"], user_prompt, model, ANALYST_SCHEMA)
    report = AgentReport(agent=spec["name"], usage=usage)
    if err or parsed is None:
        report.error = err or "Keine verwertbare Antwort."
        return report
    try:
        report.score = max(0, min(100, int(parsed.get("score", 50))))
    except (TypeError, ValueError):
        report.score = 50
    report.urteil = str(parsed.get("urteil", ""))
    report.zusammenfassung = str(parsed.get("zusammenfassung", ""))
    report.punkte = [str(p) for p in (parsed.get("punkte") or [])]
    return report
