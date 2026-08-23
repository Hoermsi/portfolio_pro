"""KI-Zyklus-Einschätzung: eigenständiger dritter Analyse-Modus (neben Einzel-
wert- und Portfolio-Review), nur Krypto, nur auf Knopfdruck. Beantwortet drei
Fragen: Zusammenfassung der Datenlage, wo im Zyklus wir stehen, und ob ein
Zyklus-Top in Reichweite sein könnte - unter Berücksichtigung ALLER drei
Krypto-Barometer der Marktanalyse-Seite (Markt-Temperatur, Zyklus-Score,
Altcoin-Überhitzung), nicht nur des Zyklus-Scores.

Arbeitsteilung bleibt strikt (wie beim Portfolio-Strategen): die Zahlen kommen
deterministisch aus analysis/cycle.py, analysis/market_timing.py und
analysis/alt_top.py - der Agent erzeugt NIE selbst einen Score, sonst wären
Reproduzierbarkeit und der Backtest wertlos. Sein Mehrwert liegt dort, wo das
Quant-Modell blind ist: struktureller Regimewechsel (ETF-Flows, Regulierung,
Token-Unlocks), und die Frage, ob die historische Analogie diesmal überhaupt
trägt. `top_wahrscheinlichkeit` ist deshalb bewusst ein ZWEITER, von allen drei
Scores UNABHÄNGIGER Wert - eine Abweichung ist Information, kein Widerspruch.

`cycle`/`temp`/`alt` werden von views/asset_detail.py bereits berechnet
durchgereicht (siehe run_cycle_analysis()) statt hier ein zweites Mal
berechnet zu werden - der Altcoin-Korb allein braucht ~10 sequentielle
Netzabrufe, das würde sich sonst bei jedem Klick verdoppeln.
"""
from __future__ import annotations

import pandas as pd

from agents.base import run_json_agent
from agents.dossier import risk_profile_prompt
from core import db
from core.config import CLAUDE_PRICING
from data import news as news_data

_TARGET = "ZYKLUS-KRYPTO"

_REGIME_ENUM = ["Akkumulation", "Aufschwung", "Später Bulle", "Euphorie",
                "Distribution", "Bär", "Unbekannt"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "zusammenfassung": {
            "type": "string",
            "description": "3-5 Sätze: die aktuelle Datenlage in Klartext zusammengefasst",
        },
        "zyklus_phase": {"type": "string", "enum": _REGIME_ENUM,
                         "description": "deine eigene Einordnung - darf vom regelbasierten Regime abweichen"},
        "phase_begruendung": {"type": "string", "description": "1-2 Sätze, warum diese Phase"},
        "top_wahrscheinlichkeit": {
            "type": "integer",
            "description": ("0-100: wie wahrscheinlich ist ein Zyklus-Top in Reichweite (Wochen "
                            "bis wenige Monate)? Das ist NICHT der Zyklus-Score (der misst "
                            "Überhitzung JETZT) - hier geht es um die Nähe zu einem Wendepunkt."),
        },
        "argumente_top": {"type": "array", "items": {"type": "string"},
                          "description": "3-5 konkrete Argumente, die für ein nahes Top sprechen"},
        "argumente_dagegen": {"type": "array", "items": {"type": "string"},
                              "description": "3-5 konkrete Argumente, die dagegen sprechen"},
        "modell_abweichung": {
            "type": "string",
            "description": ("Wo und warum weicht deine Einschätzung vom quantitativen Zyklus-Score "
                            "ab (falls überhaupt)? Explizit benennen, sonst 'Keine wesentliche "
                            "Abweichung.' schreiben."),
        },
        "beobachten": {"type": "array", "items": {"type": "string"},
                       "description": "3-5 konkrete, überprüfbare nächste Auslöser/Datenpunkte"},
        "unsicherheit": {
            "type": "string",
            "description": ("1-3 Sätze: was du ausdrücklich NICHT wissen kannst (z.B. regulatorische "
                            "Ereignisse, Liquidationskaskaden, Black-Swan-Ereignisse)"),
        },
    },
    "required": ["zusammenfassung", "zyklus_phase", "phase_begruendung", "top_wahrscheinlichkeit",
                "argumente_top", "argumente_dagegen", "modell_abweichung", "beobachten", "unsicherheit"],
    "additionalProperties": False,
}

