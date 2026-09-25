"""Gate-Ablation: misst je Pflichtbedingung in core.bot_signals.evaluate(),
was ihr Entfernen an Handelsfrequenz kostet und an Erwartungswert bringt
oder kostet - ausschliesslich OUT-OF-SAMPLE ueber dieselben verketteten
Walk-Forward-Fenster wie analysis.bot_walkforward, nicht ueber einen
einzelnen Zeitraum (eine Strategie kann in einem einzelnen Fenster gut
aussehen, obwohl sie nur dort funktioniert - genau der Grund, warum
bot_walkforward ueberhaupt existiert).

ERGEBNIS DER ERSTEN MESSUNG (Phase 3/4, 04.09.2026, 4x45-Tage-Fenster, 18
Symbole, Risikostufe 9, aus einer 180-Tage-Live-DB-Beobachtung mit 54 %
Stillstand durch Regime/ALLOW_SHORT und nur 3,3 % handelbaren
(Symbol,Takt)-Paaren): Gates 3 (Trend-Effizienz), 4 (Momentum vs. ATR), 5
(relative Staerke zu BTC) und 6 (Volumen) verschlechterten den
Erwartungswert beim Entfernen NICHT messbar - Gate 3 verbesserte Ø R sogar
um +0,142 (Profit-Faktor +19,9 %) ohne. Nur Gate 2 (Kurs/MA20/MA50-Struktur)
verdiente seine Komplexitaet messbar (Ø R -0,082, PF -17,7 % ohne). Gates
3/4/5/6 sind seither AUS core/bot_signals.py ENTFERNT (STRATEGY_VERSION
"2.2-swing4h") - VARIANTS unten testet deshalb nur noch die zwei
verbleibenden Gates. Gate 7 (Liquiditaet) konnte diese Messung nicht
pruefen: der Ruecktest kennt keine historische 24h-Volumen-Reihe und
rechnet immer mit unendlichem Volumen (siehe analysis/bot_backtest.py) -
Gate 7 blieb aus Risiko-, nicht aus Ablations-Gruenden bestehen.

EIN DATENABRUF FUER ALLE VARIANTEN: analysis.bot_backtest.load_market_data()
laeuft genau EINMAL, jede Variante laeuft danach ueber
analysis.bot_walkforward.run_windows_over_market() auf demselben,
bereits geladenen Datenstand - unterschiedliche Datenvollstaendigkeit
zwischen Varianten wuerde sonst wie ein Gate-Effekt aussehen, waere aber
nur Zufall des Abrufzeitpunkts (siehe die dokumentierte Streuung in
CLAUDE.md: "zwischen 0 und 30 Trades ... schwankende Vollstaendigkeit").

KEINE PARAMETERSUCHE: core.bot_signals.evaluate()'s `disabled_gates`-
Parameter kennt nur "ganz weg" oder "unveraendert" - kein Schwellenwert
wird verschoben. Das ist eine Ablationsstudie (wie eine Feature-Ablation in
ML: ein Merkmal entfernen, Rest unveraendert, Effekt messen), keine
Kalibrierung. Der Overfitting-Grundsatz aus CLAUDE.md
("KEINE OPTIMIERUNGSSCHLEIFE") bleibt damit gewahrt.

VORAB FESTGELEGTES BELEG-KRITERIUM (vor dem ersten Lauf, analog
analysis.bot_walkforward.GATE_CRITERIA - das Ergebnis wird nicht
nachtraeglich an eine Erwartung angepasst): ein Gate "verdient" seine
Komplexitaet, wenn sein Entfernen den risikoadjustierten Erwartungswert
MESSBAR verschlechtert (siehe ABLATION_CRITERIA, verdict()). Verschlechtert
sich der Erwartungswert beim Entfernen NICHT messbar, hat das Gate nur
Frequenz gekostet - dieses Modul berichtet das, entscheidet aber nichts
automatisch (kein Gate wird von hier aus im Code entfernt).

WEITERE VERWENDUNG: bleibt als Werkzeug fuer kuenftige Gates - kommt ein
neues Pflichtkriterium hinzu, gehoert es zuerst hier als eigene
`disabled_gates`-Option hinein, bevor irgendetwas an ihm nachjustiert wird."""
from __future__ import annotations

from analysis import bot_backtest, bot_walkforward
from core import bot_signals

