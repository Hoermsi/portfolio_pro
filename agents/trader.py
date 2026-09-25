"""KI-BERICHTERSTATTUNG über den regelbasierten Krypto-Perpetuals-Bot
(Hyperliquid): Post-Mortem abgeschlossener Trades + Regime-Erklärung.

FRÜHERE ROLLEN ENTFALLEN VOLLSTÄNDIG (Phase 8): Früher konnte dieses Modul
eine Position schliessen ('schliessen') oder ein Symbol befristet sperren
('veto'). Von 649 protokollierten KI-Läufen endeten aber nur 1 mit einem
Veto und 7 mit einer Freigabe, der Rest mit "beobachten" - die KI hat diese
Befugnis nachweislich kaum genutzt, aber bei JEDEM Lauf Geld gekostet
(Claude-API). core.bot_config.set_symbol_veto/symbol_vetoed/active_vetoes
und der Veto-Zweig in core.bot.run_signal_cycle sind deshalb ebenfalls
ersatzlos entfallen.

Jetzt schreibt die KI NUR NOCH einen Bericht: was ist bei den zuletzt
geschlossenen Trades passiert (Post-Mortem), und warum steht das
BTC-Tagesregime (core.bot_signals.regime()) gerade so, wie es steht. Sie
kann NICHTS mehr auslösen - core.bot_guards/core.bot_signals bleiben die
einzige Instanz, die über echte Orders entscheidet. Fällt die KI aus (kein
API-Key, Netzfehler, leere Antwort), handelt der Bot unverändert weiter -
er hat sie für keine einzige Entscheidung je gebraucht.
"""
from agents.base import run_json_agent
from agents.dossier import crypto_market_readings, risk_profile_prompt
from analysis import alt_top, cycle, market_timing
from core import bot, bot_config, bot_signals, clock, db
from core.config import CLAUDE_PRICING

_TARGET = "TRADING-BOT"

# Wie viele der zuletzt geschlossenen Trades ueberhaupt als Post-Mortem-
# Kandidaten angeboten werden - genug fuer eine echte Auswahl, ohne den
# Prompt mit Dutzenden Zeilen zu fuellen.
_POST_MORTEM_CANDIDATES = 10


def _closed_trade_candidates(limit: int = _POST_MORTEM_CANDIDATES) -> list[dict]:
    """Die zuletzt geschlossenen Trades als Auswahlgrundlage fuer Post-
    Mortems - jeder mit einer eindeutigen 'ref' (Symbol + Schlusszeit), weil
    dasselbe Symbol mehrfach hintereinander geschlossen werden kann und ein
    Structured-Output-Enum eindeutige Werte braucht, keine Duplikate."""
    positions = db.list_bot_positions(limit * 4)  # Puffer: auch offene Positionen sind dabei
    closed = [p for p in positions if p["status"] == "closed"][:limit]
    return [{**p, "ref": f"{p['symbol']}@{(p['closed_at'] or '')[:16]}"} for p in closed]