_SYSTEM = (
    "Du bist ein erfahrener, nüchterner Krypto-Marktanalyst. Du beurteilst den "
    "GESAMTMARKT (Bitcoin-Zyklus), NICHT das Depot des Nutzers - eine eigene "
    "Ausstiegs-Rangliste für seine Positionen existiert bereits separat. Du "
    "erzeugst NIEMALS selbst Kennzahlen oder Scores; die gelieferten Zahlen "
    "(Zyklus-Score, Indikator-Perzentile, historischer Vergleich, Backtest) sind "
    "bereits fertig berechnet - deine Aufgabe ist EINORDNUNG und KONTEXT, nicht "
    "Neuberechnung. Erfinde keine Zahlen, die nicht im Prompt stehen.\n"
    "Dein eigentlicher Mehrwert liegt dort, wo das Quant-Modell blind ist: "
    "struktureller Regimewechsel (ETF-Flows, institutionelle Nachfrage, "
    "Regulierung, große Token-Unlocks), und ob die historische Analogie zu "
    "früheren Zyklen diesmal überhaupt trägt. Wenn du dem quantitativen Score "
    "widersprichst, sag klar warum (Feld modell_abweichung) - eine bloße "
    "Nacherzählung des Scores ohne eigene Einordnung ist nutzlos.\n"
    "Du bekommst DREI unabhängige, deterministisch berechnete Krypto-Barometer, "
    "die unterschiedliche Fragen beantworten - vermische sie nicht:\n"
    "- MARKT-TEMPERATUR: kurzfristige Stimmung, mischt sehr unterschiedliche "
    "Zeitfenster (24h bis 200 Tage). Ein Stimmungsbild, kein Timing-Signal.\n"
    "- ZYKLUS-INDIKATOREN: wie überhitzt BTC JETZT gegenüber seiner eigenen "
    "mehrjährigen Historie bewertet ist.\n"
    "- ALTCOIN-ÜBERHITZUNG: wie weit der ALT-Markt gegenüber seiner eigenen "
    "Historie gelaufen ist, unabhängig von BTC. KEIN Top-Timer - der historische "
    "Höchstwert dieser Kennzahl lag beim Nov-2021-Top sechs Monate VOR dem "
    "eigentlichen Top.\n"
    "Diese drei dürfen und werden oft auseinanderlaufen - das ist die interessante "
    "Information, nicht ein Fehler, den du auflösen musst. top_wahrscheinlichkeit "
    "ist KEINER der drei Scores, sondern deine eigene, unabhängige Einschätzung "
    "der Nähe zu einem Wendepunkt.\n"
    "Wenn du einen Baustein-Wert aus den ZYKLUS-INDIKATOREN im Fließtext nennst "
    "(zusammenfassung, phase_begruendung, argumente_top/dagegen), übernimm IMMER "
    "den vollständigen Bezeichner samt Klammerzusatz aus dem Prompt (z.B. 'Fear & "
    "Greed (30-Tage-Schnitt) bei 34', nicht nur 'Fear & Greed bei 34'). Der "
    "Klammerzusatz nennt Zeitfenster oder Berechnungsart - ohne ihn wirkt eine "
    "Zahl wie ein tagesaktueller Wert, obwohl sie z.B. ein 30-Tage-Mittel ist, "
    "und weicht dann scheinbar grundlos von dem ab, was der Nutzer anderswo "
    "(z.B. CoinMarketCap) sieht.\n"
    "Dies ist keine Anlageberatung, sondern eine Markteinordnung. Sag klar, was "
    "du nicht wissen kannst."
)

# Bekannte Referenzpunkte vorheriger Zyklus-Extreme, gegen die die aktuelle
# Lage im Prompt gespiegelt wird - der Kern für ein belastbares Top-Urteil:
# ohne "MVRV stand damals bei X, heute bei Y" ist jede Einschätzung Stimmung.
_REFERENCE_POINTS = [
    ("Top Dez. 2017", "2017-12-17"),
    ("Boden Dez. 2018", "2018-12-15"),
    ("Top Nov. 2021", "2021-11-10"),
    ("Boden Nov. 2022", "2022-11-21"),
]