# Baseline + je EIN verbleibendes Gate entfernt. Gates 3/4/5/6 sind NICHT
# mehr Teil dieser Liste - sie existieren seit STRATEGY_VERSION
# "2.2-swing4h" nicht mehr im Code (siehe Moduldoc), ein
# `disabled_gates={"gate3"}` waere ein stiller No-op, der wie ein
# Messergebnis aussaehe, aber keins waere. Gate 1 (Regime/ALLOW_SHORT) und
# das ATR-Band sind weiterhin bewusst NICHT Teil der Ablation - beides sind
# keine der nummerierten Pflichtbedingungen, sondern Grundsatzentscheidungen
# (Short-Faehigkeit, Mindestbewegung fuer die Handelskosten), die eine
# Ablationsstudie nicht beilaeufig mit auswuerfeln soll.
VARIANTS: dict[str, frozenset[str]] = {
    "baseline": frozenset(),
    "ohne_gate2_ma_struktur": frozenset({"gate2"}),
    "ohne_gate7_liquiditaet": frozenset({"gate7"}),
}

ABLATION_CRITERIA = {
    # R-Multiple: um wie viel muss die Baseline das Ø R der Variante OHNE
    # dieses Gate mindestens uebertreffen, damit das Gate als "traegt zur
    # Qualitaet bei" gilt.
    "min_avg_r_drop_to_justify_gate": 0.05,
    # Alternativ: relativer Abfall des Profit-Faktors (%), der allein schon
    # genuegt, ein Gate als begruendet einzustufen - EINES der beiden
    # Kriterien reicht (siehe verdict()).
    "min_profit_factor_drop_pct_to_justify_gate": 5.0,
}


def _variant_metrics(result: dict) -> dict:
    """Kompaktes, aus den WALK-FORWARD-Fenstern gepooltes Kennzahlen-Bündel
    einer Variante. Nutzt bot_walkforward._pooled_metrics() direkt (KEIN
    Nachbau der Pool-Rechnung) statt _evaluate_gate()/GATE_CRITERIA - deren
    min_windows/min_trades-Schwellen gehören zur Live-Freigabe-Frage, nicht
    zum reinen Varianten-Vergleich hier; eine Ablation muss auch dann eine
    Zahl liefern, wenn die Datenlage (noch) nicht fürs Live-Gate reicht."""
    if result.get("error"):
        return {"error": result["error"]}
    windows = result["windows"]
    valid = [w for w in windows if "error" not in w]
    pooled = bot_walkforward._pooled_metrics(valid)
    total_days = sum((w["stop"] - w["start"]).days for w in valid)
    return {
        "valid_windows": len(valid),
        "trades": pooled["trades"],
        # Ersatzgröße für "Trades pro handelbarem Takt" (die echte Zahl
        # bräuchte eine zusätzliche Zählung tradeable-aber-nicht-gewählter
        # Kandidaten je Takt, die run_backtest() aktuell nicht separat
        # führt) - Frequenz pro Kalendertag ist aus den vorhandenen Daten
        # direkt ableitbar und für den Ablationsvergleich gleichwertig
        # aussagekräftig (dieselbe Zeitbasis über alle Varianten).
        "trades_per_day": (pooled["trades"] / total_days) if total_days else None,
        "win_rate_pct": pooled["win_rate_pct"],
        "profit_factor": pooled["profit_factor"],
        "avg_r": pooled["avg_r"],
        "max_drawdown_pct": pooled["max_drawdown_pct"],
        "cost_share_of_gross_profit_pct": pooled["cost_share_of_gross_profit_pct"],
    }


