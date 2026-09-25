"""Ausstiegsmechanik-Vergleich (Analyseplan "Trading-Bot ueberdenken",
Abschnitt 5, Schritte 2+3a): misst, ob (a) ein einfacher kostenbereinigter
Mindestgewinn-Stop ab +1R oder (b) ein INNERHALB der laufenden 4h-Kerze
anhand von 1h-Unterkerzen haeufiger nachgezogener Chandelier-Stop die
bestehende Regel aus core.bot_signals.trailing_stop_px() schlaegt -
AUSSCHLIESSLICH OUT-OF-SAMPLE ueber dieselben verketteten Walk-Forward-
Fenster wie analysis.bot_walkforward (nicht ueber einen einzelnen Zeitraum,
aus demselben Grund wie dort: eine Regel kann in einem einzelnen Fenster gut
aussehen, obwohl sie nur dort funktioniert).

WARUM DIESER VERGLEICH: der Live-Bericht (12.09.2026) zeigte bei den
Gewinnern LINK/ETHFI einen grossen Abstand zwischen dem hoechsten
beobachteten Zwischengewinn und dem tatsaechlichen Nettoergebnis - die
Trailing-Regel zieht erst ab +1R nach UND nur einmal je 4h-Takt (Chandelier:
Hoch seit Eroeffnung minus 3 ATR). Zwei unabhaengige Gegenhypothesen:
(2) ein einmaliger, harter Stop auf kostenbereinigten Einstand haette
weniger vom bereits erreichten Gewinn wieder hergegeben; (3a) DIESELBE
Chandelier-Formel, nur oefter nachgezogen (stuendlich statt nur alle 4h),
haette denselben Effekt OHNE die Gewinnmitnahme hart zu deckeln. Dieses
Modul prueft beides, statt es zu behaupten - JEDE Variante einzeln gegen
die unveraenderte Referenz, nie gegeneinander kombiniert (Schritt-Trennung
laut Plan).

EIN DATENABRUF FUER ALLE VARIANTEN: analysis.bot_backtest.load_market_data()
laeuft genau EINMAL, jede Variante laeuft danach ueber
analysis.bot_walkforward.run_windows_over_market() auf demselben,
bereits geladenen Datenstand - siehe analysis/bot_ablation.py's Moduldoc fuer
die ausfuehrliche Begruendung (dieselbe Ueberlegung gilt hier unveraendert).

NUR DIE STOP-FORMEL WIRD JE VARIANTE GETAUSCHT, SONST NICHTS: dieselbe
Aktivierungsschwelle (core.bot_signals.TRAILING_ACTIVATION_R), dieselben
Gates, dieselbe Positionsgroesse, dieselben Risikolimits. Wer die Ursache
eines Unterschieds zuordnen will, darf nur eine Variable auf einmal aendern -
die beiden Varianten werden deshalb NIE gemeinsam aktiviert, nur je einzeln
gegen dieselbe Referenz.

VORAB FESTGELEGTES BELEG-KRITERIUM (vor dem ersten Lauf, analog
analysis.bot_ablation.ABLATION_CRITERIA/analysis.bot_walkforward.
GATE_CRITERIA - das Ergebnis wird nicht nachtraeglich an eine Erwartung
angepasst): eine Variante "gewinnt" gegen die Referenz, wenn sie GEPOOLT
ueber alle gueltigen Fenster den Profit-Faktor oder das Ø R um einen vorher
benannten Betrag verbessert (siehe EXIT_VARIANT_CRITERIA/verdict()).
Verbessert sie sich NICHT messbar, bleibt die Referenz die bessere Wahl -
dieses Modul entscheidet nichts automatisch (kein Code-Pfad wird von hier
aus umgestellt).

KORREKTUR 13.09.2026 (gefunden von einer externen Pruefung): "referenz" rief
bis hierher core.bot_signals.trailing_stop_px() auf - zum Zeitpunkt des
urspruenglichen Vergleichs oben (die damalige Chandelier-Regel, "V2.2") noch
korrekt, aber NACH der Umstellung von trailing_stop_px() auf die
Mindestgewinn-Regel (STRATEGY_VERSION "2.3-swing4h") unbemerkt zur FALSCHEN
Referenz geworden - der gespeicherte "V2.3 schlaegt V2.2"-Beleg verglich
damit V2.3 faktisch mit sich selbst. "referenz" zeigt seither auf
analysis.bot_backtest._chandelier_stop_px_v2_2() - eine von core/
bot_signals.py komplett UNABHAENGIGE, eingefrorene Kopie der damaligen
Chandelier-Regel (siehe deren Docstring). Die fruehere "referenz" (die
LIVE-Funktion) bleibt unter "v2_3_aktuell" erhalten und bedeutet absichtlich
weiterhin "was der Bot GERADE tatsaechlich tut" statt eines benannten Stands
- das bleibt automatisch richtig, auch wenn trailing_stop_px() kuenftig
nochmal wechselt."""
from __future__ import annotations