def _asof_value(series: pd.Series | None, date_str: str) -> float | None:
    if series is None or series.empty:
        return None
    val = series.asof(pd.Timestamp(date_str))
    return None if val is None or pd.isna(val) else float(val)


def _reference_row(label: str, date_str: str, price: pd.Series | None,
                   mvrv: pd.Series | None, puell: pd.Series | None) -> str:
    parts = [label]
    p, m, pu = _asof_value(price, date_str), _asof_value(mvrv, date_str), _asof_value(puell, date_str)
    if p is not None:
        parts.append(f"BTC ≈ {p:,.0f} EUR")
    if m is not None:
        parts.append(f"MVRV Z {m:.2f}")
    if pu is not None:
        parts.append(f"Puell {pu:.2f}")
    return " | ".join(parts) if len(parts) > 1 else f"{label}: keine Daten"


def _historical_comparison_block() -> str:
    from analysis import cycle as cycle_mod
    price = cycle_mod.btc_price_series()
    mvrv = cycle_mod._onchain_series("mvrv-zscore")
    puell = cycle_mod._onchain_series("puell-multiple")

    lines = [_reference_row(label, date_str, price, mvrv, puell) for label, date_str in _REFERENCE_POINTS]
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    lines.append(_reference_row("Heute", today, price, mvrv, puell))
    return "\n".join(lines)


def _cycle_block(cycle: dict) -> str:
    lines = [
        f"Zyklus-Score: {cycle['score']}/100 (Datenabdeckung {cycle['coverage_pct']:.0f}%)",
        f"Regelbasiertes Regime: {cycle['regime']} - {cycle['regime_reason']}",
    ]
    for row in cycle["breakdown"]:
        lines.append(f"- {row['label']}: {row['text']} "
                     f"(Perzentil {row['score']:.0f}/100, Gewicht {row['weight_pct']:.0f}%)")
    if cycle["unavailable"]:
        lines.append("Nicht verfügbar (aus der Gewichtung genommen): " + ", ".join(cycle["unavailable"]))
    return "\n".join(lines)


def _market_temp_block(temp: dict | None) -> str:
    if not temp or temp.get("score") is None:
        return "Markt-Temperatur in diesem Lauf nicht mitgegeben."
    lines = [f"Score: {temp['score']}/100 ({temp['classification']}, "
            f"Abdeckung {temp['coverage_pct']:.0f}%)",
            "Achtung: mischt sehr unterschiedliche Zeitfenster (24h bis 200 Tage) - "
            "ein Stimmungsbild, kein Timing-Signal."]
    for row in temp.get("breakdown", []):
        lines.append(f"- {row['label']}: {row['text']} (Gewicht {row['weight_pct']:.0f}%)")
    return "\n".join(lines)


def _alt_top_block(alt: dict | None) -> str:
    if not alt or alt.get("score") is None:
        return "Altcoin-Überhitzung in diesem Lauf nicht mitgegeben."
    limited_note = ", eingeschränkt aussagekräftig (wenige Coins verfügbar)" if alt.get("limited") else ""
    lines = [f"Score: {alt['score']}/100 ({alt['regime']}, Korb {alt['basket_size']}/10 Coins"
            f"{limited_note})",
            "KEIN Top-Timer: misst, wie weit der ALT-Markt (unabhängig von BTC) gegenüber "
            "seiner eigenen Historie gelaufen ist. Der historische Höchstwert dieser Kennzahl "
            "lag beim Nov-2021-Top sechs Monate VOR dem eigentlichen Top - ein hoher Wert ist "
            "kein verlässliches Timing-Signal, nur ein Ausdehnungs-Maß."]
    for row in alt.get("breakdown", []):
        lines.append(f"- {row['label']}: {row['text']} (Gewicht {row['weight_pct']:.0f}%)")
    return "\n".join(lines)


def _trigger_price_block(cycle: dict) -> str:
    from analysis import cycle as cycle_mod
    from core import profile
    cfg = profile.ladder_config("crypto")
    lines = []
    for i, th in enumerate(cfg["sell"], start=1):
        tp = cycle_mod.trigger_price(th, cycle=cycle)
        if tp:
            lines.append(f"Verkaufsstufe {i} (Score >= {th:.0f}): ab BTC ≈ {tp:,.0f} EUR "
                         "(Näherung, siehe Erklärung unten)")
        else:
            lines.append(f"Verkaufsstufe {i} (Score >= {th:.0f}): bei aktueller On-Chain-/"
                         "Sentiment-Lage auch bei sehr hohem Preis nicht erreichbar")
    lines.append("Näherung: nur die preisabhängigen Bausteine (Mayer, 2-Jahres-Multiple, "
                "ATH-Abstand) werden variiert - On-Chain- und Sentiment-Bausteine bleiben "
                "auf ihrem heutigen Stand eingefroren.")
    return "\n".join(lines)


