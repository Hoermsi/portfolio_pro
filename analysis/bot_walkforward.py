"""Walk-Forward-Test + vorab festgelegtes Live-Freigabe-Kriterium.

WOZU EIGENES MODUL, NICHT NUR EIN LAENGERER analysis.bot_backtest-LAUF: ein
einzelner langer Rücktest mischt Bullen-, Bären- und Seitwärtsphasen zu EINER
Zahl - eine Strategie kann im Durchschnitt profitabel aussehen, obwohl sie
nur in einem einzigen Marktregime funktioniert (siehe die V1-Notiz in
CLAUDE.md: "beide Marktregime verloren"). Mehrere kürzere, unmittelbar
aufeinanderfolgende Fenster zeigen das - UND sie sind der ehrlichere Test
dafür, ob ein eingefrorenes Profil mehrere echte Marktphasen hintereinander
übersteht, ohne dass irgendwo nachjustiert wird.

"Walk-Forward" hier im engeren Sinn NUR bezogen auf die AUSWERTUNG (mehrere
aufeinanderfolgende Out-of-Sample-Fenster desselben, unveränderten Profils) -
NICHT im Sinn einer rollierenden Neukalibrierung. Genau wie
analysis.bot_backtest gilt: KEINE PARAMETERSUCHE. Ein Fenster, das schlecht
abschneidet, ist ein Ergebnis, kein Anlass nachzujustieren.

FENSTER WERDEN VERKETTET, NICHT ISOLIERT GEMESSEN: Fenster N+1 startet mit
der Endsumme, die Fenster N tatsächlich erreicht hat (nicht wieder bei
starting_equity_usd) - nur so ergibt die verkettete Kurve einen durchgehenden
Verlauf, an dem sich ein max. Rückgang MESSEN LÄSST, der auch über eine
Fenstergrenze hinweg auftreten kann. Jedes Fenster bleibt trotzdem eine
eigenständige, echte Out-of-Sample-Auswertung (eigener Burn-in, eigener
Rücktest-Lauf über core.analysis.bot_backtest.run_backtest) - nur die
Start-Equity wird durchgereicht: run_backtest() stellt am ENDE jedes
Fensters alle noch offenen Positionen zum letzten Kurs glatt (siehe dessen
Docstring), Fenster N+1 startet also IMMER ohne offene Positionen, egal was
in Fenster N tatsächlich noch lief. Eine Regel mit langer Haltedauer (z.B.
der Mindestgewinn-Stop, MAX_HOLD_HOURS bis zu 14 Tage) kann dadurch an einer
Fenstergrenze einen Trade "verlieren", der ohne den Schnitt weitergelaufen
wäre und wird tendenziell etwas benachteiligt gegenüber einer Regel mit
kürzerer typischer Haltedauer - nicht behoben (externe Prüfung, 13.09.2026),
weil die Positionszustand-Fortführung über eine Fenstergrenze hinweg ein
eigener, nicht-trivialer Umbau wäre.

BEKANNTE EINSCHRÄNKUNGEN: gelten unveraendert aus jedem einzelnen Fenster-Lauf
(analysis.bot_backtest.run_backtest, siehe dessen Modul-Docstring) - u.a. das
Kandidaten-Universum als einziger, HEUTIGER Schnappschuss statt einer je
Fenster historisch rekonstruierten Auswahl (core.bot_universe). Ein Coin, der
erst kuerzlich die Marktkap.-/Alters-Schwelle gerissen hat, erscheint auch in
weit zurueckliegenden Fenstern als durchgehend qualifiziert.

LIVE-GATE (schriftlich VOR dem ersten Lauf festgelegt, nach dem Vorbild des
Arbitrage-Bots - siehe Nutzer-Memory "Arbitrage Bot: Strategie &
Entscheidungs-Gate"): Freigabe nur, wenn ALLE Kriterien in GATE_CRITERIA
gleichzeitig erfüllt sind. Das Kriterium wird NICHT nachträglich an ein
enttäuschendes Ergebnis angepasst - fällt der Test durch, bleibt der Bot im
Papiermodus, das ist ein zulässiges Ergebnis.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis import bot_backtest
from analysis.bot_backtest import _max_drawdown_pct
from core import bot_config, bot_signals, clock

# Beispiel aus dem Plan: 4 Fenster zu je 45 Tagen (~6 Monate Gesamthistorie).
DEFAULT_WINDOW_DAYS = 45
DEFAULT_NUM_WINDOWS = 4

# Vorlauf VOR dem ersten Fenster fuer Burn-in (core.analysis.bot_backtest.
# BURN_IN_CANDLES, 4h-Kerzen) - grosszuegig in Kalendertagen bemessen (6
# 4h-Kerzen/Tag), damit auch ein paar fehlende/verzoegerte Kerzen die
# Vorlaufzeit nicht unter das Minimum druecken.
_BURN_IN_LOOKBACK_DAYS = 20

# Marktregime-Klassifikation je Fenster, aus dem EIGENEN BTC-Buy&Hold-Ergebnis
# des Fensters (core.analysis.bot_backtest._benchmarks) - unabhaengig vom
# bot-internen Tagesregime-Signal (core.bot_signals.regime), das ohnehin
# NUR long/short/neutral fuer die Signalrichtung kennt, nicht "wie stark".
# +-10% ueber ein 45-Tage-Fenster ist bei Krypto eine deutliche, keine
# knappe Bewegung.
_BULL_THRESHOLD_PCT = 10.0
_BEAR_THRESHOLD_PCT = -10.0

# --- LIVE-FREIGABE-KRITERIUM, EINGEFROREN VOR DEM ERSTEN LAUF ---
GATE_CRITERIA = {
    "min_windows": 3,
    "min_trades": 40,
    "min_profit_factor": 1.25,
    "min_avg_r": 0.0,              # strikt > 0
    "max_drawdown_pct": 20.0,      # strikt < 20 % (Betrag)
    "max_cost_share_of_gross_profit_pct": 40.0,
    "min_nonnegative_regimes": 2,  # von hoechstens 3 (bull/bear/seitwaerts)
}


def _classify_regime(btc_return_pct: float | None) -> str:
    if btc_return_pct is None:
        return "unbekannt"
    if btc_return_pct >= _BULL_THRESHOLD_PCT:
        return "bull"
    if btc_return_pct <= _BEAR_THRESHOLD_PCT:
        return "bär"
    return "seitwärts"


def _window_bounds(end_ts, window_days: int, num_windows: int) -> list[tuple]:
    """`num_windows` aufeinanderfolgende, nicht ueberlappende Fenster,
    chronologisch (aeltestes zuerst), endend bei `end_ts` ("jetzt")."""
    step = pd.Timedelta(days=window_days)
    bounds = []
    for i in range(num_windows):
        start = end_ts - step * (num_windows - i)
        stop = end_ts - step * (num_windows - i - 1)
        bounds.append((start, stop))
    return bounds


def _window_slice(df: pd.DataFrame, window_start, window_end,
                  burn_in_candles: int) -> pd.DataFrame | None:
    """Schneidet GENAU `burn_in_candles` Vorlauf-Kerzen vor `window_start`
    plus alle Kerzen INNERHALB [window_start, window_end) heraus.
    run_backtest() verwirft immer die ERSTEN BURN_IN_CANDLES der
    uebergebenen Reihe als Einschwingzeit - ohne diesen Zuschnitt wuerde es
    entweder Tage vor dem eigentlichen Fenster zu handeln beginnen (bei der
    vollen Historie) oder mangels Vorlauf gar nicht (bei einem Zuschnitt
    exakt auf window_start)."""
    before = df[df.index < window_start]
    within = df[(df.index >= window_start) & (df.index < window_end)]
    if len(before) < burn_in_candles or within.empty:
        return None
    return pd.concat([before.iloc[-burn_in_candles:], within])


def run_walkforward(symbols: list[str], window_days: int = DEFAULT_WINDOW_DAYS,
                    num_windows: int = DEFAULT_NUM_WINDOWS,
                    risk_level: int | None = None,
                    starting_equity_usd: float = 250.0,
                    candles_fn=None, daily_fn=None, funding_fn=None,
                    progress_fn=None,
                    disabled_gates: frozenset[str] = frozenset(),
                    exit_variant: str = "reference") -> dict:
    """Laedt einmal die komplette Historie (num_windows*window_days plus
    Burn-in-Vorlauf), schneidet sie in `num_windows` aufeinanderfolgende
    Fenster und laesst dasselbe, unveraenderte Profil (eine Risikostufe,
    core.bot_config.limits_for_risk_level - Phase 6) auf jedem einzeln
    laufen. `funding`/`btc_daily`/`hourly` werden je Fenster NICHT
    zugeschnitten - beides sind reine Point-in-Time-Nachschlagewerke
    (`.asof()`/Dict-Zugriff je Kerzen-Zeitpunkt), zusaetzliche Daten AUSSERHALB
    eines Fensters koennen darin keine Entscheidung INNERHALB des Fensters
    beeinflussen (kein Lookahead) - nur die 4h-"candles" (die den Kerzen-Takt
    selbst treiben) muessen exakt geschnitten werden, siehe _window_slice().
    """
    if num_windows < 1:
        return {"error": "num_windows muss mindestens 1 sein."}

    def _progress(phase: str, completed: int, total: int, detail: str):
        if not callable(progress_fn):
            return
        try:
            progress_fn(phase, completed, total, detail)
        except Exception:
            # Fortschrittsanzeige ist bewusst nur Beiwerk.
            pass

    total_days = window_days * num_windows + _BURN_IN_LOOKBACK_DAYS
    market = bot_backtest.load_market_data(
        symbols, days=total_days, candles_fn=candles_fn, daily_fn=daily_fn,
        funding_fn=funding_fn,
        progress_fn=lambda stage, completed, total, detail:
            _progress("daten", completed, total, f"{stage}: {detail}"))
    if not market["candles"]:
        return {"error": "Keine Kursdaten geladen - Walk-Forward nicht möglich.",
                "load_report": market.get("load_report", {})}

    # Derselbe Guard wie in bot_backtest.run_backtest() - hier VOR der
    # Fenster-Schleife statt einmal pro Fenster, weil ALLE Fenster dasselbe
    # (ungeschnittene) btc_daily teilen (siehe Docstring oben): ein Ausfall
    # trifft sie gleichzeitig, und ohne diesen Guard liefe jedes einzelne
    # Fenster erst durch run_backtest(), um am Ende alle mit derselben,
    # unspezifischen Fehlermeldung zu scheitern - hier gibt es EINE klare
    # Fehlermeldung statt N verwirrender.
    _min_daily = bot_signals._REGIME_MIN_CANDLES + 1
    btc_daily = market.get("btc_daily")
    if btc_daily is None or btc_daily.empty or len(btc_daily) < _min_daily:
        got = 0 if btc_daily is None else len(btc_daily)
        return {"error": (f"BTC-Tagesdaten unzureichend für das Marktregime "
                          f"({got} von mindestens {_min_daily} Kerzen) - Walk-Forward "
                          f"nicht möglich für ALLE Fenster (sie teilen dieselben "
                          f"Tagesdaten). Ein 0-Trade-Ergebnis wäre hier nicht von einem "
                          f"echten Nullbefund zu unterscheiden."),
               "load_report": market.get("load_report", {})}

    result = run_windows_over_market(
        market, window_days=window_days, num_windows=num_windows, risk_level=risk_level,
        starting_equity_usd=starting_equity_usd, disabled_gates=disabled_gates,
        exit_variant=exit_variant, progress_fn=_progress)
    result["load_report"] = market.get("load_report", {})
    return result


def run_windows_over_market(market: dict, window_days: int, num_windows: int,
                            risk_level: int | None, starting_equity_usd: float,
                            disabled_gates: frozenset[str] = frozenset(),
                            exit_variant: str = "reference",
                            progress_fn=None) -> dict:
    """Der eigentliche Fenster-Zuschnitt + Verkettung ueber ein BEREITS
    GELADENES `market`-Dict (aus bot_backtest.load_market_data()) - ausgelagert
    aus run_walkforward(), damit analysis/bot_ablation.py (Phase 3,
    Gate-Ablation) UND analysis/bot_exit_variants.py (Ausstiegsmechanik-
    Vergleich) die Daten EINMAL laden und danach mehrere Varianten
    (verschiedene `disabled_gates`/`exit_variant`) darueber laufen lassen
    koennen, ohne load_market_data() je Variante erneut aufzurufen -
    derselbe Datenstand fuer alle Varianten ist hier Voraussetzung, kein
    Zufallsprodukt.
    run_walkforward() selbst ruft diese Funktion nach dem eigenen Laden/den
    eigenen Guards auf; ihr Verhalten ist unveraendert, nur der Laden-Teil
    ist jetzt eine separate Funktion."""
    def _progress(phase: str, completed: int, total: int, detail: str):
        if callable(progress_fn):
            try:
                progress_fn(phase, completed, total, detail)
            except Exception:
                pass

    end_ts = max(df.index[-1] for df in market["candles"].values()) + pd.Timedelta(seconds=1)
    bounds = _window_bounds(end_ts, window_days, num_windows)

    windows = []
    equity = starting_equity_usd
    for i, (start, stop) in enumerate(bounds):
        _progress("fenster", i, len(bounds),
                  f"Berechne Fenster {i + 1} von {len(bounds)} …")
        sliced = {}
        for symbol, df in market["candles"].items():
            piece = _window_slice(df, start, stop, bot_backtest.BURN_IN_CANDLES)
            if piece is not None:
                sliced[symbol] = piece
        window_market = {
            "candles": sliced, "funding": market["funding"],
            "missing_funding": market["missing_funding"],
            "btc_daily": market["btc_daily"], "hourly": market.get("hourly", {}),
        }
        result = bot_backtest.run_backtest(
            window_market, risk_level=risk_level, starting_equity_usd=equity,
            disabled_gates=disabled_gates, exit_variant=exit_variant)

        if result.get("error"):
            windows.append({"index": i, "start": start, "stop": stop,
                           "error": result["error"], "regime": "unbekannt"})
            continue

        btc_hold = result["benchmarks"].get("btc_buy_hold_usd")
        btc_return_pct = ((btc_hold / equity - 1) * 100) if btc_hold else None
        windows.append({
            "index": i, "start": start, "stop": stop,
            "regime": _classify_regime(btc_return_pct),
            "btc_return_pct": btc_return_pct,
            "metrics": result["metrics"], "trades": result["trades"],
            "equity_curve": result["equity_curve"],
            "risk_level": result["risk_level"], "limits": result["limits"],
            "blocked_reasons": result.get("blocked_reasons", {}),
        })
        equity = result["metrics"]["end_usd"]
        _progress("fenster", i + 1, len(bounds),
                  f"Fenster {i + 1} von {len(bounds)} berechnet")

    gate = _evaluate_gate(windows)
    return {
        "windows": windows, "gate": gate,
        "starting_equity_usd": starting_equity_usd,
        "ending_equity_usd": equity,
        "overridden_limit_keys": bot_config.overridden_keys(),
    }


def _pooled_metrics(valid_windows: list[dict]) -> dict:
    """ALLE Trades ueber ALLE gueltigen Fenster gepoolt (nicht der
    Durchschnitt der Fenster-Kennzahlen - ein Mittelwert aus Verhaeltnis-
    zahlen wie Profit-Faktor waere statistisch schief, siehe z.B. das
    "Mittelwert von Raten"-Problem)."""
    trades = [t for w in valid_windows for t in w["trades"]]
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] <= 0]
    gross_win = sum(t["net_pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["net_pnl_usd"] for t in losses))
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    costs = sum(w["metrics"]["costs_usd"] for w in valid_windows)
    # Verkettete Kurve: Fenster N+1 setzt bei der Endsumme von Fenster N an
    # (siehe Modul-Docstring) - Concat ergibt damit einen durchgehenden
    # Verlauf, an dem ein Rueckgang ueber eine Fenstergrenze hinweg sichtbar
    # bleibt statt an jeder Grenze "zurueckgesetzt" zu werden.
    chained = pd.concat([w["equity_curve"] for w in valid_windows]) if valid_windows else pd.Series(dtype=float)
    return {
        "trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "max_drawdown_pct": _max_drawdown_pct(chained),
        # ACHTUNG Namensfalle: "gross_win" ist die Summe der bereits NETTO
        # (nach Gebuehren/Funding/Slippage) verbuchten Gewinner-Trades, nicht
        # der Bruttogewinn vor Kosten (den gibt es hier nicht getrennt je
        # Trade). cost_share_of_gross_profit_pct rechnet damit tatsaechlich
        # Kosten / (Nettogewinn, der diese Kosten schon enthaelt) - strenger
        # als der Name suggeriert (nicht optimistisch verzerrt), aber die
        # Beschriftung "des Bruttogewinns" in der UI war schlicht falsch.
        # Feldname bewusst unveraendert (GATE_CRITERIA/Tests referenzieren
        # ihn), die Beschriftung an den Anzeigestellen wurde korrigiert.
        "gross_win_usd": gross_win,
        "costs_usd": costs,
        "cost_share_of_gross_profit_pct": (costs / gross_win * 100) if gross_win > 0 else None,
    }


def _regime_breakdown(valid_windows: list[dict]) -> dict:
    """NUR Fenster mit mindestens einem Trade zaehlen als Regime-Beleg.

    Ohne diesen Filter war das Regime-Kriterium (GATE_CRITERIA
    min_nonnegative_regimes) mit ALLOW_SHORT=False quasi geschenkt: in einem
    Baer-Fenster sagt regime() "short", evaluate() blockt mangels Short-
    Faehigkeit JEDEN Kandidaten, 0 Trades -> end_usd - start_usd == 0 ->
    >= 0 -> das Baer-Regime zaehlte als "nicht negativ", OHNE einen einzigen
    Beleg, dass die Strategie ein fallendes Regime tatsaechlich uebersteht.
    Ein 0-Trade-Fenster ist kein Ergebnis in irgendeine Richtung - es ist
    Abwesenheit von Evidenz und darf weder als positiv noch als negativ
    gewertet werden, also gar nicht in diese Auswertung eingehen."""
    out: dict[str, dict] = {}
    for w in valid_windows:
        if w["metrics"]["trades"] == 0:
            continue
        bucket = out.setdefault(w["regime"], {"windows": 0, "net_pnl_usd": 0.0})
        bucket["windows"] += 1
        bucket["net_pnl_usd"] += w["metrics"]["end_usd"] - w["metrics"]["start_usd"]
    return out


def _evaluate_gate(windows: list[dict]) -> dict:
    """Prueft GATE_CRITERIA gegen das gepoolte Ergebnis. `ok=False` UND
    ein leerer/zu kurzer `reasons`-Zusammenhang ist bewusst kein Sonderfall -
    fehlende Daten sind selbst ein Grund, nicht freizugeben."""
    valid = [w for w in windows if "error" not in w]
    reasons = []
    for w in windows:
        if "error" in w:
            reasons.append(f"Fenster {w['index']} ({w['start']:%d.%m.%Y}-{w['stop']:%d.%m.%Y}): "
                           f"{w['error']}")

    if len(valid) < GATE_CRITERIA["min_windows"]:
        reasons.append(f"Nur {len(valid)} auswertbare Fenster, mindestens "
                       f"{GATE_CRITERIA['min_windows']} nötig.")
        return {"ok": False, "reasons": reasons, "pooled": None, "by_regime": {}}

    pooled = _pooled_metrics(valid)
    by_regime = _regime_breakdown(valid)

    def _fmt(value, suffix=""):
        return "—" if value is None else f"{value:.2f}{suffix}"

    if pooled["trades"] < GATE_CRITERIA["min_trades"]:
        reasons.append(f"Nur {pooled['trades']} Trades insgesamt, mindestens "
                       f"{GATE_CRITERIA['min_trades']} nötig.")
    if pooled["profit_factor"] is None or pooled["profit_factor"] < GATE_CRITERIA["min_profit_factor"]:
        reasons.append(f"Profit-Faktor {_fmt(pooled['profit_factor'])} unter "
                       f"{GATE_CRITERIA['min_profit_factor']}.")
    if pooled["avg_r"] is None or pooled["avg_r"] <= GATE_CRITERIA["min_avg_r"]:
        reasons.append(f"Ø R-Multiple {_fmt(pooled['avg_r'])} nicht über 0.")
    if pooled["max_drawdown_pct"] is None or abs(pooled["max_drawdown_pct"]) >= GATE_CRITERIA["max_drawdown_pct"]:
        reasons.append(f"Max. Rückgang {_fmt(pooled['max_drawdown_pct'], '%')} erreicht/übersteigt "
                       f"{GATE_CRITERIA['max_drawdown_pct']}%.")
    if (pooled["cost_share_of_gross_profit_pct"] is None
           or pooled["cost_share_of_gross_profit_pct"] >= GATE_CRITERIA["max_cost_share_of_gross_profit_pct"]):
        reasons.append(f"Kosten {_fmt(pooled['cost_share_of_gross_profit_pct'], '%')} des Nettogewinns "
                       f"der Gewinner-Trades erreichen/übersteigen "
                       f"{GATE_CRITERIA['max_cost_share_of_gross_profit_pct']}%.")
    nonneg_regimes = sum(1 for b in by_regime.values() if b["net_pnl_usd"] >= 0)
    if nonneg_regimes < GATE_CRITERIA["min_nonnegative_regimes"]:
        reasons.append(f"Nur in {nonneg_regimes} von {len(by_regime)} beobachteten Marktregimen "
                       f"nicht negativ, mindestens {GATE_CRITERIA['min_nonnegative_regimes']} nötig.")

    return {"ok": len(reasons) == 0, "reasons": reasons, "pooled": pooled, "by_regime": by_regime}