from analysis import bot_backtest, bot_walkforward
from core import bot_signals

# "referenz" = analysis.bot_backtest._chandelier_stop_px_v2_2() - die
# EINGEFRORENE V2.2-Baseline (siehe Korrektur-Hinweis oben), unabhaengig von
# core/bot_signals.py. "v2_3_aktuell" = core.bot_signals.trailing_stop_px()
# unveraendert (der LIVE-Pfad, aktuell die V2.3-Mindestgewinn-Regel).
# "mindestgewinn_stop"/"stuendliches_nachziehen" = analysis.bot_backtest.
# _min_profit_stop_px()/_hourly_trailing_stop_px() (siehe deren Docstrings -
# existieren NUR dort, kein Live-Code ruft sie auf). Jede Variante laeuft
# EINZELN gegen die Referenz (siehe Moduldoc) - nie gleichzeitig mit einer
# anderen Variante aktiv.
VARIANTS: dict[str, str] = {
    "referenz": "chandelier_v2_2",
    "v2_3_aktuell": "reference",
    "mindestgewinn_stop": "min_profit_stop",
    "stuendliches_nachziehen": "hourly_trailing",
}

EXIT_VARIANT_CRITERIA = {
    # Ø R-Multiple: um wie viel muss eine Variante die Referenz mindestens
    # uebertreffen, damit sie als "besser" gilt.
    "min_avg_r_gain_to_prefer_variant": 0.05,
    # Alternativ: relative Verbesserung des Profit-Faktors (%) - EINES der
    # beiden Kriterien reicht (siehe verdict(), Vorbild bot_ablation.verdict()).
    "min_profit_factor_gain_pct_to_prefer_variant": 5.0,
}


def _variant_metrics(result: dict) -> dict:
    """Wie analysis.bot_ablation._variant_metrics(): gepoolt aus den
    Walk-Forward-Fenstern, NICHT ueber GATE_CRITERIA (dessen
    min_windows/min_trades-Schwellen gehoeren zur Live-Freigabe-Frage, nicht
    zum reinen Varianten-Vergleich)."""
    if result.get("error"):
        return {"error": result["error"]}
    windows = result["windows"]
    valid = [w for w in windows if "error" not in w]
    pooled = bot_walkforward._pooled_metrics(valid)
    total_days = sum((w["stop"] - w["start"]).days for w in valid)
    return {
        "valid_windows": len(valid),
        "trades": pooled["trades"],
        "trades_per_day": (pooled["trades"] / total_days) if total_days else None,
        "win_rate_pct": pooled["win_rate_pct"],
        "profit_factor": pooled["profit_factor"],
        "avg_r": pooled["avg_r"],
        "max_drawdown_pct": pooled["max_drawdown_pct"],
        "cost_share_of_gross_profit_pct": pooled["cost_share_of_gross_profit_pct"],
    }