def _depot_block() -> str:
    from analysis import exit_ranking
    ranking = exit_ranking.exit_ranking()
    if not ranking:
        return "Kein Krypto-Bestand vorhanden."
    has_btc = any(r["symbol"] == "BTC" for r in ranking)
    depot_note = "BTC ist Teil des Bestands." if has_btc else "reines Alt-Depot, kein BTC im Bestand."
    total_value = sum(r["value_eur"] or 0.0 for r in ranking)
    lines = [f"{len(ranking)} Krypto-Positionen, Gesamtwert ca. {total_value:,.0f} EUR ({depot_note})"]
    lines.append("Schwächste 5 nach der separaten Ausstiegs-Rangliste (zuerst zu reduzieren, "
                "falls eine Verkaufsstufe aktiv wird):")
    for r in ranking[:5]:
        score_txt = f"{r['exit_score']:.0f}" if r["exit_score"] is not None else "n/v"
        lines.append(f"- {r['symbol']}: exit_score {score_txt}")
    return "\n".join(lines)


def _backtest_block() -> str:
    from analysis import cycle_backtest
    bt = cycle_backtest.run_backtest()
    if not bt.get("available"):
        return "Kein Backtest verfügbar: " + bt.get("reason", "unbekannter Grund")
    lines = [
        f"Walk-Forward-Backtest {bt['start_date']} bis {bt['end_date']} "
        "(nur Verkaufsseite der Leiter, kein simulierter Wiedereinstieg):",
        f"Deine Leiter-Regel: {bt['ladder_value_eur']:,.0f} EUR-Äquivalent je ursprünglich "
        f"1 BTC-Einheit, vs. Halten {bt['buy_hold_value_eur']:,.0f} EUR, vs. perfekter, "
        f"kostenfreier Verkauf am historischen Top {bt['perfect_top_value_eur']:,.0f} EUR.",
    ]
    if bt.get("avg_miss_vs_next_peak_pct") is not None:
        lines.append(f"{bt['trade_count']} Verkaufs-Trades ausgelöst, im Schnitt "
                     f"{bt['avg_miss_vs_next_peak_pct']:.0f}% unter dem jeweils nächsten Hoch "
                     f"(schlechtester Einzel-Trade: {bt['worst_miss_vs_next_peak_pct']:.0f}%).")
    else:
        lines.append("In diesem Zeitraum wurde keine Verkaufsstufe ausgelöst.")
    lines.append("Hinweis: Plausibilitätscheck über einen einzigen historischen Pfad mit sehr "
                "wenigen vollständigen Zyklen - kein Beleg für optimale Schwellen; kein "
                "Wiedereinstieg, keine Steuern, keine Cash-Verzinsung modelliert.")
    return "\n".join(lines)


def _news_block() -> str:
    items = news_data.get_news("BTC", "crypto", limit=6)
    if not items:
        return "Keine aktuellen Schlagzeilen verfügbar."
    return "\n".join(f"- {it.get('title', '')} ({it.get('source', '')})" for it in items)


def build_prompt(cycle: dict, temp: dict | None = None, alt: dict | None = None) -> str:
    return (
        "== ZYKLUS-INDIKATOREN (BTC, mehrjährig) ==\n" + _cycle_block(cycle) + "\n\n"
        "== MARKT-TEMPERATUR (kurzfristig, gemischte Zeitfenster) ==\n"
        + _market_temp_block(temp) + "\n\n"
        "== ALTCOIN-ÜBERHITZUNG (unabhängig vom BTC-Zyklus) ==\n" + _alt_top_block(alt) + "\n\n"
        "== HISTORISCHER VERGLEICH (frühere Zyklus-Extreme vs. heute) ==\n"
        + _historical_comparison_block() + "\n\n"
        "== TRIGGER-PREISE DEINER EIGENEN LEITER ==\n" + _trigger_price_block(cycle) + "\n\n"
        "== DEPOT-KONTEXT ==\n" + _depot_block() + "\n\n"
        "== AKTUELLE SCHLAGZEILEN ==\n" + _news_block() + "\n\n"
        "== BACKTEST DEINER LEITER-REGEL ==\n" + _backtest_block() + "\n\n"
        "== RISIKOPROFIL DES NUTZERS ==\n" + risk_profile_prompt() + "\n\n"
        "Gib jetzt deine Zyklus-Einschätzung unter Berücksichtigung ALLER drei Barometer."
    )