def run_ablation(symbols: list[str], window_days: int = bot_walkforward.DEFAULT_WINDOW_DAYS,
                 num_windows: int = bot_walkforward.DEFAULT_NUM_WINDOWS,
                 risk_level: int | None = None, starting_equity_usd: float = 250.0,
                 candles_fn=None, daily_fn=None, funding_fn=None,
                 progress_fn=None) -> dict:
    """Baseline + je eine Ein-Gate-Variante + die Gate-4+5-Kombination
    (VARIANTS), alle über DIESELBEN, einmal geladenen Marktdaten und
    dieselben Walk-Forward-Fenster (bot_walkforward.run_windows_over_market).

    `progress_fn(stage, completed, total, detail)`: stage "daten" während
    des einmaligen Ladens, stage "variante" je fertig gerechneter Variante.
    """
    if num_windows < 1:
        return {"error": "num_windows muss mindestens 1 sein."}

    def _progress(stage: str, completed: int, total: int, detail: str):
        if not callable(progress_fn):
            return
        try:
            progress_fn(stage, completed, total, detail)
        except Exception:
            pass

    total_days = window_days * num_windows + bot_walkforward._BURN_IN_LOOKBACK_DAYS
    market = bot_backtest.load_market_data(
        symbols, days=total_days, candles_fn=candles_fn, daily_fn=daily_fn,
        funding_fn=funding_fn,
        progress_fn=lambda stage, completed, total, detail:
            _progress("daten", completed, total, f"{stage}: {detail}"))
    if not market["candles"]:
        return {"error": "Keine Kursdaten geladen - Ablation nicht möglich.",
                "load_report": market.get("load_report", {})}

    # Derselbe Guard wie bot_walkforward.run_walkforward() - alle Varianten
    # teilen dasselbe (ungeschnittene) btc_daily, ein Ausfall traefe sie
    # gleichzeitig und saehe sonst wie ein (falscher) Gate-Effekt aus.
    _min_daily = bot_signals._REGIME_MIN_CANDLES + 1
    btc_daily = market.get("btc_daily")
    if btc_daily is None or btc_daily.empty or len(btc_daily) < _min_daily:
        got = 0 if btc_daily is None else len(btc_daily)
        return {"error": (f"BTC-Tagesdaten unzureichend für das Marktregime "
                          f"({got} von mindestens {_min_daily} Kerzen) - Ablation "
                          f"nicht möglich für ALLE Varianten (sie teilen dieselben "
                          f"Tagesdaten)."),
               "load_report": market.get("load_report", {})}

    variants = {}
    names = list(VARIANTS.items())
    for idx, (name, disabled) in enumerate(names):
        _progress("variante", idx, len(names), name)
        result = bot_walkforward.run_windows_over_market(
            market, window_days=window_days, num_windows=num_windows,
            risk_level=risk_level, starting_equity_usd=starting_equity_usd,
            disabled_gates=disabled)
        variants[name] = {"disabled_gates": sorted(disabled), **_variant_metrics(result)}
        _progress("variante", idx + 1, len(names), f"{name} fertig")

    return {
        "variants": variants,
        "criteria": ABLATION_CRITERIA,
        "verdict": verdict(variants),
        "risk_level": risk_level,
        "window_days": window_days,
        "num_windows": num_windows,
        "load_report": market.get("load_report", {}),
    }


def verdict(variants: dict) -> dict:
    """Wendet ABLATION_CRITERIA MECHANISCH auf die Varianten an - keine
    nachträgliche Interpretation. Ein Gate "bleibt" (keep_gate=True), wenn
    sein Entfernen den Erwartungswert messbar verschlechtert; sonst hat es
    im gemessenen Zeitraum nur Frequenz gekostet. Trifft keine Entscheidung
    über Gate-Kombinationen (die "ohne_gate4_und_5"-Zeile wird berichtet,
    aber nicht einzeln gegen ABLATION_CRITERIA bewertet, da sie zwei Gates
    gleichzeitig betrifft)."""
    baseline = variants.get("baseline")
    if not baseline or baseline.get("error"):
        return {"error": "Keine gültige Baseline - kein Urteil möglich."}

    out = {}
    for name, v in variants.items():
        if name == "baseline" or v.get("error") or len(v.get("disabled_gates") or []) != 1:
            continue
        base_avg_r, var_avg_r = baseline.get("avg_r"), v.get("avg_r")
        base_pf, var_pf = baseline.get("profit_factor"), v.get("profit_factor")
        avg_r_drop = (base_avg_r - var_avg_r) if None not in (base_avg_r, var_avg_r) else None
        pf_drop_pct = (((base_pf - var_pf) / base_pf * 100.0)
                      if base_pf not in (None, 0) and var_pf is not None else None)

        keep = ((avg_r_drop is not None
                and avg_r_drop >= ABLATION_CRITERIA["min_avg_r_drop_to_justify_gate"])
               or (pf_drop_pct is not None
                   and pf_drop_pct >= ABLATION_CRITERIA["min_profit_factor_drop_pct_to_justify_gate"]))

        out[name] = {
            "gate": v["disabled_gates"][0],
            "trades_baseline": baseline.get("trades"), "trades_without_gate": v.get("trades"),
            "avg_r_drop": avg_r_drop, "profit_factor_drop_pct": pf_drop_pct,
            "keep_gate": keep,
        }
    return out
