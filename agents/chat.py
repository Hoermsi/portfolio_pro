"""Claude-Chat mit Tool-Use: Claude holt sich Portfolio- und Marktdaten selbst."""
import json

import anthropic

from agents import dossier as dossier_mod
from agents.base import compute_cost
from core import config
from core.portfolio import portfolio_summary

_SYSTEM = (
    "Du bist der persönliche Asset-Management-Assistent des Nutzers. Du hast über "
    "Tools Zugriff auf sein echtes Portfolio (Aktien und Krypto getrennt, plus das "
    "Bank-Cash-Guthaben) sowie auf Marktdaten. Das Gesamtvermögen umfasst Aktien, "
    "Krypto UND Cash - bei Fragen zu Vermögen oder Allokation immer den Cash-Bestand "
    "mitberücksichtigen. Nutze die Tools, statt zu raten - besonders bei Fragen zum "
    "Portfolio oder zu aktuellen Kursen. Antworte auf Deutsch, nüchtern und präzise, "
    "mit konkreten Zahlen aus den Tool-Ergebnissen. Du gibst keine Anlageberatung, "
    "sondern fachliche Einschätzungen. Beträge in EUR formatieren."
)

TOOLS = [
    {
        "name": "get_portfolio_summary",
        "description": (
            "Liefert das komplette aktuelle Portfolio des Nutzers: alle Aktien- und "
            "Krypto-Positionen mit Werten in EUR, Gewinn/Verlust, den Bank-Cash-Bestand "
            "(cash_eur) sowie das Gesamtvermögen (gesamt_eur, inkl. Cash) und die "
            "Allokation (aktien_pct / krypto_pct / cash_pct). "
            "Rufe dieses Tool bei jeder Frage zum Portfolio, Vermögen, Cash oder zu Positionen auf."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_asset_data",
        "description": (
            "Liefert aktuelle Daten zu einem einzelnen Wert: Kurs, technische "
            "Indikatoren, Fundamentaldaten, Risiko-Kennzahlen und News. Rufe dieses "
            "Tool auf, wenn nach einer bestimmten Aktie oder Kryptowährung gefragt wird."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker bzw. Krypto-Symbol, z.B. NVDA oder BTC"},
                "asset_type": {"type": "string", "enum": ["stock", "crypto"]},
            },
            "required": ["symbol", "asset_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_shadow_portfolio",
        "description": (
            "Liefert den Stand der beiden KI-gemanagten Schatten-Depots (Krypto-KI und "
            "Aktien-KI, getrennte Experimente): virtuelle Positionen, Performance-Vergleich "
            "gegen die jeweilige Klasse des echten Portfolios (Index Start=100), sowie die "
            "letzten von der KI ausgeführten Änderungen (Changelog). Rufe dies auf bei "
            "Fragen wie 'Wie laufen deine KI-Portfolios?', 'Schlägt die KI mein echtes "
            "Depot?' oder zu den bisherigen Umschichtungen."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _shadow_scope_summary(scope: str) -> dict:
    from core import shadow
    if not shadow.exists(scope):
        return {"status": f"Kein {shadow.SCOPES[scope]}-Depot angelegt."}
    vals = shadow.valued_shadow(scope)
    total = shadow.total_value(scope, vals)
    comp = shadow.comparison_df(scope)
    stand = None
    if comp is not None and len(comp) >= 1:
        last = comp.iloc[-1]
        stand = {"echt_index": round(last.get("Echt"), 1) if last.get("Echt") is not None else None,
                 "ki_index": round(last.get("KI"), 1) if last.get("KI") is not None else None}
    return {
        "start": shadow.start_info(scope),
        "ki_gesamtwert_eur": round(total, 2),
        "positionen": [{"symbol": v["symbol"], "typ": v["asset_type"],
                        "wert_eur": round(v["value_eur"], 2) if v["value_eur"] else None}
                       for v in vals],
        "performance_index_start_100": stand,
        "letzte_aenderungen": [
            {"zeit": e["created_at"][:16], "aktion": e["aktion"],
             "von": e["von_symbol"], "nach": e["nach_symbol"],
             "wert_eur": e["wert_eur"], "notiz": e.get("notiz")}
            for e in shadow.db.list_shadow_log(scope, 15)
        ],
    }


def _shadow_summary() -> dict:
    return {
        "krypto_ki": _shadow_scope_summary("crypto"),
        "aktien_ki": _shadow_scope_summary("stock"),
    }


def _execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name == "get_portfolio_summary":
            return json.dumps(portfolio_summary(), ensure_ascii=False, default=str)
        if name == "get_asset_data":
            d = dossier_mod.build_asset_dossier(
                tool_input.get("symbol", ""), tool_input.get("asset_type", "stock")
            )
            if d is None:
                return json.dumps({"fehler": "Keine Kursdaten gefunden - Symbol prüfen."})
            return dossier_mod.dossier_prompt(d)
        if name == "get_shadow_portfolio":
            return json.dumps(_shadow_summary(), ensure_ascii=False, default=str)
        return json.dumps({"fehler": f"Unbekanntes Tool: {name}"})
    except Exception as e:
        return json.dumps({"fehler": str(e)})


def chat_turn(messages: list, model: str, max_iterations: int = 8):
    """Führt einen Chat-Turn inkl. Tool-Schleife aus.

    messages: bisherige Konversation im API-Format (letzter Eintrag = User-Frage).
    Gibt (messages, final_text, cost_usd, error) zurück - messages enthält danach
    alle Assistant-/Tool-Turns.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        return messages, None, 0.0, "ANTHROPIC_API_KEY fehlt in der .env"
    client = anthropic.Anthropic(api_key=api_key)
    cost = 0.0

    try:
        for _ in range(max_iterations):
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=messages,
            )
            cost += compute_cost(model, response.usage)["cost_usd"]

            if response.stop_reason == "refusal":
                return messages, None, cost, "Anfrage wurde vom Modell abgelehnt."

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                final_text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                return messages, final_text, cost, None

            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    result = _execute_tool(block.name, block.input or {})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

        return messages, None, cost, "Maximale Tool-Iterationen erreicht."
    except anthropic.AuthenticationError:
        return messages, None, cost, "ANTHROPIC_API_KEY ungültig."
    except anthropic.RateLimitError:
        return messages, None, cost, "Rate-Limit erreicht - bitte kurz warten."
    except anthropic.APIStatusError as e:
        return messages, None, cost, f"Claude-API-Fehler ({e.status_code}): {e.message}"
    except anthropic.APIConnectionError:
        return messages, None, cost, "Keine Verbindung zur Claude-API."