def run_exit_variant_comparison(symbols: list[str],
                                window_days: int = bot_walkforward.DEFAULT_WINDOW_DAYS,
                                num_windows: int = bot_walkforward.DEFAULT_NUM_WINDOWS,
                                risk_level: int | None = None,
                                starting_equity_usd: float = 250.0,
                                candles_fn=None, daily_fn=None, funding_fn=None,
                                progress_fn=None) -> dict:
    """Referenz + Mindestgewinn-Stop-Variante, beide ueber DIESELBEN, einmal
    geladenen Marktdaten und dieselben Walk-Forward-Fenster
    (bot_walkforward.run_windows_over_market).

    `progress_fn(stage, completed, total, detail)`: stage "daten" waehrend
    des einmaligen Ladens, stage "variante" je fertig gerechneter Variante -
    dasselbe Muster wie analysis.bot_ablation.run_ablation()."""
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
        return {"error": "Keine Kursdaten geladen - Vergleich nicht möglich.",
                "load_report": market.get("load_report", {})}

    # Derselbe Guard wie bot_walkforward.run_walkforward()/bot_ablation.
    # run_ablation() - beide Varianten teilen dasselbe (ungeschnittene)
    # btc_daily, ein Ausfall traefe sie gleichzeitig.
    _min_daily = bot_signals._REGIME_MIN_CANDLES + 1
    btc_daily = market.get("btc_daily")
    if btc_daily is None or btc_daily.empty or len(btc_daily) < _min_daily:
        got = 0 if btc_daily is None else len(btc_daily)
        return {"error": (f"BTC-Tagesdaten unzureichend für das Marktregime "
                          f"({got} von mindestens {_min_daily} Kerzen) - Vergleich "
                          f"nicht möglich für BEIDE Varianten (sie teilen dieselben "
                          f"Tagesdaten)."),
               "load_report": market.get("load_report", {})}

    variants = {}
    names = list(VARIANTS.items())
    for idx, (name, exit_variant) in enumerate(names):
        _progress("variante", idx, len(names), name)
        result = bot_walkforward.run_windows_over_market(
            market, window_days=window_days, num_windows=num_windows,
            risk_level=risk_level, starting_equity_usd=starting_equity_usd,
            exit_variant=exit_variant)
        variants[name] = {"exit_variant": exit_variant, **_variant_metrics(result)}
        _progress("variante", idx + 1, len(names), f"{name} fertig")

    return {
        "variants": variants,
        "criteria": EXIT_VARIANT_CRITERIA,
        "verdict": verdict(variants),
        "risk_level": risk_level,
        "window_days": window_days,
        "num_windows": num_windows,
        "load_report": market.get("load_report", {}),
    }


def _compare_to_reference(reference: dict, variant: dict) -> dict:
    """EIN Varianten-Vergleich gegen die Referenz - EXIT_VARIANT_CRITERIA
    MECHANISCH angewandt, keine nachtraegliche Interpretation."""
    ref_avg_r, var_avg_r = reference.get("avg_r"), variant.get("avg_r")
    ref_pf, var_pf = reference.get("profit_factor"), variant.get("profit_factor")
    avg_r_gain = (var_avg_r - ref_avg_r) if None not in (ref_avg_r, var_avg_r) else None
    pf_gain_pct = (((var_pf - ref_pf) / ref_pf * 100.0)
                  if ref_pf not in (None, 0) and var_pf is not None else None)

    prefer_variant = (
        (avg_r_gain is not None
         and avg_r_gain >= EXIT_VARIANT_CRITERIA["min_avg_r_gain_to_prefer_variant"])
        or (pf_gain_pct is not None
            and pf_gain_pct >= EXIT_VARIANT_CRITERIA["min_profit_factor_gain_pct_to_prefer_variant"]))

    return {
        "trades_referenz": reference.get("trades"), "trades_variante": variant.get("trades"),
        "avg_r_gain": avg_r_gain, "profit_factor_gain_pct": pf_gain_pct,
        "prefer_variant": prefer_variant,
    }


def verdict(variants: dict) -> dict:
    """Vergleicht JEDE Variante (ausser der Referenz selbst) EINZELN gegen
    die Referenz - nie zwei Varianten gegeneinander (siehe Moduldoc: die
    Ursache eines Unterschieds muss zuordenbar bleiben). `prefer_variant`
    steht je Variante; die Referenz bleibt die unveraenderte Wahl fuer jede
    Variante, die ihr Kriterium nicht erreicht."""
    reference = variants.get("referenz")
    if not reference or reference.get("error"):
        return {"error": "Keine gültige Referenz - kein Urteil möglich."}

    out = {}
    for name, v in variants.items():
        if name == "referenz":
            continue
        if v.get("error"):
            out[name] = {"error": v["error"]}
            continue
        out[name] = _compare_to_reference(reference, v)
    return out
