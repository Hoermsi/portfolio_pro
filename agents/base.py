"""Basis für alle Claude-Agenten: API-Call, JSON-Parsing, Kosten-Tracking."""
import json
import re

import anthropic

from core import config

# JSON-Schema für Spezialisten-Antworten (Structured Outputs)
ANALYST_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0 = sehr negativ/riskant, 100 = sehr positiv"},
        "urteil": {"type": "string", "enum": ["positiv", "neutral", "negativ"]},
        "zusammenfassung": {"type": "string", "description": "2-3 Sätze auf Deutsch"},
        "punkte": {"type": "array", "items": {"type": "string"},
                    "description": "3-5 wichtigste Beobachtungen"},
    },
    "required": ["score", "urteil", "zusammenfassung", "punkte"],
    "additionalProperties": False,
}

SENIOR_SCHEMA = {
    "type": "object",
    "properties": {
        "gesamtscore": {"type": "integer"},
        "empfehlung": {"type": "string", "enum": ["Kaufen", "Aufstocken", "Halten", "Reduzieren", "Verkaufen"]},
        "begruendung": {"type": "string", "description": "4-6 Sätze auf Deutsch, nüchtern und konkret"},
        "chancen": {"type": "array", "items": {"type": "string"}},
        "risiken": {"type": "array", "items": {"type": "string"}},
        "allokationshinweis": {"type": "string", "description": "Hinweis zur Positionsgröße im Portfolio-Kontext"},
    },
    "required": ["gesamtscore", "empfehlung", "begruendung", "chancen", "risiken", "allokationshinweis"],
    "additionalProperties": False,
}

# Wie SENIOR_SCHEMA, aber zusätzlich mit konkreten Cash-Einsatz-Vorschlägen für
# das Portfolio-Review (frei investierbares Cash entlang der Zielallokation).
PORTFOLIO_SENIOR_SCHEMA = {
    "type": "object",
    "properties": {
        **SENIOR_SCHEMA["properties"],
        "cash_vorschlaege": {
            "type": "array",
            "description": "Konkrete Vorschläge, freies Cash einzusetzen. Leer, wenn kein "
                           "Cash über der Ziel-Reserve frei ist.",
            "items": {
                "type": "object",
                "properties": {
                    "betrag_eur": {"type": "number", "description": "einzusetzender Betrag in EUR"},
                    "symbol": {"type": "string", "description": "Ziel-Instrument, z.B. IWDA oder BTC"},
                    "asset_type": {"type": "string", "enum": ["stock", "crypto", "cash"]},
                    "begruendung": {"type": "string", "description": "kurze Begründung auf Deutsch"},
                },
                "required": ["betrag_eur", "symbol", "asset_type", "begruendung"],
                "additionalProperties": False,
            },
        },
    },
    "required": SENIOR_SCHEMA["required"] + ["cash_vorschlaege"],
    "additionalProperties": False,
}


def parse_json(text: str) -> dict | None:
    """Robustes Parsen, auch wenn JSON in Text eingebettet ist."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None


def compute_cost(model: str, usage) -> dict:
    """Kosten in USD aus der API-Usage (inkl. Cache-Lesen zu 10%)."""
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    price_in, price_out = config.CLAUDE_PRICING.get(model, (5.0, 25.0))
    cost = (
        (in_tok + cache_write * 1.25) / 1e6 * price_in
        + cache_read / 1e6 * price_in * 0.1
        + out_tok / 1e6 * price_out
    )
    return {"input": in_tok, "output": out_tok, "cache_read": cache_read, "cost_usd": cost}


def call_claude(system: str, user_prompt: str, model: str,
                schema: dict | None = None, max_tokens: int = 16000):
    """Einzelner Claude-Aufruf. Gibt (text, usage_dict, error) zurück.

    max_tokens großzügig, weil bei Modellen mit adaptivem Thinking (Opus 4.8,
    Sonnet 5) die Denk-Token gegen max_tokens zählen - bei zu knappem Budget
    wird die (erzwungene) JSON-Ausgabe abgeschnitten und liefert leere Felder.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        return None, {}, "ANTHROPIC_API_KEY fehlt in der .env"

    client = anthropic.Anthropic(api_key=api_key)
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    try:
        response = client.messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        # Fallback: Modell unterstützt Structured Outputs evtl. nicht -> ohne Format
        if schema:
            kwargs.pop("output_config", None)
            kwargs["system"] = system + "\nAntworte AUSSCHLIESSLICH mit einem JSON-Objekt nach dem vereinbarten Schema, ohne Text davor oder danach."
            try:
                response = client.messages.create(**kwargs)
            except anthropic.APIError as e2:
                return None, {}, f"Claude-API-Fehler: {e2}"
        else:
            return None, {}, f"Claude-API-Fehler: {e}"
    except anthropic.AuthenticationError:
        return None, {}, "Authentifizierung fehlgeschlagen - ANTHROPIC_API_KEY ungültig."
    except anthropic.RateLimitError:
        return None, {}, "Rate-Limit erreicht - bitte kurz warten und erneut versuchen."
    except anthropic.APIStatusError as e:
        return None, {}, f"Claude-API-Fehler ({e.status_code}): {e.message}"
    except anthropic.APIConnectionError:
        return None, {}, "Keine Verbindung zur Claude-API (Netzwerk prüfen)."

    usage = compute_cost(model, response.usage)
    if response.stop_reason == "refusal":
        return None, usage, "Anfrage wurde vom Modell abgelehnt."
    if response.stop_reason == "max_tokens":
        return None, usage, ("Antwort abgeschnitten (max_tokens erreicht) - die "
                             "Analyse hat das Token-Budget aufgebraucht. Bitte erneut "
                             "versuchen oder ein Modell ohne so ausführliches Reasoning wählen.")

    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return text, usage, None


def run_json_agent(system: str, user_prompt: str, model: str, schema: dict):
    """Agent-Call mit JSON-Ergebnis. Gibt (dict|None, usage, error) zurück."""
    text, usage, err = call_claude(system, user_prompt, model, schema=schema)
    if err:
        return None, usage, err
    parsed = parse_json(text)
    if parsed is None:
        return None, usage, f"Antwort nicht als JSON parsebar: {text[:200]}"
    return parsed, usage, None