def estimate_cost(model: str) -> float:
    p_in, p_out = CLAUDE_PRICING.get(model, (5.0, 25.0))
    return 4800 / 1e6 * p_in + 900 / 1e6 * p_out


def run_cycle_analysis(model: str, progress_cb=None, cycle=None, temp=None, alt=None) -> dict:
    """Holt eine KI-Zyklus-Einschätzung. Läuft ausschließlich auf expliziten
    Aufruf (Kosten) - nie automatisch beim Seitenaufruf.

    `cycle`/`temp`/`alt`: bereits im selben Seitenaufruf berechnete Dicts
    (analysis.cycle.cycle_score() / market_timing.market_temperature() /
    analysis.alt_top.alt_top_score()) - werden NICHT neu berechnet, wenn
    übergeben (Regelfall: views/asset_detail.py reicht sie durch). Ohne
    `cycle` wird nur dieser als einzig zwingend benötigter Wert selbst
    nachgeladen; fehlende `temp`/`alt` erscheinen im Prompt ehrlich als
    "nicht mitgegeben" statt einen zweiten, teuren Netz-Roundtrip zu erzwingen
    (der Altcoin-Korb allein braucht ~10 sequentielle Abrufe)."""
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    progress("Berechne Zyklus-Kennzahlen ...")
    from analysis import cycle as cycle_mod
    cycle = cycle if cycle is not None else cycle_mod.cycle_score()
    if cycle["score"] is None:
        return {"error": "Zyklus-Score aktuell nicht berechenbar (zu wenig Datenlage - "
                         "BTC-Historie oder On-Chain-Quelle nicht erreichbar)."}

    prompt = build_prompt(cycle, temp, alt)

    progress("KI wertet Markt-Temperatur, Zyklus-Score und Altcoin-Überhitzung aus ...")
    parsed, usage, err = run_json_agent(_SYSTEM, prompt, model, _SCHEMA)
    if err or parsed is None:
        cost = (usage or {}).get("cost_usd", 0.0)
        error_result = {"error": err or "Keine verwertbare Antwort.", "usage": usage or {},
                        "total_cost_usd": cost}
        if cost > 0:
            # Anthropic hat trotz Fehler (z.B. max_tokens) bereits abgerechnet - das muss
            # in agent_runs und im Session-Kostenzähler sichtbar bleiben (Muster strategist.py).
            db.log_agent_run(target=_TARGET, mode="cycle", total_score=None,
                             recommendation="Fehlgeschlagen", cost_usd=cost, report=error_result)
        return error_result

    cost = usage.get("cost_usd", 0.0)
    try:
        top_prob = max(0, min(100, int(parsed.get("top_wahrscheinlichkeit", 50))))
    except (TypeError, ValueError):
        top_prob = 50

    result = {
        "mode": "cycle",
        "cycle_score": cycle,
        "zusammenfassung": parsed.get("zusammenfassung", ""),
        "zyklus_phase": parsed.get("zyklus_phase", ""),
        "phase_begruendung": parsed.get("phase_begruendung", ""),
        "top_wahrscheinlichkeit": top_prob,
        "argumente_top": parsed.get("argumente_top", []) or [],
        "argumente_dagegen": parsed.get("argumente_dagegen", []) or [],
        "modell_abweichung": parsed.get("modell_abweichung", ""),
        "beobachten": parsed.get("beobachten", []) or [],
        "unsicherheit": parsed.get("unsicherheit", ""),
        "usage": usage,
        "total_cost_usd": cost,
    }
    run_id = db.log_agent_run(
        target=_TARGET, mode="cycle", total_score=top_prob,
        recommendation=result["zyklus_phase"] or "-", cost_usd=cost, report=result,
    )
    result["run_id"] = run_id
    return result