def _schema(trade_refs: list[str]) -> dict:
    """Structured-Output-Schema. Kein 'aktion'-Feld mehr - dieselbe
    Narrowing-Idee wie zuvor beim 'symbol'-Enum, nur konsequent zu Ende
    gedacht: was das Modell nicht mehr vorschlagen kann (schliessen, veto),
    muss später auch nicht mehr abgefangen werden."""
    return {
        "type": "object",
        "properties": {
            "marktausblick": {
                "type": "string",
                "description": "2-4 Sätze: aktuelle Krypto-Marktlage UND eine Erklärung, "
                               "warum das BTC-Tagesregime (long/short/neutral) gerade so "
                               "steht, wie es steht",
            },
            "gesamtkommentar": {
                "type": "string",
                "description": "Ehrliche Einschätzung zum bisherigen Verlauf des "
                               "Bot-Depots und zur Arbeit der Regel-Engine",
            },
            "post_mortems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trade": {"type": "string", "enum": list(trade_refs)},
                        "einschaetzung": {
                            "type": "string",
                            "description": "War der Trade im Rahmen des trendfolgenden "
                                           "Setups plausibel? Was, falls überhaupt etwas, "
                                           "war am Ausgang lehrreich? Rein deskriptiv - "
                                           "keine Handlungsempfehlung, keine Spekulation "
                                           "über künftige Trades.",
                        },
                    },
                    "required": ["trade", "einschaetzung"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["marktausblick", "gesamtkommentar", "post_mortems"],
        "additionalProperties": False,
    }


def _system_prompt(limits: dict, risk: int) -> str:
    return (
        "Du bist die Berichterstattung über einen regelbasierten, "
        "TRENDFOLGENDEN Handelsbot auf einem ECHTEN, bewusst kleinen "
        "Krypto-Perpetuals-Konto (Hyperliquid). Das ist kein virtuelles "
        "Testdepot - jede ausgeführte Order bewegt echtes Geld.\n"
        "WICHTIG ZU DEINER ROLLE: Du triffst KEINE Entscheidung. Eine "
        "deterministische Signal-Engine eröffnet und schliesst Positionen "
        "vollständig selbständig; core.bot_guards prüft jede Order "
        "unabhängig von dir. Deine Aufgabe ist rein beschreibend:\n"
        "1. 'marktausblick': erkläre die aktuelle Marktlage UND warum das "
        "Tagesregime gerade so steht, wie es steht - für jemanden, der die "
        "Rohdaten nicht selbst liest.\n"
        "2. 'gesamtkommentar': eine ehrliche Einschätzung zum bisherigen "
        "Verlauf des Bot-Depots.\n"
        "3. 'post_mortems': wähle die 2-5 LEHRREICHSTEN der zuletzt "
        "geschlossenen Trades aus (grosse Gewinner, grosse Verlierer, oder "
        "überraschende Ausgänge) und ordne jeden ehrlich ein. Ein hoher RSI "
        "beim Einstieg war KEIN Fehler - in einer Trendfolge-Strategie "
        "laufen starke Trends lange überkauft. Wenn ein Fenster kein "
        "lehrreiches Muster zeigt, gib wenige oder gar keine Post-Mortems "
        "zurück.\n"
        f"Risikostufe des Nutzers: {risk}/10. Dies ist keine Anlageberatung, "
        "sondern Berichterstattung über ein vom Nutzer bewusst klein "
        "gehaltenes Experiment-Konto."
    )


def _market_block() -> str:
    readings, coinbase_rank = crypto_market_readings()
    temp = market_timing.market_temperature(readings, market="crypto")
    cyc = cycle.cycle_score()
    alt = alt_top.alt_top_score()

    lines = []
    if temp.get("score") is not None:
        lines.append(f"Markt-Temperatur: {temp['classification']} ({temp['score']:.0f}/100)")
    else:
        lines.append("Markt-Temperatur: nicht verfügbar")
    if cyc.get("score") is not None:
        lines.append(f"BTC-Zyklus: {cyc['regime']} (Score {cyc['score']:.0f}/100)")
    else:
        lines.append("BTC-Zyklus: nicht verfügbar")
    if alt.get("score") is not None:
        alarm = " ⚠️ ALARM" if alt.get("alarm") else ""
        lines.append(f"Altcoin-Überhitzung: {alt['score']:.0f}/100{alarm} (Hinweis: dieses "
                     "Signal hat laut Backtest KEINEN belegten Vorhersage-Edge - nur als "
                     "grober Kontext, keine Timing-Grundlage)")
    if coinbase_rank:
        lines.append(f"Coinbase-App-Rang (Retail-Hype-Indikator): Platz {coinbase_rank}")
    return "\n".join(lines)


def _regime_block(btc_regime: str) -> str:
    return f"Aktuelles BTC-Tagesregime (core.bot_signals.regime): {btc_regime}."


def _positions_block(exchange) -> str:
    state = exchange.account_state()
    if not state.positions:
        return f"Keine offenen Positionen. Equity: {state.equity_usd:,.2f} $."
    lines = [f"OFFENE POSITIONEN (Gesamt-Equity: {state.equity_usd:,.2f} $):"]
    for p in state.positions:
        db_pos = db.get_bot_position(p.symbol)
        stop_txt = f", Stop {db_pos['stop_px']:g} $" if db_pos and db_pos.get("stop_px") else ""
        opened_txt = f", offen seit {db_pos['opened_at'][:16]}" if db_pos else ""
        funding = exchange.funding_rate_hourly(p.symbol)
        funding_txt = f", Funding {funding * 100:+.4f}%/h" if funding is not None else ""
        lines.append(f"- {p.symbol} ({p.side}, {p.leverage:g}x): Größe {p.size:.6g}, "
                     f"Einstieg {p.entry_px:,.2f} $, uPnL {p.unrealized_pnl_usd:+,.2f} $"
                     f"{stop_txt}{opened_txt}{funding_txt}")
    return "\n".join(lines)


def _signals_block(signals: list) -> str:
    """Was die Regel-Engine GERADE sieht - reiner Kontext für die
    Regime-Erklärung, keine Handlungsgrundlage mehr (siehe Modul-Docstring:
    die KI kann nichts davon auslösen).

    Seit der V2-Signal-Engine gibt es KEINE Punktzahl-Schwelle mehr - ob ein
    Symbol handelbar ist, entscheiden Pflichtbedingungen (core.bot_signals.
    evaluate(), `Signal.tradeable`), der Score ist nur noch die Rangfolge
    unter bereits qualifizierten Kandidaten (siehe dessen Modul-Docstring).
    """
    lines = ["Score ist nur die Rangfolge unter bereits qualifizierten Kandidaten - "
             "ob ein Symbol ÜBERHAUPT handelbar ist, entscheiden Pflichtbedingungen "
             "(Tagesregime, Trend-Effizienz, Momentum, relative Stärke zu BTC, "
             "Volumen, Liquidität), nicht die Punktzahl."]
    for s in signals:
        row = f"- {s.symbol}: {s.side or '—'} {s.score:.0f}/100"
        if s.price:
            row += f", Kurs {s.price:,.2f} $"
        if s.atr_pct is not None:
            row += f", ATR {s.atr_pct:.2f} %"
        if s.funding_hourly is not None:
            row += f", Funding {s.funding_hourly * 100:+.4f} %/h"
        if s.blocked_by:
            row += f" — NICHT handelbar: {s.blocked_by}"
        elif s.tradeable:
            row += " — handelbar"
        if s.reasons:
            row += f" | {'; '.join(s.reasons[:3])}"
        lines.append(row)
    return "\n".join(lines)


def _activity_block(exchange) -> str:
    """Wie aktiv der Bot tatsächlich war - macht Untätigkeit zu einer sichtbaren
    Zahl statt zu einem stillen Zustand."""
    lines = []
    positions = db.list_bot_positions(200)
    closed = [p for p in positions if p["status"] == "closed"]
    open_now = [p for p in positions if p["status"] == "open"]
    lines.append(f"Abgeschlossene Trades bisher: {len(closed)}, aktuell offen: {len(open_now)}.")

    last_ts = max((p["closed_at"] or p["opened_at"] for p in positions), default=None)
    last_dt = clock.parse_utc(last_ts)
    if last_dt is not None:
        hours = (clock.now_utc() - last_dt).total_seconds() / 3600
        lines.append(f"Letzte Handelsaktivität vor {hours:.0f} Stunden.")
    else:
        info = bot.start_info() or {}
        lines.append(f"Noch KEIN einziger Trade seit dem Start am {info.get('date', '?')}.")

    summary = bot.performance_summary()
    if summary:
        lines.append(f"Netto seit Start: {summary['net_pnl_usd']:+,.2f} $ "
                     f"({summary['net_return_pct']:+.2f} %), erfasste Kosten "
                     f"{summary['costs_usd']:,.3f} $, Laufzeit {summary['days_running']} Tage.")
    return "\n".join(lines)


def _history_block(closed_trades: list[dict]) -> str:
    """Post-Mortem-Kandidaten mit ihrer eindeutigen 'ref' - genau die Werte,
    die 'post_mortems[].trade' im Schema auswählen kann."""
    if not closed_trades:
        return "Noch keine geschlossenen Trades."
    lines = []
    for p in closed_trades:
        pnl = p["realized_pnl_usd"] or 0.0
        notional = p["entry_px"] * p["size"]
        pct = (pnl / notional * 100) if notional else 0.0
        lines.append(f"[{p['ref']}] {p['symbol']} {p['side']} @ {p['entry_px']:,.2f} $ -> "
                    f"{p['close_px']:,.2f} $ = {pnl:+,.2f} $ ({pct:+.1f}%)")
    return "\n".join(lines)


def build_prompt(exchange, signals: list, btc_regime: str, closed_trades: list[dict]) -> str:
    return (
        "== MARKTLAGE (Krypto gesamt) ==\n" + _market_block() + "\n" + _regime_block(btc_regime) + "\n\n"
        "== DEIN BOT-DEPOT ==\n" + _positions_block(exchange) + "\n\n"
        "== WAS DIE REGEL-ENGINE GERADE SIEHT ==\n"
        + _signals_block(signals) + "\n\n"
        "== HANDELSAKTIVITÄT & ERGEBNIS ==\n" + _activity_block(exchange) + "\n\n"
        "== POST-MORTEM-KANDIDATEN (zuletzt geschlossene Trades) ==\n"
        + _history_block(closed_trades) + "\n\n"
        "== RISIKOPROFIL DES NUTZERS ==\n" + risk_profile_prompt() + "\n\n"
        "Schreibe jetzt deinen Bericht."
    )


def estimate_cost(model: str) -> float:
    p_in, p_out = CLAUDE_PRICING.get(model, (5.0, 25.0))
    return 8000 / 1e6 * p_in + 1500 / 1e6 * p_out


def run_trading_cycle(exchange, model: str, progress_cb=None, candles_fn=None) -> dict:
    """Holt den KI-Bericht zum aktuellen Zustand. Läuft gegen jede
    data.hyperliquid.ExchangeProtocol-Implementierung, verändert aber NIE
    deren Zustand - core.bot_guards/core.bot_signals entscheiden dort,
    nicht hier (siehe Modul-Docstring)."""
    def progress(msg):
        if progress_cb:
            progress_cb(msg)

    if not bot.is_active():
        return {"error": "Bot ist noch nicht initialisiert (core.bot.initialize())."}

    progress("Analysiere Marktlage, Depot & Engine-Signale ...")
    limits = bot_config.bot_limits()
    candidates = bot.candidate_symbols()
    signals = bot_signals.scan(exchange, candidates, limits, candles_fn=candles_fn)
    btc_regime = bot._current_btc_regime()
    closed_trades = _closed_trade_candidates()
    prompt = build_prompt(exchange, signals, btc_regime, closed_trades)

    progress("KI schreibt den Bericht ...")
    trade_refs = [t["ref"] for t in closed_trades]
    parsed, usage, err = run_json_agent(_system_prompt(limits, bot_config.risk_level()),
                                        prompt, model, _schema(trade_refs))
    cost = (usage or {}).get("cost_usd", 0.0)
    if err or parsed is None:
        error_result = {"error": err or "Keine verwertbare Antwort.", "usage": usage or {},
                        "total_cost_usd": cost}
        if cost > 0:
            # Anthropic hat trotz Fehler (z.B. max_tokens) bereits abgerechnet -
            # das muss in agent_runs sichtbar bleiben (Vorbild: strategist.run_strategy).
            db.log_agent_run(target=_TARGET, mode="trading", total_score=None,
                             recommendation="Fehlgeschlagen", cost_usd=cost, report=error_result)
        return error_result

    post_mortems = parsed.get("post_mortems", []) or []
    result = {
        "mode": "trading",
        "marktausblick": parsed.get("marktausblick", ""),
        "gesamtkommentar": parsed.get("gesamtkommentar", ""),
        "post_mortems": post_mortems,
        "signals": [s.as_row() for s in signals],
        "usage": usage,
        "total_cost_usd": cost,
    }
    run_id = db.log_agent_run(target=_TARGET, mode="trading", total_score=None,
                              recommendation=f"{len(post_mortems)} Post-Mortems",
                              cost_usd=cost, report=result)
    result["run_id"] = run_id
    return result