# --- Stufen-Vorschlag für "Eigene Stufen einstellen" (beide Märkte) ---
#
# Anders als der Rest dieses Moduls (nur Krypto) funktioniert dieser Teil für
# BEIDE Märkte: er braucht nur die Score/Breakdown-Form, die market_timing.
# market_temperature() (Aktien) und cycle.cycle_score() (Krypto) beide teilen
# ({"score","breakdown","coverage_pct",...}) - Backtest-Kontext kommt nur bei
# Krypto dazu, weil cycle_backtest.py BTC-spezifisch ist.

_LADDER_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "verkauf_stufe1": {"type": "number", "description": "Score-Schwelle 0-100 für Verkaufsstufe 1 (33%)"},
        "verkauf_stufe2": {"type": "number", "description": "Score-Schwelle 0-100 für Verkaufsstufe 2 (66%), > Stufe 1"},
        "verkauf_stufe3": {"type": "number", "description": "Score-Schwelle 0-100 für Verkaufsstufe 3 (100%), > Stufe 2"},
        "kauf_stufe1": {"type": "number", "description": "Score-Schwelle 0-100 für Kaufstufe 1 (33%)"},
        "kauf_stufe2": {"type": "number", "description": "Score-Schwelle 0-100 für Kaufstufe 2 (66%), < Stufe 1"},
        "kauf_stufe3": {"type": "number", "description": "Score-Schwelle 0-100 für Kaufstufe 3 (100%), < Stufe 2"},
        "begruendung": {"type": "string",
                        "description": "2-4 Sätze auf Deutsch: warum genau diese Schwellen, mit Bezug auf die gelieferten Daten"},
    },
    "required": ["verkauf_stufe1", "verkauf_stufe2", "verkauf_stufe3",
                "kauf_stufe1", "kauf_stufe2", "kauf_stufe3", "begruendung"],
    "additionalProperties": False,
}

_LADDER_SUGGESTION_SYSTEM = (
    "Du bist ein erfahrener, nüchterner Portfolio-Manager. Der Nutzer betreibt eine "
    "selbst konfigurierte Verkaufs-/Kauf-Leiter: bei Erreichen bestimmter Score-"
    "Schwellen (0-100, 100 = maximale Überhitzung/Gier) verkauft er stufenweise "
    "33/66/100% seines Bestands (Verkauf) bzw. setzt stufenweise 33/66/100% seines "
    "verfügbaren Cash ein (Kauf). Deine Aufgabe: sinnvolle Schwellenwerte für alle "
    "sechs Stufen vorschlagen, begründet aus der gelieferten Datenlage - keine "
    "erfundenen Zahlen, nur Einordnung der gelieferten.\n"
    "Regeln:\n"
    "- Verkauf-Schwellen aufsteigend (Stufe1 < Stufe2 < Stufe3), sinnvoll gestaffelt "
    "(nicht zu eng beieinander) und nicht bereits beim aktuellen Score ausgelöst, "
    "außer die Datenlage rechtfertigt das explizit (z.B. bereits extreme Werte).\n"
    "- Kauf-Schwellen absteigend (Stufe1 > Stufe2 > Stufe3), analog gestaffelt.\n"
    "- Feste Schwellen sind eine Heuristik, kein Backtest-Beweis - wäge das in der "
    "Begründung ab, insbesondere wenn Backtest-Daten vorliegen.\n"
    "- Nennst du in der Begründung einen Baustein-Wert aus BAUSTEINE, übernimm den "
    "vollständigen Bezeichner samt Klammerzusatz (z.B. 'Fear & Greed (30-Tage-"
    "Schnitt)', nicht nur 'Fear & Greed') - sonst wirkt eine Zahl wie ein "
    "tagesaktueller Wert, obwohl sie z.B. ein 30-Tage-Mittel ist.\n"
    "- Keine Anlageberatung, sondern Vorschlagswerte für die eigene Regel des Nutzers."
)


def build_ladder_suggestion_prompt(market: str, cyc: dict, current_sell: list[float],
                                   current_buy: list[float]) -> str:
    lines = [f"== AKTUELLER SCORE ({'Krypto-Zyklus' if market == 'crypto' else 'Markt-Temperatur'}) =="]
    lines.append(f"Score: {cyc['score']}/100 (Abdeckung {cyc['coverage_pct']:.0f}%)")
    if cyc.get("regime"):
        lines.append(f"Regime: {cyc['regime']} - {cyc.get('regime_reason', '')}")
    lines.append("")
    lines.append("== BAUSTEINE ==")
    for row in cyc["breakdown"]:
        lines.append(f"- {row['label']}: {row['text']} (Gewicht {row['weight_pct']:.0f}%)")
    lines.append("")
    lines.append("== AKTUELLE EIGENE STUFEN (zum Vergleich) ==")
    lines.append(f"Verkauf: {current_sell[0]:.0f} / {current_sell[1]:.0f} / {current_sell[2]:.0f}")
    lines.append(f"Kauf: {current_buy[0]:.0f} / {current_buy[1]:.0f} / {current_buy[2]:.0f}")
    if market == "crypto":
        from analysis import cycle_backtest
        bt = cycle_backtest.run_backtest(sell_thresholds=current_sell)
        lines.append("")
        lines.append("== BACKTEST DER AKTUELLEN VERKAUFS-SCHWELLEN ==")
        if bt.get("available"):
            lines.append(f"Walk-Forward {bt['start_date']} bis {bt['end_date']}: Leiter "
                         f"{bt['ladder_value_eur']:,.0f} vs. Halten {bt['buy_hold_value_eur']:,.0f} "
                         f"vs. perfekter Topverkauf {bt['perfect_top_value_eur']:,.0f}.")
            if bt.get("avg_miss_vs_next_peak_pct") is not None:
                lines.append(f"{bt['trade_count']} Trades, im Schnitt "
                             f"{bt['avg_miss_vs_next_peak_pct']:.0f}% unter dem nächsten Hoch verkauft.")
        else:
            lines.append(bt.get("reason", "nicht verfügbar"))
    lines.append("")
    lines.append("Schlage jetzt sinnvolle Stufen für alle sechs Werte vor.")
    return "\n".join(lines)


def estimate_ladder_suggestion_cost(model: str) -> float:
    p_in, p_out = CLAUDE_PRICING.get(model, (5.0, 25.0))
    return 1200 / 1e6 * p_in + 300 / 1e6 * p_out


def suggest_ladder_stages(market: str, cyc: dict, current_sell: list[float],
                          current_buy: list[float], model: str) -> dict:
    """KI-Vorschlag für die sechs Score-Schwellen der Verkaufs-/Kauf-Leiter -
    reiner TEXT-Vorschlag mit Begründung. core.profile.ladder_config bleibt die
    alleinige Quelle der Wahrheit; die KI ändert nichts automatisch, der Nutzer
    übernimmt einen Vorschlag (falls überhaupt) explizit per Klick."""
    prompt = build_ladder_suggestion_prompt(market, cyc, current_sell, current_buy)
    parsed, usage, err = run_json_agent(_LADDER_SUGGESTION_SYSTEM, prompt, model,
                                        _LADDER_SUGGESTION_SCHEMA)
    cost = (usage or {}).get("cost_usd", 0.0)
    target = f"LADDER-{market.upper()}"
    if err or parsed is None:
        error_result = {"error": err or "Keine verwertbare Antwort.", "usage": usage or {},
                        "total_cost_usd": cost}
        if cost > 0:
            db.log_agent_run(target=target, mode="ladder_suggestion", total_score=None,
                             recommendation="Fehlgeschlagen", cost_usd=cost, report=error_result)
        return error_result

    def _clamped(key: str) -> float:
        try:
            return round(min(100.0, max(0.0, float(parsed.get(key, 0.0)))), 1)
        except (TypeError, ValueError):
            return 0.0

    sell = [_clamped("verkauf_stufe1"), _clamped("verkauf_stufe2"), _clamped("verkauf_stufe3")]
    buy = [_clamped("kauf_stufe1"), _clamped("kauf_stufe2"), _clamped("kauf_stufe3")]
    result = {
        "sell": sell, "buy": buy,
        "begruendung": parsed.get("begruendung", ""),
        "usage": usage, "total_cost_usd": cost,
    }
    db.log_agent_run(target=target, mode="ladder_suggestion", total_score=None,
                     recommendation="Vorschlag", cost_usd=cost, report=result)
    return result
