"""Kontrollzentrum für den Hyperliquid-Trading-Bot.

Diese Seite sendet selbst NIE eine Order - das kann ausschließlich der
separate Runner-Prozess (core/bot_process.py startet/stoppt ihn, sendet aber
selbst keine). Auch "Alle Positionen schließen" läuft nur über ein Meta-Flag
(bot_close_all_requested), das der Runner beim nächsten Takt liest -
dasselbe Muster wie der Stop-Wunsch (bot_runner_stop_requested): nur der
Runner-Prozess hält die aktive Börsen-/Paper-Verbindung, ein hier frisch
gebautes Exchange-Objekt kennt offene Paper-Positionen gar nicht. Die UI
liest seinen Audit-Trail, setzt Sicherheitsbremsen und macht die
Netto-Performance samt Vergleich transparent.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from core import bot, bot_config, bot_process, bot_universe, clock, db
from data import crypto_history
from ui import components

_HEARTBEAT_MAX_MINUTES = 35
_ORDERS_VISIBLE = 7
_DECISIONS_VISIBLE = 7
_TRADES_VISIBLE = 7

_GREEN = "color: #23c55e; font-weight: 600"
_RED = "color: #ff4b4b; font-weight: 600"


_INTENT_LABEL = {"open_long": "🟢 Long eröffnet", "open_short": "🔴 Short eröffnet",
                 "close": "⚪ Geschlossen",
                 "exchange_reconciled_close": "↔️ Bestandsabgleich (Börse)"}

_ACTION_LABEL = {
    "stop_triggered": "🛑 Stop ausgelöst", "equity_floor": "🛑 Equity-Boden erreicht",
    "90_day_abort": "🛑 90-Tage-Abbruch", "reconciled_close": "↔️ Bestandsabgleich",
    "unknown_exchange_position": "⚠️ Unbekannte Börsen-Position",
    "position_adopted": "↔️ Position automatisch übernommen",
    "position_adopt_failed": "⚠️ Übernahme fehlgeschlagen",
    "stop_close_failed": "⚠️ Stop-Schliessung fehlgeschlagen", "kill_switch": "🛑 Kill-Switch",
    "signal_entry_long": "🟢 Signal: Long eröffnet",
    "signal_entry_short": "🔴 Signal: Short eröffnet",
    "signal_exit": "⚪ Signal: Ausstieg", "signal_scan": "🔍 Signal-Takt (kein Einstieg)",
    "trailing_stop_exchange_failed": "⚠️ Nachgezogener Stop nicht auf der Börse übernommen",
    "signal_cycle_failed": "⚠️ Signal-Zyklus fehlgeschlagen",
    "manual_close_all": "🚪 Manuell alle Positionen geschlossen",
    "manual_close_single": "🚪 Manuell geschlossen",
    "manual_stop_update": "🎯 Stop manuell nachgezogen",
    "manual_stop_failed": "⚠️ Manueller Stop nicht übernommen",
    "existing_position_limits_exceeded": "⚠️ Bestehende Position(en) über aktuellem Limit",
    "reconciled_side_mismatch": "🛑 Richtungswechsel außerhalb des Bots",
    "reconciled_size_adjusted": "↔️ Größe an Börse angeglichen",
    "reconciled_entry_px_drift": "ℹ️ Einstandskurs weicht von der Börse ab",
}


def render():
    components.page_header(
        "Analysen", "Trading-Bot",
        "Regelbasierter Krypto-Positionsmanager · keine Anlageberatung · "
        "Zahlen netto nach Gebühren und Funding.")

    # Runner-Status wird JEDEM Zweig gebraucht (auch vor der Erstinitialisierung),
    # deshalb hier statt in einem der Tabs ermittelt.
    state = _runner_state()
    summary = bot.performance_summary() if bot.is_active() else None

    if not bot.is_active():
        # Vor der Erstinitialisierung gibt es noch kein "Bot-Aktivität"/
        # "Strategie-Tests"-Paar, zwischen das die Steuerung sonst gehört -
        # sie steht deshalb hier weiterhin offen ueber den beiden Start-Tabs,
        # Runner-Stop/Kill-Switch bleiben so auch in diesem Zustand direkt
        # erreichbar.
        with st.container(border=True):
            st.markdown("#### Bot-Steuerung")
            st.caption("Status prüfen, Runner schalten oder den Handel sofort sperren.")
            _render_status(state, summary)
            _render_action_row(state)
            _render_kill_switch()
        tab_start, tab_settings = st.tabs(["🚀 Erster Start", "⚙️ Risikoeinstellungen"])
        with tab_start:
            _render_not_initialized()
        with tab_settings:
            st.caption("Dieses Risikoprofil gilt ab dem ersten Runner-Zyklus.")
            _render_limits()
        return

    tab_overview, tab_activity, tab_control, tab_tests, tab_settings = st.tabs([
        "📊 Überblick", "🔎 Bot-Aktivität", "🛡️ Bot-Steuerung", "🧪 Strategie-Tests",
        "⚙️ Einstellungen",
    ])

    with tab_overview:
        st.caption("Kontostand, offene Positionen und aktuelle Risikoauslastung auf einen Blick.")
        _render_performance(summary)
        st.divider()
        _render_positions(state)
        if summary:
            st.divider()
            _render_risk_bars(summary)
        with st.expander("🧠 Letzten KI-Bericht anzeigen"):
            _render_ai_insight(show_heading=False)

    with tab_activity:
        st.caption("Nachvollziehen, was der Bot zuletzt geprüft, entschieden und ausgeführt hat.")
        activity_scan, activity_pool, activity_trades, activity_log = st.tabs([
            "Letzter Signal-Check", "Kandidaten", "Trades", "Protokoll",
        ])
        with activity_scan:
            _render_signal_scan()
        with activity_pool:
            _render_candidate_pool(state)
        with activity_trades:
            _render_trade_history()
        with activity_log:
            _render_audit_log()

    with tab_control:
        st.caption("Status prüfen, Runner schalten oder den Handel sofort sperren.")
        _render_status(state, summary)
        _render_action_row(state)
        _render_kill_switch()

    with tab_tests:
        st.caption("Regeln mit historischen Daten prüfen. Diese Tests senden keine Orders.")
        _render_backtest()

    with tab_settings:
        st.caption("Risikobereitschaft und Experten-Limits für künftige Bot-Entscheidungen.")
        _render_ai_toggle()
        st.divider()
        _render_limits()
        st.divider()
        _render_reset(state)


def _runner_state() -> dict:
    """Runner-Zustand aus zwei Signalen statt nur dem Heartbeat.

    Der Heartbeat allein reicht fuer einen Umschalt-Button nicht: er
    erscheint erst, nachdem bot_runner.py die Boersenanbindung aufgebaut hat
    (bot_runner.py:_heartbeat, nach _build_exchange()) - in den paar Sekunden
    davor wuerde "Runner starten" nach einem Klick unveraendert dastehen.
    core.bot_process.start() speichert die PID darum sofort selbst; is_running()
    (tasklist) schliesst diese Luecke, wird aber nur bei nicht-frischem
    Heartbeat ueberhaupt aufgerufen, um nicht bei jedem Rerun einen Prozess-
    Check zu erzwingen."""
    raw = db.get_meta("bot_runner_heartbeat")
    mode = db.get_meta("bot_runner_mode")
    heartbeat_fresh = False
    last_seen = None
    if raw:
        try:
            age_minutes = (clock.now_utc() - clock.parse_utc(raw)).total_seconds() / 60
            heartbeat_fresh = 0 <= age_minutes <= _HEARTBEAT_MAX_MINUTES
            last_seen = raw
        except ValueError:
            last_seen = None
    starting = False if heartbeat_fresh else bot_process.is_running()
    return {
        "running": heartbeat_fresh or starting,
        "heartbeat_fresh": heartbeat_fresh,
        "starting": starting,
        "stop_pending": db.get_meta("bot_runner_stop_requested") == "1",
        "last_seen": last_seen,
        "mode": mode,
    }


def _position_side_label(side: str) -> str:
    return "🟢 LONG" if side == "long" else "🔴 SHORT"


def _next_decision_window() -> datetime:
    """Naechster moegliche 4h-Entscheidungszeitpunkt, rein fuer die Anzeige -
    core.bot._due_for_decision() prueft live gegen einen neu geschlossenen
    4h-BTC-Kerzenstand, keine feste Uhrzeit. Ausgangspunkt ist der zuletzt
    tatsaechlich verarbeitete Kerzenstand (bot_last_decision_candle) + 4h,
    das ist exakt die naechste Kerze in DEMSELBEN Raster wie die Boerse.
    Fehlt der Marker (frischer Reset) oder liegt er bereits in der
    Vergangenheit (Runner stand laengere Zeit still), faellt die Funktion auf
    das naechste UTC-4h-Rastermass (00/04/08/12/16/20 Uhr) ab jetzt zurueck -
    dieselbe Ausrichtung, mit der Hyperliquid seine 4h-Kerzen fuehrt."""
    now = clock.now_utc()
    last = clock.parse_utc(db.get_meta("bot_last_decision_candle"))
    if last is not None:
        candidate = last + timedelta(hours=4)
        if candidate > now:
            return candidate
    grid_hour = (now.hour // 4 + 1) * 4
    extra_days, hour_of_day = divmod(grid_hour, 24)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=extra_days, hours=hour_of_day)


def _format_countdown(delta: timedelta) -> str:
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _render_status(state: dict, summary: dict | None = None):
    switch = bot_config.kill_switch_info()
    mode = "Papier" if bot_config.dry_run_enabled() else "Live"
    if state["mode"]:
        # Der Modus gehoert zur LAUFENDEN Exchange-Instanz (bot_runner.py setzt
        # ihn beim Start). Eine spaetere Meta-Aenderung darf einen noch
        # laufenden Papier-Prozess nicht als Live-Prozess ausweisen.
        mode = "Papier" if state["mode"] == "paper" else "Live"

    if state["heartbeat_fresh"]:
        runner_label = "Aktiv"
    elif state["starting"]:
        runner_label = "Startet …"
    else:
        runner_label = "Gestoppt"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runner", runner_label,
              clock.local_str(state["last_seen"]) if state["last_seen"] else None,
              delta_color="off",
              help="Der eigenständige Prozess, der Kurse prüft und Orders verwaltet.")
    c2.metric("Handelsmodus", mode,
              help="Papier simuliert Orders; Live sendet echte Orders an Hyperliquid.")
    c3.metric("Kill-Switch", "Aktiv" if switch["active"] else "Bereit",
              help="Ein aktiver Kill-Switch sperrt alle neuen Einstiege.")
    c4.metric("Orders heute", str(bot.trades_today()),
              help="Heute tatsächlich gefüllte Eröffnungen und Schließungen.")

    if bot.is_active():
        next_decision = _next_decision_window()
        remaining = next_decision - clock.now_utc()
        if remaining.total_seconds() > 60:
            st.caption(f"⏱️ Nächste Entscheidungsprüfung in {_format_countdown(remaining)} "
                       f"(4h-Kerzenschluss ca. {clock.to_local(next_decision).strftime('%H:%M')} Uhr "
                       f"lokal, plus bis zu 15 Min. bis zum nächsten Runner-Takt) - "
                       f"nur eine Schätzung anhand des UTC-4h-Rasters, kein Live-Wert.")
        else:
            st.caption("⏱️ Neues 4h-Entscheidungsfenster steht unmittelbar bevor - "
                       "wird beim nächsten Runner-Takt geprüft.")

    if bot_config.runner_degraded():
        last_ok = bot_config.last_successful_cycle_at()
        st.error(f"⚠️ {bot_config.consecutive_cycle_failures()} Zyklen in Folge fehlgeschlagen - "
                 f"neue Einstiege sind pausiert (bestehende Positionen/Stops laufen weiter). "
                 f"Letzter erfolgreicher Zyklus: "
                 f"{clock.local_str(last_ok) if last_ok else 'nie'}.")

    last_error = db.get_meta("bot_runner_last_error")
    if last_error and not state["running"]:
        error_at = db.get_meta("bot_runner_last_error_at")
        st.error(f"Letzter Runner-Abbruch"
                 f"{' (' + clock.local_str(error_at) + ')' if error_at else ''}: "
                 + _md_escape(last_error))
        # Der Anker-Konflikt ist der haeufigste Abbruchgrund - die Loesung
        # gehoert deshalb direkt unter die Fehlermeldung, nicht in ein Menue.
        if bot.is_active() and bot.anchor_mismatch():
            st.caption(f"Start-Anker gehört zu '{(bot.start_info() or {}).get('exchange_kind')}', "
                       f"eingestellt ist '{bot.current_mode()}'.")
            if st.button("Start-Anker auf aktuellen Modus setzen", key="bot_realign_anchor"):
                bot.realign_anchor_to_current_mode()
                db.delete_meta("bot_runner_last_error")
                db.delete_meta("bot_runner_last_error_at")
                st.success(f"Anker auf '{bot.current_mode()}' gesetzt.")
                st.rerun()

    if not bot_config.live_trading_allowed():
        st.warning("Demo-Modus: Jede Bot-Order wird zusätzlich im Code blockiert.")
    elif not bot_config.dry_run_enabled():
        st.error("🔴 LIVE-HANDEL AKTIV — der Runner sendet echte Orders mit echtem Geld.")

    if db.get_meta("bot_equity_untrusted_at"):
        st.warning("⚠️ Letzte Equity-Messung nicht vertrauenswürdig - keine neuen Einstiege, "
                   "bestehende Positionen laufen unberührt weiter.")

    # Machbarkeit der AKTUELL WIRKSAMEN Konfiguration (nicht nur einer
    # Regler-Vorschau) gehört hierher, nicht nur als Hinweis unterhalb des
    # Risikoreglers (Phase 2, Kapital-Realität): ein Konto, das bei der
    # gerade aktiven Stufe/den aktiven Overrides keine einzige Order über
    # der Hyperliquid-Mindestgröße produzieren kann, handelt still gar
    # nicht - das muss im Status sofort auffallen, nicht erst beim
    # Aufklappen des Risikoradlers unten auf der Seite.
    active_equity = (summary or {}).get("current_usd")
    if active_equity:
        active_check = bot_config.feasibility(active_equity)   # limits=None -> bot_limits()
        if not active_check["ok"]:
            st.error("🚫 " + _md_escape(active_check["hint"]))


def _render_action_row(state: dict):
    """Die drei Schalt-Aktionen des Bots in einer Zeile: Echtgeld-Freigabe
    (öffnet einen Dialog), Runner starten/stoppen (ein Button, der seinen
    Zustand wechselt statt zwei getrennte Buttons vorzuhalten) und
    Kill-Switch (löst sofort aus / öffnet den Reset-Dialog)."""
    col_runner, col_live, col_kill = st.columns(3)

    with col_runner:
        if state["stop_pending"]:
            st.button("⏳ Runner stoppt …", disabled=True, key="bot_process_toggle",
                      width="stretch")
            if st.button("Runner hart beenden", key="bot_force_kill",
                         help="Nur verwenden, wenn der normale Stopp hängen bleibt."):
                ok, msg = bot_process.force_kill()
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        elif state["running"]:
            if st.button("⏹️ Runner stoppen", key="bot_process_toggle", width="stretch",
                         help="Beendet den Bot-Prozess kontrolliert; offene Positionen bleiben bestehen."):
                st.info(bot_process.request_stop())
                st.rerun()
        else:
            if st.button("▶️ Runner starten", type="primary", key="bot_process_toggle",
                         width="stretch",
                         help="Startet den eigenständigen Bot-Prozess im angezeigten Handelsmodus."):
                ok, msg = bot_process.start()
                (st.success if ok else st.error)(msg)
                if ok:
                    # Kurz warten, damit der frisch gestartete Prozess beim
                    # folgenden Rerun bereits als laufend erkannt wird.
                    time.sleep(1.0)
                    st.rerun()

    with col_live:
        is_live = not bot_config.dry_run_enabled()
        label = "🔴 Live-Modus verwalten" if is_live else "🔓 Live-Freigabe"
        if st.button(label, key="bot_open_live_gate", width="stretch",
                     help="Prüft die Verbindung und verwaltet den Wechsel zwischen Papier- und Live-Handel."):
            # Altes Vorpruefungs-Ergebnis verwerfen: der Dialog darf nie eine
            # Verbindung als "geprueft" ausweisen, die zu einem frueheren
            # Oeffnen gehoerte.
            st.session_state.pop("bot_preflight", None)
            _live_gate_dialog(state)

    with col_kill:
        with st.container(key="bot_kill_action"):
            switch = bot_config.kill_switch_info()
            if switch["active"]:
                if st.button("🛑 Kill-Switch lösen", key="bot_kill_toggle", width="stretch",
                             help="Erst nach Prüfung der Ursache wieder freigeben."):
                    _kill_switch_dialog()
            else:
                # Ausloesen passiert SOFORT (kein Dialog): im Ernstfall zaehlt
                # jede Sekunde. Nur das Zuruecksetzen braucht eine Bestaetigung.
                if st.button("🛑 Kill-Switch", key="bot_kill_toggle", width="stretch",
                             help="Sperrt sofort alle neuen Einstiege, bis du ihn bewusst löst."):
                    bot_config.trip_kill_switch("Manuell über Trading-Bot-Oberfläche ausgelöst")
                    st.rerun()


@st.dialog("Kill-Switch lösen")
def _kill_switch_dialog():
    switch = bot_config.kill_switch_info()
    reason = _md_escape(switch["reason"]) if switch["reason"] else "kein Grund protokolliert"
    st.write(f"Aktiv seit {switch['at'] or 'unbekannt'}: {reason}")
    st.caption("Neue Einstiege bleiben bis dahin gesperrt; offene Positionen bleiben unangetastet.")
    confirm = st.checkbox("Ich habe Ursache und offene Positionen geprüft.",
                          key="bot_reset_kill_confirm")
    if st.button("Kill-Switch zurücksetzen", type="primary", disabled=not confirm,
                 key="bot_reset_kill"):
        bot_config.reset_kill_switch()
        st.rerun()


def _render_kill_switch():
    switch = bot_config.kill_switch_info()
    if not switch["active"]:
        return
    reason = _md_escape(switch["reason"]) if switch["reason"] else "kein Grund protokolliert"
    st.error(f"🛑 Kill-Switch aktiv seit {switch['at'] or 'unbekannt'}: {reason}")


def _render_not_initialized():
    st.info("Bot noch nicht initialisiert. Runner oben starten (▶️) oder "
            "**Start Trading Bot.bat** ausführen – legt automatisch ein Papierdepot "
            "mit 250 $ an.")


@st.dialog("Echtgeld-Freigabe", width="large")
def _live_gate_dialog(state: dict):
    """Ersetzt die frühere Aufklapper-Kaskade (Vorprüfung + Live-Schalter +
    optionaler Scharfschuss) durch einen Dialog. Das Vorprüfungs-Ergebnis
    landet in session_state statt einer lokalen Variable, weil jeder Klick
    im Dialog einen eigenen Rerun auslöst."""
    if not bot_config.dry_run_enabled():
        st.error("🔴 Live-Handel ist aktiv.")
        if st.button("Zurück in den Dry-Run (Papierhandel)", type="primary", key="bot_live_off",
                     disabled=state["running"]):
            bot_config.set_dry_run(True)
            st.rerun()
        if state["running"]:
            st.caption("Runner läuft noch mit der aktiven Live-Verbindung – erst stoppen, dann "
                       "zurück in den Dry-Run wechseln. Der Schalter wirkt erst nach einem "
                       "Neustart des Runners.")
        with st.expander("Optional: separaten Echtgeld-Scharfschuss durchführen"):
            st.code("python phase_f_shot.py --confirm BTC-LONG-12USD-1X-STOP1PCT",
                    language="powershell")
            if bot_config.phase_f_is_fresh():
                st.success("Zuletzt erfolgreich: "
                           f"{clock.local_str(bot_config.phase_f_verified_at())}")
        return

    st.markdown("#### Read-only-Vorprüfung")
    if st.button("Verbindung prüfen (read-only)", key="bot_phase_f_preflight"):
        from core.bot_live import read_only_preflight
        with st.spinner("Lese Hyperliquid-Kontostand …"):
            st.session_state["bot_preflight"] = read_only_preflight()

    preflight = st.session_state.get("bot_preflight")
    if preflight:
        if preflight["ok"]:
            st.success(f"Erfolgreich · Kontowert {preflight['equity_usd']:,.2f} \\$ · "
                       f"Mindestorder {preflight['min_notional_usd']:,.0f} \\$")
        else:
            st.error(_md_escape(preflight.get("error") or "Vorprüfung nicht bestanden."))
        for check in preflight["checks"]:
            st.write(("✅ " if check["ok"] else "❌ ") + check["label"])

    st.divider()
    st.markdown("#### Live-Schalter")
    blockers = []
    if state["running"]:
        blockers.append("Runner läuft noch – zuerst stoppen.")
    if not bot_config.preflight_is_fresh():
        blockers.append("Vorprüfung fehlt oder ist älter als 1 Stunde.")
    if blockers:
        st.info(" ".join(blockers))

    st.caption(f"Die erste Eröffnung nach der Aktivierung wird auf "
               f"{bot_config.FIRST_LIVE_ORDER_CAP_USD:,.0f} $ Nominale gedeckelt.")
    confirm_word = st.text_input("Zum Freischalten 'LIVE' eintippen:", key="bot_live_confirm_word")
    if st.button("Live-Handel aktivieren", type="primary",
                 disabled=bool(blockers) or confirm_word.strip().upper() != "LIVE",
                 key="bot_live_on"):
        bot_config.set_dry_run(False)
        bot_config.reset_first_live_order()
        st.rerun()

    with st.expander("Optional: separaten Echtgeld-Scharfschuss durchführen"):
        st.code("python phase_f_shot.py --confirm BTC-LONG-12USD-1X-STOP1PCT",
                language="powershell")
        if bot_config.phase_f_is_fresh():
            st.success("Zuletzt erfolgreich: "
                       f"{clock.local_str(bot_config.phase_f_verified_at())}")


def _completeness_warning(report: dict, threshold_pct: float = 90.0) -> str | None:
    """Symbole, deren geladene 4h-Kerzenzahl spürbar unter dem theoretischen
    Idealwert (6 Kerzen/Tag) liegt - entweder eine kürzere Listungs-Historie
    (legitim) ODER ein während des Abrufs abgebrochener/lückenhafter
    Datenabruf (data.hyperliquid.candles_range bricht bei einem Fehler still
    ab und gibt den bisherigen Teil zurück). Ohne diesen Hinweis sieht ein
    Lauf mit stillschweigend unvollständiger Historie identisch aus wie
    einer mit vollständiger - genau das Symptom, das CLAUDE.md unter
    "schwankende Vollständigkeit" dokumentiert."""
    completeness = report.get("candle_completeness") or {}
    thin = {s: c["completeness_pct"] for s, c in completeness.items()
           if c["completeness_pct"] < threshold_pct}
    if not thin:
        return None
    parts = ", ".join(f"{s} ({pct:.0f} %)" for s, pct in sorted(thin.items(), key=lambda kv: kv[1]))
    return (f"Kerzen-Vollständigkeit unter {threshold_pct:.0f} % (kürzere Historie ODER "
           f"abgebrochener Abruf - Ergebnis mit Vorsicht vergleichen): {parts}")


def _override_diff_caption(risk_level: int, effective_limits: dict, overridden_keys: list) -> str:
    """"Rechnet mit aktiven Experten-Overrides für: X" nannte bisher nur die
    BETROFFENEN Schlüssel, nie WIE stark sie von dem abweichen, was die
    gewählte Stufe eigentlich vorsähe (Phase 2, Kapital-Realität) - eine
    Stufe 9 mit wirksam auf 2 gedeckeltem max_same_side_positions sah in der
    Kopfzeile identisch aus wie eine Stufe 9 ganz ohne Override."""
    nominal = bot_config.derive_limits(risk_level)
    parts = []
    for key in overridden_keys:
        label = bot_config.LIMIT_LABELS.get(key, key)
        nom, eff = nominal.get(key), effective_limits.get(key)
        if nom == eff:
            parts.append(label)
        else:
            parts.append(f"{label} (Stufe {risk_level}: {nom:g} → wirksam {eff:g})")
    return f"Rechnet mit aktiven Experten-Overrides für: {', '.join(parts)}."


def _md_escape(text) -> str:
    """Escaped Markdown-Sonderzeichen in FREITEXT aus DB/Guards/LLM (Fehler-,
    Begründungs-, Marktausblick-Texte) - insbesondere '$' liest Streamlits
    Markdown sonst als LaTeX-Mathe-Trenner: zwei oder mehr '$' im selben
    st.caption/st.markdown/st.info-Aufruf lassen den Text dazwischen
    unsichtbar verschwinden (beobachtet u.a. bei core.bot_guards-Meldungen
    wie "12.34$ ... 56.78$" und bei KI-Marktkommentaren mit Kurszielen wie
    "$50.000 ... $52.000"). st.metric ist NICHT betroffen (kein Markdown)."""
    return str(text).replace("$", "\\$")


def _latest_trading_bot_run() -> dict | None:
    for run in db.list_agent_runs(50):
        if run["target"] != "TRADING-BOT":
            continue
        try:
            report = json.loads(run["report_json"])
            report["_created_at"] = run["created_at"]
            report["_cost_usd"] = run["cost_usd"]
            return report
        except (json.JSONDecodeError, TypeError):
            return None
    return None


_AI_REPORT_INTERVAL_LABELS = {6: "6 h", 12: "12 h", 24: "24 h", 48: "48 h", 168: "168 h (1 Woche)"}


def _render_ai_toggle():
    """Schalter fuer den KI-Bericht (agents/trader.py) - reine
    Berichterstattung ohne Entscheidungsbefugnis, kostet aber bei JEDEM Lauf
    Claude-API-Gebuehren. Aus laesst den Bot unveraendert regelbasiert
    weiterlaufen (core.bot_signals/core.bot_guards brauchen ihn nie). Das
    Intervall ist eine reine Kosten-/Frequenz-Einstellung, KEIN Strategie-
    Parameter - deshalb hier statt im Risikoregler (core.bot_config
    strategy_constants())."""
    st.markdown("#### KI-Bericht")
    enabled = bot_config.ai_report_enabled()
    new_val = st.toggle(
        "🧠 KI-Bericht aktiv", value=enabled, key="bot_ai_report_toggle",
        help="Post-Mortem der letzten Trades + Marktausblick - reine Berichterstattung, "
             "kann keine Order auslösen oder verhindern. Kostet bei jedem Lauf Claude-API-"
             "Gebühren. Aus spart die Kosten vollständig; die Regel-Engine handelt "
             "unverändert weiter.")
    if new_val != enabled:
        bot_config.set_ai_report_enabled(new_val)
        st.rerun()

    if new_val:
        current_interval = int(bot_config.ai_report_interval_hours())
        options = list(bot_config.AI_REPORT_INTERVAL_CHOICES_HOURS)
        if current_interval not in options:
            current_interval = options[0]
        interval = st.select_slider(
            "Berichtsintervall", options=options, value=current_interval,
            format_func=lambda h: _AI_REPORT_INTERVAL_LABELS.get(h, f"{h} h"),
            key="bot_ai_report_interval_slider",
            help="Höchstens so oft läuft der KI-Bericht - zusätzlich sofort nach einem "
                 "ausgelösten Stop, unabhängig vom Intervall.")
        if interval != current_interval:
            bot_config.set_ai_report_interval_hours(interval)
            st.success(f"Berichtsintervall: {_AI_REPORT_INTERVAL_LABELS[interval]}.")


def _render_ai_insight(show_heading: bool = True):
    """Seit Phase 8 reine Berichterstattung (agents/trader.py) - die KI kann
    nichts mehr auslösen (kein 'schliessen', kein 'veto' mehr), deshalb gibt
    es hier auch keine "outcomes" mehr anzuzeigen."""
    if show_heading:
        st.markdown("#### KI-Bericht")
    run = _latest_trading_bot_run()
    if not run:
        st.caption("Noch kein KI-Lauf protokolliert.")
        return
    if run.get("error"):
        st.error(run["error"])
        return

    st.caption(f"Letzter Lauf: {clock.local_str(run.get('_created_at'))}")
    if run.get("marktausblick"):
        st.info(_md_escape(run["marktausblick"]))
    if run.get("gesamtkommentar"):
        st.markdown(f"_{_md_escape(run['gesamtkommentar'])}_")

    post_mortems = run.get("post_mortems", [])
    if not post_mortems:
        st.caption("Keine Post-Mortems in diesem Lauf.")
    for pm in post_mortems:
        # "trade" ist SYMBOL@Schlusszeitpunkt (agents.trader._closed_trade_candidates) -
        # nur zur Auswahl im Structured-Output-Schema eindeutig, fuer die
        # Anzeige wird es wieder in seine beiden Teile zerlegt.
        symbol, _, closed_at = str(pm.get("trade", "")).partition("@")
        with st.container(border=True):
            st.markdown(f"**📋 {symbol or '?'}**" + (f" · geschlossen {closed_at}" if closed_at else ""))
            st.caption(_md_escape(pm.get("einschaetzung", "")))
    components.render_usage(run.get("usage") or {"cost_usd": run.get("_cost_usd", 0.0)})


def _render_performance(summary: dict | None):
    if not summary:
        st.info("Noch keine Equity-Messung vorhanden. Nach dem ersten Runner-Zyklus "
                "erscheint hier der Netto-Verlauf.")
        return
    # Depot-Vergleichbarkeit: der Rest der App zeigt Portfoliowerte in EUR
    # (data.crypto.get_prices_eur), nur das Hyperliquid-Konto rechnet nativ in
    # USDC. Sicherheitskritische Rechnungen (core.bot.performance_summary,
    # das 90-Tage-Kriterium) bleiben bewusst in USD - hier wird nur fürs
    # Anzeigen mit demselben Kurs umgerechnet, den auch die Equity-Snapshots
    # nutzen (data.fx.get_fx_to_eur), damit Netto-Equity und PNL denselben
    # Kurs teilen statt zwei leicht unterschiedliche.
    from data import fx as fx_data
    rate = fx_data.get_fx_to_eur("USD") or 1.0

    c1, c2, c3, c4, c5 = st.columns(5)
    # BEREINIGTE Rendite als Kennzahl. Vorher stand hier net_return_pct, das
    # Ein-/Auszahlungen enthält - direkt neben dem bereinigten PnL. Eine
    # Einzahlung erschien so als zweistellige "Rendite" neben 0 € Gewinn.
    c1.metric("Netto-Equity (€)", f"{summary['current_eur']:,.2f} €",
              f"{summary['return_pct']:+.2f} %"
              if summary.get("return_pct") is not None else None,
              help="Rendite bereinigt um Ein-/Auszahlungen - also das, was der Handel "
                   "selbst erwirtschaftet hat.")
    c2.metric("PNL gesamt (€)", f"{summary['pnl_usd'] * rate:+,.2f} €",
              help="Ebenfalls bereinigt: eingezahltes Kapital zählt nicht als Gewinn.")
    c3.metric("Startkapital, Session (€)",
              f"{(bot.session_start_usd() or summary['start_usd']) * rate:,.2f} €")
    c4.metric("Gebühren kumuliert (€)", f"{summary['fees_usd'] * rate:,.3f} €")
    c5.metric("Funding netto (€)", f"{summary['funding_net_usd'] * rate:+,.3f} €")
    st.caption(f"Kontostand-Delta (inkl. Ein-/Auszahlungen): "
               f"{summary['net_pnl_usd'] * rate:+,.2f} € · Bruttogewinn vor Kosten: "
               f"{summary['gross_profit_usd'] * rate:+,.2f} € · Erfasste Kosten: "
               f"{summary['costs_usd'] * rate:,.3f} € · Ein-/Auszahlungen netto: "
               f"{summary['net_flows_usd'] * rate:+,.2f} € · Laufzeit: "
               f"{summary['days_running']} von 90 Tagen")

    _render_abort_criterion(summary)
    mode = st.segmented_control("Chartansicht",
                                ["Index", "Kursverlauf Bot", "Kontostand"],
                                default="Index", key="bot_performance_view")
    in_eur = mode in ("Kursverlauf Bot", "Kontostand")
    btc_prices = _load_btc_prices()
    since = bot.chart_start()
    perf = (bot.performance_eur_df(btc_prices, since=since) if in_eur
            else bot.performance_index_df(btc_prices, since=since))
    if perf is not None:
        # Kursverlauf und Kontostand getrennt: der Kontostand enthält Einzahlungen
        # und liegt deshalb weit über den bereinigten Reihen - gemeinsam gezeichnet
        # stauchte er die Vergleichslinien.
        perf = perf[["Kontostand"]] if mode == "Kontostand" else perf.drop(
            columns="Kontostand", errors="ignore")
    if perf is not None and len(perf) >= 2:
        value_label = "Wert (€)" if in_eur else "Index (Start = 100)"
        long = perf.reset_index().melt(id_vars="Zeitpunkt", var_name="Strategie",
                                       value_name="Wert")
        fig = px.line(long, x="Zeitpunkt", y="Wert", color="Strategie",
                      color_discrete_map={"Bot netto": "#23c55e", "BTC Buy & Hold": "#60a5fa",
                                          "EUR halten": "#94a3b8", "Kontostand": "#e2e8f0"},
                      labels={"Wert": value_label})
        fig.update_layout(height=350, margin=dict(l=0, r=0, t=10, b=0),
                          legend=dict(orientation="h", y=1.05), autosize=True,
                          hovermode="x unified")
        fig.update_xaxes(tickformat="%d.%m.%Y %H:%M", hoverformat="%d.%m.%Y %H:%M:%S")
        if in_eur:
            fig.update_yaxes(ticksuffix=" €", hoverformat=",.2f")
        # key ist Pflicht, sobald mehrere Plotly-Charts auf einer Seite stehen
        # koennen (StreamlitDuplicateElementId) - siehe CLAUDE.md.
        chart_key = {"Kursverlauf Bot": "eur", "Kontostand": "balance"}.get(mode, "index")
        st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False},
                        key=f"bot_performance_chart_{chart_key}")
        if mode == "Kontostand":
            st.caption("Tatsächliche Equity des Bot-Kontos inklusive Ein- und Auszahlungen - "
                       "ein Sprung nach oben kann eine Einzahlung sein, kein Handelsgewinn.")
        elif in_eur:
            st.caption("Bot netto, BTC Buy & Hold und EUR halten: vergleichbarer Wertverlauf "
                       "ab demselben Startkapital, ohne Ein- und Auszahlungen.")
        else:
            st.caption("Alle Vergleichsreihen starten bei 100. Beim Bot werden Ein- und "
                       "Auszahlungen aus dem Verlauf herausgerechnet.")
        if since is not None:
            st.caption(f"Verlauf ab {since.astimezone():%d.%m.%Y %H:%M} Uhr - frühere "
                       "Messpunkte sind ausgeblendet, Kennzahlen oben bleiben unverändert.")
        if mode != "Kontostand" and "BTC Buy & Hold" not in perf:
            st.caption("BTC-Buy-&-Hold ist erst sichtbar, sobald eine passende "
                       "BTC-Kurshistorie verfügbar ist.")
    else:
        st.caption("Der Vergleichschart entsteht ab der zweiten Equity-Messung.")


def _load_btc_prices() -> pd.Series | None:
    """Nur Anzeige-Daten: ein Fehler oder fehlende Historie darf den Bot nie beeinflussen."""
    try:
        info = bot.start_info() or {}
        started = datetime.fromisoformat(str(info["date"])).date()
        days = max(30, (clock.now_utc().date() - started).days + 5)
        return crypto_history.crypto_series_eur("BTC", days=days)
    except Exception:
        return None


def _render_abort_criterion(summary: dict):
    if not summary["evaluation_due"]:
        return
    reasons = bot.abort_reasons(summary)
    if reasons:
        st.error("Abbruchkriterium erfüllt: " + " · ".join(reasons)
                 + ". Neue Einstiege werden gesperrt; offene Positionen prüfen und den "
                   "Bot neu bewerten.")
    else:
        st.success("90-Tage-Abbruchkriterium aktuell nicht erfüllt.")


def _render_risk_bars(summary: dict):
    """Wie nah der Bot an seinen eigenen Limits ist - alle Bausteine kommen
    aus core.bot/core.bot_config, keiner erfordert eine neue Datenquelle."""
    limits = bot_config.bot_limits()
    positions_count = len(bot.open_positions())
    trades = bot.trades_today()
    current = summary.get("current_usd")

    rows = [
        {"label": "Positionen belegt", "invert": True,
         "score": min(100.0, positions_count / limits["max_positions"] * 100),
         "horizon": f"{positions_count} von {limits['max_positions']}"},
        {"label": "Trades heute", "invert": True,
         "score": min(100.0, trades / limits["max_trades_per_day"] * 100),
         "horizon": f"{trades} von {limits['max_trades_per_day']}"},
    ]

    # Portfolio-Heat (core.bot_guards.check_portfolio_heat, Phase 5): Dollar-
    # Risiko bis zum jeweiligen Stop, NICHT die Brutto-Nominale wie
    # "Positionen belegt" - eng gestoppte und weit gestoppte Positionen sehen
    # dort gleich aus, obwohl sie wirtschaftlich sehr unterschiedlich riskant
    # sind.
    if current and current > 0:
        heat_usd = bot._open_portfolio_heat_usd()
        heat_pct = heat_usd / current * 100
        heat_limit = limits["max_portfolio_heat_pct"]
        rows.append({"label": "Portfolio-Heat", "invert": True,
                     "score": min(100.0, heat_pct / heat_limit * 100) if heat_limit else 0.0,
                     "horizon": f"{heat_pct:.1f}% von {heat_limit:.1f}% Equity"})

    # Richtungs-Konzentration (core.bot_guards.check_same_side_concentration,
    # Phase 5): die staerker ausgelastete der beiden Richtungen - mehrere
    # gleichgerichtete Positionen auf verschiedenen Symbolen sind wirtschaftlich
    # eine gebuendelte Richtungswette, keine unabhaengigen (CLAUDE.md: "ETH,
    # NEAR und SOL innerhalb von 32 Minuten alle short").
    same_side_limit = limits["max_same_side_positions"]
    if same_side_limit:
        long_count = bot._same_side_open_count("long")
        short_count = bot._same_side_open_count("short")
        side, count = ("long", long_count) if long_count >= short_count else ("short", short_count)
        rows.append({"label": "Richtungs-Konzentration", "invert": True,
                     "score": min(100.0, count / same_side_limit * 100),
                     "horizon": f"{count} von {same_side_limit} {side}"})

    start_of_day = bot.equity_start_of_day_usd()
    if start_of_day and current is not None and start_of_day > 0:
        loss_pct = max(0.0, (start_of_day - current) / start_of_day * 100)
        limit_pct = limits["daily_loss_limit_pct"]
        rows.append({"label": "Tagesverlust-Limit", "invert": True,
                     "score": min(100.0, loss_pct / limit_pct * 100) if limit_pct else 0.0,
                     "horizon": f"{loss_pct:.1f}% von {limit_pct:.0f}%"})

    start_total = bot.equity_start_usd()
    if start_total and current is not None and start_total > 0:
        floor_pct = limits["equity_floor_pct"]
        current_pct = current / start_total * 100
        # Anteil des PUFFERS bis zum Boden, nicht der Abstand zum Start: bei
        # einem Boden von 70 % sind 100 -> 70 die vollen 100 % des Puffers.
        cushion_total = max(0.001, 100 - floor_pct)
        used = max(0.0, min(100.0, (100 - current_pct) / cushion_total * 100))
        rows.append({"label": "Equity-Boden-Puffer verbraucht", "invert": True, "score": used,
                     "horizon": f"aktuell {current_pct:.0f}% vom Start, "
                                f"Boden bei {floor_pct:.0f}%"})

    st.markdown("#### Risiko-Auslastung")
    components.render_bar_list(rows, key="bot_risk_bars")


def _gv_style(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    return _GREEN if v > 0 else _RED


def _render_positions(state: dict):
    st.markdown("#### Offene Positionen")
    rows = bot.open_positions()
    if not rows:
        st.caption("Keine offenen Positionen.")
        return

    from data.hyperliquid import mid_price
    view_rows = []
    prices: dict[str, float | None] = {}
    for p in rows:
        try:
            price = mid_price(p["symbol"])
        except Exception:
            # Ein fehlender Kurs darf die Tabelle nie sprengen - die Position
            # erscheint dann ohne uPnL/Stop-Abstand statt gar nicht.
            price = None
        prices[p["symbol"]] = price
        upnl = None
        stop_dist_pct = None
        if price:
            direction = 1 if p["side"] == "long" else -1
            upnl = (price - p["entry_px"]) * p["size"] * direction
            if p["stop_px"]:
                stop_dist_pct = abs(price - p["stop_px"]) / price * 100
        view_rows.append({
            "Symbol": p["symbol"],
            "Seite": _position_side_label(p["side"]),
            "Menge": p["size"],
            "Einstand ($)": p["entry_px"],
            "Kurs ($)": price,
            "uPnL ($)": upnl,
            "Hebel": f"{p['leverage']:.1f}x",
            "Stop ($)": p["stop_px"],
            "Abstand Stop (%)": stop_dist_pct,
            "Eröffnet": clock.local_str(p["opened_at"]),
        })

    df = pd.DataFrame(view_rows)
    styled = df.style.map(_gv_style, subset=["uPnL ($)"])
    st.dataframe(styled, width="stretch", hide_index=True, column_config={
        "uPnL ($)": st.column_config.NumberColumn(format="%.4f"),
        "Abstand Stop (%)": st.column_config.NumberColumn(format="%.1f%%"),
    })

    _render_position_editor(rows, prices, state)

    # Nachtrag fuer bereits VOR core.bot.backfill_exchange_opened_at() (bzw.
    # dem exchange_opened_at-Feature generell) uebernommene Positionen, deren
    # echter Eroeffnungszeitpunkt noch fehlt - ohne ihn zaehlt max_hold_hours
    # ab dem Uebernahme- statt dem tatsaechlichen Eroeffnungszeitpunkt.
    missing_opened_at = [p["symbol"] for p in rows
                         if p.get("origin") == "adopted" and not p.get("exchange_opened_at")]
    if missing_opened_at:
        if db.get_meta("bot_backfill_opened_at_requested") == "1":
            st.info("⏳ Nachtrag angefordert - wird vom laufenden Runner innerhalb weniger "
                    "Sekunden ausgeführt.")
        else:
            st.caption(f"Übernommene Position(en) ohne bekannten echten Eröffnungszeitpunkt: "
                       f"{', '.join(missing_opened_at)}.")
            if st.button("Echten Eröffnungszeitpunkt nachtragen", key="bot_backfill_opened_at",
                         disabled=not state["running"]):
                db.set_meta("bot_backfill_opened_at_requested", "1")
                st.rerun()
    if missing_opened_at and not state["running"]:
        st.caption("Runner muss laufen, damit der Nachtrag ausgeführt werden kann - "
                   "nur er hält die aktive Börsen-/Paper-Verbindung.")


def _locked_pnl(p: dict, stop_px: float) -> float:
    """Kursergebnis, falls der Stop exakt bei stop_px ausloest (vor Gebuehren,
    Slippage und Funding) - reine Orientierung fuer die Eingabe."""
    direction = 1 if p["side"] == "long" else -1
    return (stop_px - p["entry_px"]) * p["size"] * direction


# Default-Position des Reglers - "30" heisst "30 % des Wegs von Einstand bis
# aktuellem Kurs als Gewinn sichern", NICHT 30 % Kursabstand. Nutzerentscheidung
# (Beispiel: Einstand 7 $, Kurs 9 $ -> Regler-Default ergibt 7,6 $).
_MANUAL_STOP_DEFAULT_PCT = 30


def _profit_range(p: dict, price: float | None) -> float | None:
    """Strecke von Einstand bis Kurs IN GEWINNRICHTUNG - positiv nur, wenn die
    Position gerade im Gewinn steht (sonst kein sinnvoller 0-100%-Bereich:
    ein Long unter Einstand hat keine Gewinnstrecke zum Aufteilen). None,
    wenn kein Kurs vorliegt oder die Position im Verlust steht."""
    if not price or price <= 0:
        return None
    direction = 1.0 if p["side"] == "long" else -1.0
    rng = (price - p["entry_px"]) * direction
    return rng if rng > 0 else None


def _stop_from_pct(p: dict, price: float, pct: float) -> float:
    """pct=0 -> Einstand (Breakeven), pct=100 -> aktueller Kurs (voller,
    aber ungeschützter Gewinn) - linear dazwischen. Dieselbe Formel fuer
    Long UND Short ueber das Vorzeichen von `direction`."""
    direction = 1.0 if p["side"] == "long" else -1.0
    rng = (price - p["entry_px"]) * direction
    return p["entry_px"] + direction * (pct / 100.0) * rng


def _render_position_editor(rows: list[dict], prices: dict, state: dict):
    """Je Position ein Prozent-Regler (0-100 % der Strecke Einstand -> Kurs)
    statt einer manuellen Zahleneingabe (Nutzerentscheidung) fürs Stop-
    Nachziehen, plus ein eigener "Position schließen"-Knopf, und ganz unten
    "Alle Positionen schließen". Die UI legt in jedem Fall nur eine Anfrage
    ab (core.bot.request_manual_stop/request_close_position/das bestehende
    bot_close_all_requested-Flag) - ausgeführt wird ausschließlich vom
    Runner-Prozess, siehe Moduldoc."""
    with st.expander("🛠️ Positionen bearbeiten"):
        st.caption(
            f"Der Regler bestimmt, wie viel Prozent des Wegs von Einstand bis aktuellem "
            f"Kurs als Gewinn gesichert wird (0 % = Einstand, 100 % = aktueller Kurs). Der "
            f"Stop kann nur in Gewinnrichtung verschoben werden (nie lockern) und muss "
            f"mindestens {bot.MANUAL_STOP_MIN_GAP_PCT:.1f} % vom aktuellen Kurs entfernt "
            f"liegen. Der Runner ersetzt die Stop-Order auf der Börse innerhalb weniger "
            f"Sekunden; die automatische Regel zieht einen manuell engeren Stop nie zurück."
        )
        if not state["running"]:
            st.warning("Runner läuft nicht - eine Änderung wird erst nach dem Start "
                       "ausgeführt. Nur er hält die Börsenverbindung.")
        stop_pending = bot.manual_stop_requests()
        stop_results = bot.manual_stop_results()
        close_pending = bot.close_position_requests()
        close_results = bot.close_position_results()
        for p in rows:
            symbol = p["symbol"]
            price = prices.get(symbol)
            current = p["stop_px"]
            rng = _profit_range(p, price)
            with st.container(border=True):
                col_info, col_input, col_stop = st.columns(
                    [3, 3, 1.5], vertical_alignment="bottom")
                with col_info:
                    price_txt = f"{price:,.6g} $" if price else "–"
                    locked_txt = (f" · bei Stop {_locked_pnl(p, current):+,.2f} $"
                                  if current is not None else "")
                    stop_txt = f"{current:,.6g} $" if current is not None else "–"
                    st.markdown(_md_escape(f"**{symbol}** {_position_side_label(p['side'])}  \n"
                                           f"Kurs {price_txt} · Stop {stop_txt}{locked_txt}"))
                new_stop = None
                with col_input:
                    if rng is None:
                        st.caption("Position aktuell nicht im Gewinn oder Kurs nicht "
                                   "verfügbar - kein Bereich zum Aufteilen.")
                    else:
                        pct = st.slider("Gewinnsicherung", 0, 100, _MANUAL_STOP_DEFAULT_PCT,
                                        key=f"bot_manual_stop_pct_{symbol}", format="%d%%")
                        new_stop = _stop_from_pct(p, price, pct)
                        st.caption(f"→ Stop bei {new_stop:,.6g} $")
                with col_stop:
                    stop_clicked = st.button("Stop setzen", key=f"bot_manual_stop_btn_{symbol}",
                                             disabled=symbol in stop_pending or new_stop is None,
                                             width="stretch")
                if stop_clicked and new_stop is not None:
                    error = bot.validate_manual_stop(p, new_stop, price)
                    if error:
                        st.error(error)
                    else:
                        _manual_stop_dialog(p, new_stop, price)

                # Eigene, rechtsbuendige Zeile statt eng neben dem Stop-
                # Regler - "Position schließen" ist eine eigenstaendige,
                # unumkehrbare Aktion und braucht sichtbar mehr Platz als in
                # einer engen vierten Spalte.
                _, col_close = st.columns([4, 1.7])
                with col_close:
                    close_clicked = st.button("🚪 Position schließen",
                                              key=f"bot_close_position_btn_{symbol}",
                                              disabled=symbol in close_pending or not state["running"],
                                              width="stretch")
                if close_clicked:
                    _close_position_dialog(p)

                if symbol in stop_pending:
                    req = stop_pending[symbol]
                    st.info(_md_escape(f"⏳ Stop auf {req['stop_px']:,.6g} $ angefordert - "
                                       f"wartet auf den Runner."))
                    if st.button("Anfrage zurückziehen", key=f"bot_manual_stop_cancel_{symbol}"):
                        bot.cancel_manual_stop_request(symbol)
                        st.rerun()
                elif symbol in stop_results:
                    res = stop_results[symbol]
                    when = clock.local_str(res.get("finished_at")) if res.get("finished_at") else ""
                    if res.get("status") == "ok":
                        st.success(_md_escape(f"✅ {res.get('reason')} {when}"))
                    else:
                        st.error(_md_escape(f"❌ {res.get('reason')} {when}"))

                if symbol in close_pending:
                    st.info("⏳ Schließung angefordert - wartet auf den nächsten Runner-Takt.")
                    if st.button("Schließen-Anfrage zurückziehen",
                                 key=f"bot_close_position_cancel_{symbol}"):
                        bot.cancel_close_position_request(symbol)
                        st.rerun()
                elif symbol in close_results:
                    res = close_results[symbol]
                    when = clock.local_str(res.get("finished_at")) if res.get("finished_at") else ""
                    if res.get("status") == "filled":
                        pnl = res.get("realized_pnl_usd")
                        pnl_txt = f" · PnL {pnl:+,.2f} $" if pnl is not None else ""
                        st.success(_md_escape(f"✅ {symbol}: geschlossen{pnl_txt} {when}"))
                    elif not res.get("still_open", True):
                        st.success(_md_escape(f"✅ {symbol}: geschlossen {when}"))
                    else:
                        st.error(_md_escape(f"❌ {symbol}: {res.get('reason') or res.get('status')} "
                                            f"{when} - nächster Takt versucht erneut."))

        st.divider()
        close_all_pending = db.get_meta("bot_close_all_requested") == "1"
        if close_all_pending:
            if state["running"]:
                st.info("⏳ Schließung aller Positionen angefordert - wartet auf den nächsten "
                        "Runner-Takt (läuft, wird innerhalb weniger Sekunden ausgeführt).")
            else:
                # Der Runner ist NICHT aktiv - eine frühere Fassung meldete hier
                # trotzdem "wird ausgeführt" und blendete den Knopf per return aus,
                # obwohl nur der Runner das Flag jemals liest (core.bot_runner.
                # _maybe_close_all). Ohne laufenden Runner bleibt das Flag stehen,
                # bis er gestartet wird - das muss sichtbar sein, sonst wirkt eine
                # tatenlose Anfrage wie ein erledigter Notfall-Schluss.
                st.warning("⚠️ Schließung angefordert, aber der Runner läuft nicht - "
                           "sie wird erst ausgeführt, sobald er gestartet ist. Nur er hält "
                           "die aktive Börsen-/Paper-Verbindung.")
                if st.button("Anfrage zurückziehen", key="bot_close_all_cancel"):
                    db.delete_meta("bot_close_all_requested")
                    st.rerun()
        if st.button("🚪 Alle Positionen schließen", key="bot_close_all_open",
                     disabled=not state["running"]):
            _close_all_dialog(rows)
        if not state["running"]:
            st.caption("Runner muss laufen, damit eine Änderung ausgeführt werden kann - "
                       "nur er hält die aktive Börsen-/Paper-Verbindung.")


@st.dialog("Stop-Loss nachziehen")
def _manual_stop_dialog(p: dict, new_stop: float, price: float | None):
    symbol = p["symbol"]
    st.write(f"{_position_side_label(p['side'])} **{symbol}** · Größe {p['size']:.6g}")
    old = p.get("stop_px")
    old_txt = f"{old:,.6g} $" if old is not None else "–"
    st.write(_md_escape(f"Stop: **{old_txt} → {new_stop:,.6g} $**"))
    if price:
        gap = abs(price - new_stop) / price * 100
        st.write(_md_escape(f"Aktueller Kurs {price:,.6g} $ · Abstand {gap:.2f} %"))
    before = f" (bisher {_locked_pnl(p, old):+,.2f} $)" if old is not None else ""
    st.write(_md_escape(f"Ergebnis bei Auslösung (vor Kosten): "
                        f"**{_locked_pnl(p, new_stop):+,.2f} $**{before}"))
    st.caption("Der Runner ersetzt die Stop-Order auf der Börse: erst wird der neue Stop "
               "platziert und verifiziert, danach der alte storniert.")
    if st.button("Ja, Stop setzen", type="primary", key=f"bot_manual_stop_go_{symbol}"):
        bot.request_manual_stop(symbol, new_stop)
        st.rerun()


@st.dialog("Position schließen")
def _close_position_dialog(p: dict):
    symbol = p["symbol"]
    st.write(f"{_position_side_label(p['side'])} **{symbol}** · Größe {p['size']:.6g} wird "
             f"zum aktuellen Marktpreis geschlossen.")
    confirm = st.checkbox(f"Ich möchte {symbol} jetzt schließen.",
                          key=f"bot_close_position_confirm_{symbol}")
    if st.button("Ja, jetzt schließen", type="primary", disabled=not confirm,
                 key=f"bot_close_position_go_{symbol}"):
        bot.request_close_position(symbol)
        st.rerun()


@st.dialog("Alle Positionen schließen")
def _close_all_dialog(rows: list[dict]):
    st.write(f"{len(rows)} offene Position(en) werden zum aktuellen Marktpreis geschlossen:")
    for p in rows:
        st.write(f"- {_position_side_label(p['side'])} **{p['symbol']}** · Größe {p['size']:.6g}")
    confirm = st.checkbox("Ich möchte alle offenen Positionen jetzt schließen.",
                          key="bot_close_all_confirm")
    if st.button("Ja, jetzt schließen", type="primary", disabled=not confirm,
                 key="bot_close_all_go"):
        db.set_meta("bot_close_all_requested", "1")
        st.rerun()


def _signal_scans(limit: int = 200) -> list[dict]:
    """Alle `signal_scan`- und `signal_entry_*`-Einträge, jüngster zuerst.

    core.bot.run_signal_cycle() protokolliert bei JEDEM Takt einen dieser
    beiden Aktionstypen - auch dann, wenn nichts passiert - und seit dem
    Scan-Protokoll-Fix in beiden Fällen den KOMPLETTEN Kandidatenscan.
    Vorher las diese Funktion nur den einen jüngsten Takt; ein Blick zurück
    ("was sah der Bot vor zwei Stunden?") war damit gar nicht möglich.
    """
    out = []
    for d in db.list_bot_decisions(limit):
        if d["action"] != "signal_scan" and not (d["action"] or "").startswith("signal_entry_"):
            continue
        try:
            signals = json.loads(d["signals_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            signals = {}
        out.append({"created_at": d["created_at"], "reason": d["reason"],
                    "executed": d["executed"], "action": d["action"], "signals": signals,
                    "config_hash": d.get("config_hash")})
    return out


# Maximalpunkte je Score-Block - Spiegel von core.bot_signals.score_side().
# Stehen hier NUR für die Anzeige ("31 von 35"); die Punktevergabe selbst
# bleibt allein Sache der Signal-Engine. "Momentum" ist seit V2 EIN Fenster
# (core.bot_signals.TREND_WINDOW Kerzen), nicht mehr V1s getrennte 24h/72h.
_SCORE_PARTS = (
    ("trend", "Trend (Lage zu MA50, MA20/MA50)", 35.0),
    ("momentum", "Momentum (Trendfenster)", 35.0),
    ("confirmation", "MACD-Bestätigung", 18.0),
    ("rsi", "RSI-Fortsetzungsband", 12.0),
    ("funding", "Funding", 10.0),
)

# Pflichtbedingungen in core.bot_signals.evaluate(), IN DER REIHENFOLGE, in
# der die Funktion sie tatsächlich prüft (IF/ELIF-Kette - bricht bei der
# ERSTEN verletzten Bedingung ab). `markers` sind Teilstrings, an denen ein
# gegebenes `blocked_by` eindeutig einer Stufe zugeordnet werden kann - siehe
# _gate_checklist(). Reihenfolge MUSS mit evaluate() übereinstimmen, sonst
# zeigt die Checkliste vor dem tatsächlichen Blocker fälschlich "erfüllt".
#
# Gates 3/4/5/6 sind seit STRATEGY_VERSION "2.2-swing4h" ENTFERNT (Phase 3/4,
# Gate-Ablation, siehe core/bot_signals.py's Konstanten-Block - eine
# Out-of-Sample-Messung zeigte, dass ihr Entfernen den Erwartungswert nicht
# messbar verschlechtert). Die Nummerierungslücke bis Gate 7 ist bewusst
# stehen gelassen (historische bot_signal_log-Zeilen referenzieren die
# ursprüngliche Nummerierung).
_GATE_STAGES = (
    ("Datenbasis (genug Kerzen)", ("Zu wenige Kerzen",)),
    ("Tagesregime (Gate 1)", ("Kein handelbares Tagesregime", "Shorts sind deaktiviert")),
    ("ATR-Band (Stop berechenbar)", ("Kein ATR verfügbar", "Kein Live-Kurs verfügbar",
                                     "zu niedrig - Bewegung deckt", "zu hoch - der nötige Stop")),
    ("Kurs/MA-Struktur (Gate 2)", ("Gate 2",)),
    ("Liquidität (Gate 7)", ("Gate 7",)),
)


def _gate_checklist(blocked_by: str | None) -> list[tuple[str, str]]:
    """Ordnet ein Signal.blocked_by den tatsächlich durchlaufenen
    Pflichtbedingungen zu - evaluate() bricht bei der ERSTEN verletzten
    Bedingung ab, also ist alles DAVOR implizit erfüllt (sonst wäre die
    Funktion dort schon zurückgekehrt) und alles DANACH nie geprüft. Gibt je
    Stufe (Label, 'ok'|'failed'|'unchecked') zurück."""
    failed_index = None
    if blocked_by:
        for i, (_, markers) in enumerate(_GATE_STAGES):
            if any(m in blocked_by for m in markers):
                failed_index = i
                break
    out = []
    for i, (label, _) in enumerate(_GATE_STAGES):
        if failed_index is None or i < failed_index:
            out.append((label, "ok"))
        elif i == failed_index:
            out.append((label, "failed"))
        else:
            out.append((label, "unchecked"))
    return out


def _render_signal_detail(row: dict):
    """Aufschlüsselung EINER Kandidaten-Bewertung.

    Die Daten lagen längst vollständig in bot_decisions.signals_json
    (core.bot_signals.Signal.as_row schreibt `components`, `reasons`,
    Long-/Short-Score, Stop-Abstand und Funding mit) - die Tabelle warf fünf
    dieser Felder beim Rendern weg. Hier wird nichts neu berechnet, nur
    endlich gezeigt. Kein "nötige Schwelle" mehr (V1) - der Score ist seit
    der V2-Signal-Engine nur noch die Rangfolge unter bereits qualifizierten
    Kandidaten; die Pflichtbedingungen (Gates) entscheiden allein, OB
    gehandelt wird - deshalb stehen sie hier als eigene Checkliste, nicht
    nur als ein einzelner `blocked_by`-Satz.
    """
    st.markdown("**Pflichtbedingungen**")
    for label, status in _gate_checklist(row.get("blocked_by")):
        icon = {"ok": "✅", "failed": "⛔", "unchecked": "—"}[status]
        st.caption(f"{icon} {label}")
    if row.get("blocked_by"):
        st.caption(f"↳ _{_md_escape(str(row['blocked_by']))}_")

    components_map = row.get("components") or {}
    lines = []
    for key, label, maximum in _SCORE_PARTS:
        value = components_map.get(key)
        if value is None:
            continue
        lines.append(f"| {label} | {value:+.0f} | {maximum:.0f} |")
    if lines:
        st.markdown("**Rangfolge-Score** (nur unter bereits qualifizierten Kandidaten)")
        st.markdown("| Baustein | Punkte | Maximum |\n|---|---:|---:|\n" + "\n".join(lines))

    score = row.get("score")
    if score is not None:
        st.markdown(f"**Gesamtscore: {score:.0f}**")

    meta = []
    if row.get("long") is not None and row.get("short") is not None:
        meta.append(f"Long {row['long']:.0f} / Short {row['short']:.0f}")
    if row.get("stop_pct") is not None:
        meta.append(f"Stop-Abstand {row['stop_pct']:.2f} %")
    if row.get("atr_pct") is not None:
        meta.append(f"ATR {row['atr_pct']:.2f} %")
    if row.get("funding") is not None:
        meta.append(f"Funding {row['funding'] * 100:+.4f} %/h")
    if meta:
        st.caption(" · ".join(meta))
    for reason in row.get("reasons") or []:
        st.caption(f"• {_md_escape(str(reason))}")


def _render_candidate_pool(state: dict | None = None):
    """Zeigt den laufend aktualisierten Kandidaten-Pool (core.bot_universe) -
    ersetzt seit der Umstellung auf einen dynamischen Filter (Marktkap. +
    Mindestalter + Hyperliquid-Liquidität) den früher festen 11er-Pool.
    Steht bewusst VOR "Warum handelt der Bot (nicht)?": erst was der Bot
    überhaupt ansehen darf, dann warum er (nicht) gehandelt hat."""
    st.markdown("#### Kandidaten-Pool")
    pool = bot_universe.active_pool()
    refreshed_at = bot_universe.pool_refreshed_at()
    ts = clock.local_str(refreshed_at) if refreshed_at else None

    if not pool:
        st.caption("Noch kein dynamischer Pool ermittelt - der Bot nutzt bis dahin den "
                   f"festen Rückfall-Pool ({', '.join(bot.CANDIDATE_UNIVERSE)}).")
    else:
        view = [{
            "Symbol": r.get("symbol"), "Rang": r.get("rang"),
            "Marktkap. (€)": r.get("marktkap_eur"),
            "HL-24h-Volumen ($)": r.get("hl_day_volume_usd"),
            "Aufnahmegrund": r.get("included_reason") or "",
        } for r in pool]
        df = pd.DataFrame(view)
        st.dataframe(df, width="stretch", hide_index=True, column_config={
            "Marktkap. (€)": st.column_config.NumberColumn(format="%.0f"),
            "HL-24h-Volumen ($)": st.column_config.NumberColumn(format="%.0f"),
        })
        caption = (f"Stand: {ts} · Schwellen: Marktkap. ≥ {bot_universe.MIN_MARKET_CAP_EUR:,.0f} € · "
                  f"Alter ≥ {bot_universe.MIN_LISTING_AGE_DAYS} Tage · "
                  f"HL-24h-Volumen ≥ {bot_universe.MIN_HL_DAY_VOLUME_USD:,.0f} $ · "
                  f"höchstens {bot_universe.MAX_ACTIVE_CANDIDATES} aktive Kandidaten (nach Marktkap.-Rang).")
        st.caption(caption)

    if db.get_meta("bot_universe_refresh_requested") == "1":
        if state and not state["running"]:
            st.warning("⚠️ Aktualisierung angefordert, aber der Runner ist gestoppt. "
                       "Sie startet erst mit dem nächsten Runner-Start.")
        else:
            st.info("⏳ Aktualisierung angefordert - wird vom laufenden Runner innerhalb "
                    "weniger Sekunden ausgeführt.")
    else:
        runner_stopped = bool(state and not state["running"])
        if st.button("🔄 Pool jetzt aktualisieren", key="bot_universe_refresh_btn",
                     disabled=runner_stopped,
                     help="Der Runner lädt Marktkapitalisierung und Liquidität neu."):
            db.set_meta("bot_universe_refresh_requested", "1")
            st.rerun()
        if runner_stopped:
            st.caption("Runner starten, um den Kandidaten-Pool zu aktualisieren.")


def _render_signal_scan():
    st.markdown("#### Warum handelt der Bot (nicht)?")
    scans = _signal_scans()
    if not scans:
        st.caption("Noch kein Signal-Takt protokolliert. Erscheint nach dem ersten Runner-Zyklus.")
        return

    labels = [clock.local_str(s["created_at"])
              + (" · Einstieg" if s["action"].startswith("signal_entry_") else "")
              for s in scans]
    choice = st.selectbox("Takt", range(len(scans)), format_func=lambda i: labels[i],
                          key="bot_scan_pick")
    latest = scans[choice]

    ts = clock.local_str(latest["created_at"])
    if latest.get("config_hash"):
        # config_fingerprint() (core.bot_config) - der exakte Nachweis, unter
        # welcher effektiven Konfiguration DIESER Takt entschied (aussagekräftiger
        # als eine blosse strategy_version-Zeichenkette, die sich zwischen zwei
        # Konfigurationsänderungen nicht unterscheidet).
        st.caption(f"Konfigurations-Hash dieses Takts: `{latest['config_hash']}`")
    if latest["action"].startswith("signal_entry_"):
        st.success(f"Takt {ts}: Einstieg ausgelöst - {_md_escape(latest['reason'])}")
    else:
        st.info(f"Takt {ts}: {_md_escape(latest['reason'])}")

    # Seit dem Scan-Protokoll-Fix tragen BEIDE Aktionstypen den kompletten
    # Scan. Vorher wurde die Tabelle bei einem Einstiegs-Takt übersprungen -
    # also ausgerechnet dann, wenn eine Entscheidung gefallen war.
    rows = latest["signals"].get("signals") or []
    entry = (latest["signals"].get("entry") or {}).get("symbol")
    if not rows:
        st.caption("Für diesen Takt wurde kein vollständiger Scan protokolliert "
                   "(Eintrag stammt aus einer älteren Version).")
        return

    # Kein "Status: über Schwelle" mehr (V1) - seit der V2-Signal-Engine ist
    # jedes nicht blockierte Signal per Definition gate-qualifiziert
    # (core.bot_signals.Signal.tradeable), der Score nur noch die Rangfolge
    # darunter.
    view = [{
        "Symbol": row.get("symbol"), "Seite": row.get("side") or "—",
        "Score": row.get("score"),
        "Status": ("gehandelt" if row.get("symbol") == entry
                   else "blockiert" if row.get("blocked_by")
                   else "qualifiziert"),
        "ATR (%)": row.get("atr_pct"), "Blockiert durch": row.get("blocked_by") or "",
    } for row in rows]
    df = pd.DataFrame(view).sort_values("Score", ascending=False, na_position="last")
    st.dataframe(df, width="stretch", hide_index=True)

    # EIN Ausklapper fuer die gesamte Aufschluesselung statt einem je
    # Kandidat direkt auf der Seite - Streamlit erlaubt keine verschachtelten
    # Expander, daher hier Markdown-Zwischenueberschriften + Trenner statt
    # einzelner st.expander() je Kandidat innerhalb dieses Ausklappers.
    with st.expander("Aufschlüsselung je Kandidat"):
        sorted_rows = sorted(rows, key=lambda r: r.get("score") or 0, reverse=True)
        for i, row in enumerate(sorted_rows):
            if i > 0:
                st.divider()
            side = (row.get("side") or "").upper()
            st.markdown(f"**{row.get('symbol')} {side} · {row.get('score', 0):.0f} Punkte**")
            _render_signal_detail(row)


_ORIGIN_LABEL = {"bot": "Bot-Entscheidung", "adopted": "auf der Börse vorgefunden",
                 "manual": "manuell"}


def _trade_postmortem(p: dict) -> dict:
    """Kennzahlen NACH dem Trade - aus vorhandenen Daten, ohne Netzabruf.

    R-Multiple ist die ehrlichste Einzelzahl: sie misst den Gewinn in
    Vielfachen des Betrags, der beim Einstieg bewusst riskiert wurde (Abstand
    Einstand -> initialer Stop). Ein +2R-Trade bei 1 % Risiko und ein
    +2R-Trade bei 3 % Risiko sind dieselbe Güte der Entscheidung, auch wenn
    die Eurobeträge weit auseinanderliegen.
    """
    out = {"fees_usd": None, "r_multiple": None, "hold_hours": None,
           "risk_usd": None, "hold_return_pct": None}
    if p.get("id"):
        out["fees_usd"] = db.bot_position_fees_usd(p["id"])
    # initial_stop_px ist UNVERAENDERLICH, stop_px wird beim Nachziehen
    # ueberschrieben - mit stop_px waere ein Trade, der anfangs 2 $ riskierte
    # und spaeter fast bis zum Einstieg nachgezogen wurde, faelschlich mit
    # einem Vielfachen des tatsaechlichen R gezeigt worden. Fuer Zeilen aus
    # der Zeit vor dieser Spalte (Migration) bleibt stop_px der einzig
    # verfuegbare Notbehelf.
    entry = p.get("entry_px")
    stop = p.get("initial_stop_px") or p.get("stop_px")
    size = p.get("size")
    if entry and stop and size:
        risk = abs(float(entry) - float(stop)) * float(size)
        out["risk_usd"] = risk
        if risk > 0 and p.get("realized_pnl_usd") is not None:
            out["r_multiple"] = float(p["realized_pnl_usd"]) / risk
    opened = p.get("exchange_opened_at") or p.get("opened_at")
    if opened and p.get("closed_at"):
        try:
            delta = datetime.fromisoformat(str(p["closed_at"])) - datetime.fromisoformat(str(opened))
            out["hold_hours"] = delta.total_seconds() / 3600
        except (TypeError, ValueError):
            pass
    if entry and p.get("close_px"):
        direction = 1 if p.get("side") == "long" else -1
        out["hold_return_pct"] = (float(p["close_px"]) / float(entry) - 1) * 100 * direction
    return out


def _exit_reason(p: dict) -> str | None:
    """Ausstiegsgrund aus der zugehörigen Order-Zeile.

    Bewusst über den `intent` statt über einen Textabgleich: ein
    Bestandsabgleich (`exchange_reconciled_close`) und ein echter Ausstieg
    dürfen im Postmortem nie gleich aussehen. Innerhalb des Bestandsabgleichs
    selbst wird zusätzlich zwischen echtem Börsen-Fill und einer Schätzung
    unterschieden (`fee_usd is None` markiert die Schätzung, siehe
    core.bot._actual_close_from_fills) - sonst wäre ein grob geschätzter PnL
    im Postmortem nicht von einem präzisen zu unterscheiden ("falsche
    Genauigkeit").
    """
    orders = [o for o in db.list_bot_orders(500) if o.get("position_id") == p.get("id")]
    closing = [o for o in orders if o["intent"] in ("close", "exchange_reconciled_close")]
    if closing and closing[-1]["intent"] == "exchange_reconciled_close":
        if closing[-1].get("fee_usd") is None:
            return ("Bestandsabgleich (auf der Börse nicht mehr offen, PnL GESCHÄTZT "
                    "anhand des Kurses beim nächsten Takt, kein echter Fill-Kurs)")
        return "Bestandsabgleich (auf der Börse nicht mehr offen, echter Fill-Kurs aus der Börsen-Historie)"
    return None


def _render_backtest():
    """Rücktest der Bot-Regeln über echte Hyperliquid-Historie - zwei Tabs:
    ein einzelner Rücktest (wie bisher) und der Walk-Forward-Test mit dem
    eingefrorenen Live-Freigabe-Kriterium (analysis/bot_walkforward.py,
    Phase 7)."""
    st.markdown("#### Rücktest der Regeln")
    tab_single, tab_wf = st.tabs(["Einzelner Rücktest", "Walk-Forward + Live-Gate"])
    with tab_single:
        _render_single_backtest()
    with tab_wf:
        _render_walkforward()


def _render_single_backtest():
    st.caption("Rechnet die HEUTIGE Signal- und Guard-Logik über echte "
               "4h-Kerzen und das BTC-Tagesregime zurück - inklusive Gebühren, "
               "Funding, Slippage und Intrabar-Stops. Bewusst ohne "
               "Parametersuche: die Frage ist \"hat diese Regel Geld "
               "verdient\", nicht \"welche Einstellung wäre die beste "
               "gewesen\". Liquidität (Gate 7) ist historisch nicht "
               "rekonstruierbar und wird deshalb immer als erfüllt "
               "angenommen - das Ergebnis ist tendenziell optimistischer "
               "als live. Das Kandidaten-Universum ist ebenfalls ein "
               "einziger, HEUTIGER Schnappschuss (Marktkap./Alter/Liquidität) "
               "statt einer je Zeitpunkt rekonstruierten Auswahl.")

    c1, c2, c3 = st.columns([1, 1, 2])
    months = c1.selectbox("Zeitraum", [6, 9, 12], index=0,
                          format_func=lambda m: f"{m} Monate", key="bot_bt_months")
    level = c2.number_input("Risikostufe", 1, 10, bot_config.risk_level(),
                            key="bot_bt_level")
    if c3.button("Rücktest starten", key="bot_bt_run"):
        from analysis import bot_backtest

        symbols = bot.candidate_symbols()
        with st.spinner(f"Lade {months} Monate 4h-Kerzen für "
                        f"{len(symbols)} Symbole …"):
            market = bot_backtest.load_market_data(symbols, days=months * 30)
        if not market["candles"]:
            st.error("Keine Kursdaten erhalten - Rücktest nicht möglich.")
            return
        with st.spinner("Rechne jeden Takt durch … das dauert ein bis zwei Minuten."):
            st.session_state["bot_backtest"] = bot_backtest.run_backtest(
                market, risk_level=int(level),
                starting_equity_usd=bot.equity_start_usd() or 250.0)

    result = st.session_state.get("bot_backtest")
    if not result:
        st.caption("Noch kein Rücktest gerechnet.")
        return
    if result.get("error"):
        st.error(_md_escape(result["error"]))
        return

    m = result["metrics"]
    start, end = result["period"]
    st.caption(f"Zeitraum {start:%d.%m.%Y} bis {end:%d.%m.%Y} · Risikostufe "
               f"{result['risk_level']} · {len(result['candles_per_symbol'])} Symbole")
    if result.get("overridden_limit_keys"):
        # Seit Phase 6 uebernimmt der Rücktest dieselben gespeicherten
        # Experten-Overrides wie der Live-Pfad (core.bot_config.
        # limits_for_risk_level) - der reine Schluessel-Name allein sagt
        # aber nicht, WIE stark die Stufe dadurch verschoben wird (Phase 2,
        # Kapital-Realität) - deshalb je Zeile Stufen- gegen Wirkwert.
        st.caption(_override_diff_caption(result["risk_level"], result["limits"],
                                          result["overridden_limit_keys"]))
    if result.get("missing_funding"):
        # Lieber laut sagen als still mit 0 rechnen - Funding ist bei
        # gehebelten Dauerpositionen der groesste Kostenblock.
        st.warning("Ohne Funding-Historie gerechnet (Ergebnis daher zu gut) für: "
                   + ", ".join(result["missing_funding"]))
    completeness_warning = _completeness_warning(result.get("load_report") or {})
    if completeness_warning:
        st.warning(completeness_warning)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Ergebnis", f"{m['return_pct']:+.1f} %")
    k2.metric("Trades", f"{m['trades']}")
    k3.metric("Trefferquote",
              f"{m['win_rate_pct']:.0f} %" if m["win_rate_pct"] is not None else "—")
    k4.metric("Profit-Faktor",
              f"{m['profit_factor']:.2f}" if m["profit_factor"] is not None else "—")
    k5.metric("Max. Rückgang",
              f"{m['max_drawdown_pct']:.1f} %" if m["max_drawdown_pct"] is not None else "—")
    avg_r = f"{m['avg_r']:+.2f}" if m["avg_r"] is not None else "—"
    st.caption(f"Ø R-Multiple: {avg_r} · Gebühren {m['fees_usd']:,.2f} \\$ · "
               f"Funding {m['funding_usd']:+,.2f} \\$ · "
               f"Slippage {m['slippage_usd']:,.2f} \\$")

    bench = result["benchmarks"]
    b1, b2, b3 = st.columns(3)
    b1.metric("Bot", f"{bench['bot_usd']:,.2f} $" if bench["bot_usd"] else "—")
    b2.metric("BTC halten", f"{bench['btc_buy_hold_usd']:,.2f} $"
              if bench["btc_buy_hold_usd"] else "—")
    b3.metric("BTC über MA50", f"{bench['btc_ma50_trend_usd']:,.2f} $"
              if bench["btc_ma50_trend_usd"] else "—")
    st.caption("Schlägt der vielteilige Score die Ein-Zeilen-Trendfolge nicht, "
               "trägt seine Komplexität nichts bei.")

    curve = result["equity_curve"]
    if len(curve) >= 2:
        fig = px.line(curve.reset_index().rename(
            columns={"index": "Zeitpunkt", 0: "Equity ($)"}),
            x="Zeitpunkt", y="Equity ($)")
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0), autosize=True)
        st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False},
                        key="bot_backtest_chart")

    tab_side, tab_regime, tab_blocked, tab_trades = st.tabs(
        ["Long/Short", "Marktregime", "Warum nicht gehandelt", "Trades"])
    with tab_side:
        st.dataframe(_group_frame(result["by_side"]), width="stretch")
    with tab_regime:
        st.caption("Regime = BTC-Tagesregime bei Eröffnung (core.bot_signals.regime: "
                   "MA50 auf Tageskerzen, long/short/neutral).")
        st.dataframe(_group_frame(result["by_regime"]), width="stretch")
    with tab_blocked:
        if result["blocked_reasons"]:
            st.dataframe(pd.DataFrame(
                [{"Grund": k, "Takte": v} for k, v in result["blocked_reasons"].items()]),
                width="stretch", hide_index=True)
        else:
            st.caption("Keine Blockaden protokolliert.")
    with tab_trades:
        trades = result["trades"]
        if not trades:
            st.caption("Keine Trades im Zeitraum.")
        else:
            st.dataframe(pd.DataFrame([{
                "Symbol": t["symbol"], "Seite": t["side"],
                "Eröffnet": clock.local_str(t["opened_at"]),
                "Geschlossen": clock.local_str(t["closed_at"]),
                "Netto ($)": round(t["net_pnl_usd"], 4),
                "R": None if t["r_multiple"] is None else round(t["r_multiple"], 2),
                "Stunden": round(t["hold_hours"], 1), "Grund": t["reason"],
            } for t in trades]), width="stretch", hide_index=True)


def _group_frame(groups: dict) -> pd.DataFrame:
    return pd.DataFrame([{
        "Gruppe": name, "Trades": g["trades"],
        "Netto ($)": round(g["net_pnl_usd"], 2),
        "Trefferquote (%)": round(g["win_rate_pct"], 1),
    } for name, g in sorted(groups.items())])


def _render_walkforward():
    """Walk-Forward-Test (analysis/bot_walkforward.py, Phase 7): mehrere
    aufeinanderfolgende, verkettete Out-of-Sample-Fenster desselben
    unveränderten Profils, geprüft gegen das VOR dem ersten Lauf eingefrorene
    GATE_CRITERIA. Auf Knopfdruck wie der einzelne Rücktest - ein Lauf über
    4 Fenster à 45 Tage lädt Monate an 4h- UND 1h-Kerzen und dauert
    entsprechend."""
    from analysis import bot_walkforward

    st.caption("Testet dasselbe, unveränderte Profil auf mehreren aufeinanderfolgenden "
               "Zeitfenstern statt auf einem einzigen langen Zeitraum - eine Strategie kann "
               "im Schnitt gut aussehen, obwohl sie nur in einem einzigen Marktregime "
               "funktioniert. Das Freigabe-Kriterium unten wurde VOR dem ersten Lauf "
               "festgelegt und wird nicht nachträglich an ein Ergebnis angepasst.")

    with st.expander("Freigabe-Kriterium (GATE_CRITERIA)"):
        c = bot_walkforward.GATE_CRITERIA
        st.markdown(
            f"- Mindestens **{c['min_windows']}** auswertbare Fenster, "
            f"**{c['min_trades']}** Trades insgesamt\n"
            f"- Profit-Faktor ≥ **{c['min_profit_factor']}**, Ø R-Multiple > **{c['min_avg_r']}**\n"
            f"- Max. Rückgang < **{c['max_drawdown_pct']}%**, Kosten < "
            f"**{c['max_cost_share_of_gross_profit_pct']}%** des Nettogewinns der Gewinner-Trades\n"
            f"- In mindestens **{c['min_nonnegative_regimes']}** der beobachteten Marktregime "
            "(bull/bär/seitwärts) nicht negativ\n\n"
            "**Alle Kriterien müssen gleichzeitig erfüllt sein.**")

    c1, c2, c3, c4 = st.columns(4)
    window_days = c1.number_input("Fenstergröße (Tage)", 10, 90,
                                  bot_walkforward.DEFAULT_WINDOW_DAYS, key="bot_wf_window_days")
    num_windows = c2.number_input("Anzahl Fenster", 2, 8,
                                  bot_walkforward.DEFAULT_NUM_WINDOWS, key="bot_wf_num_windows")
    level = c3.number_input("Risikostufe", 1, 10, bot_config.risk_level(), key="bot_wf_level")
    if c4.button("Walk-Forward starten", key="bot_wf_run"):
        symbols = bot.candidate_symbols()
        total_days = int(window_days) * int(num_windows) + bot_walkforward._BURN_IN_LOOKBACK_DAYS
        progress = st.progress(0, text="Walk-Forward wird vorbereitet …")
        status = st.empty()

        def _show_progress(phase: str, completed: int, total: int, detail: str):
            fraction = completed / max(int(total), 1)
            if phase == "daten":
                value = int(fraction * 70)
                label = f"Marktdaten ({completed}/{total}): {detail}"
            else:
                value = 70 + int(fraction * 30)
                label = detail
            progress.progress(min(max(value, 0), 100), text=label)
            status.caption("Der Browser kann währenddessen geöffnet bleiben; "
                           "das Ergebnis erscheint automatisch nach dem letzten Fenster.")

        try:
            result = bot_walkforward.run_walkforward(
                symbols, window_days=int(window_days), num_windows=int(num_windows),
                risk_level=int(level), starting_equity_usd=bot.equity_start_usd() or 250.0,
                progress_fn=_show_progress)
        except Exception as exc:
            result = {"error": f"Walk-Forward wurde abgebrochen: {type(exc).__name__}: {exc}"}
        finally:
            progress.empty()
            status.empty()
        st.session_state["bot_walkforward"] = result
        # JEDEN Lauf protokollieren (bestanden UND durchgefallen) - siehe
        # bot_config.log_walkforward_run()'s Docstring: sonst waere nicht
        # nachvollziehbar, wie oft eine andere Fenstergroesse/Risikostufe
        # zuerst durchgefallen ist, bevor eine Kombination bestand.
        bot_config.log_walkforward_run(result, window_days=int(window_days),
                                       num_windows=int(num_windows), risk_level=int(level))

    result = st.session_state.get("bot_walkforward")
    if not result:
        st.caption("Noch kein Walk-Forward-Test gerechnet.")
        return
    if result.get("error"):
        st.error(_md_escape(result["error"]))
        report = result.get("load_report") or {}
        if report.get("skipped_symbols"):
            st.caption(f"Nicht ladbare Symbole: {', '.join(report['skipped_symbols'])}")
        return

    report = result.get("load_report") or {}
    if report:
        st.caption(f"Marktdaten: {report.get('loaded_symbols', 0)} von "
                   f"{report.get('requested_symbols', 0)} Kandidaten auswertbar.")
        if report.get("skipped_symbols"):
            st.caption("Übersprungen: " + ", ".join(
                f"{symbol} ({reason})" for symbol, reason in report["skipped_symbols"].items()))
        if report.get("fetch_notes"):
            st.caption("Unvollständige Zusatzdaten: " + ", ".join(
                f"{symbol} ({reason})" for symbol, reason in report["fetch_notes"].items()))
        completeness_warning = _completeness_warning(report)
        if completeness_warning:
            st.warning(completeness_warning)

    gate = result["gate"]
    if gate["ok"]:
        st.success("✅ Live-Freigabe-Kriterium ERFÜLLT.")
    else:
        st.error("❌ Live-Freigabe-Kriterium NICHT erfüllt.")
        for reason in gate["reasons"]:
            st.caption(f"• {_md_escape(reason)}")

    pooled = gate.get("pooled")
    if pooled:
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Trades gesamt", pooled["trades"])
        p2.metric("Profit-Faktor",
                  f"{pooled['profit_factor']:.2f}" if pooled["profit_factor"] is not None else "—")
        p3.metric("Ø R-Multiple",
                  f"{pooled['avg_r']:+.2f}" if pooled["avg_r"] is not None else "—")
        p4.metric("Max. Rückgang (verkettet)",
                  f"{pooled['max_drawdown_pct']:.1f} %" if pooled["max_drawdown_pct"] is not None else "—")
        cost_txt = (f"{pooled['cost_share_of_gross_profit_pct']:.1f} %"
                   if pooled["cost_share_of_gross_profit_pct"] is not None else "—")
        st.caption(f"Kosten: {cost_txt} des Nettogewinns der Gewinner-Trades · "
                   f"Start {result['starting_equity_usd']:,.2f} \\$ "
                   f"→ Ende {result['ending_equity_usd']:,.2f} \\$")

    windows = result["windows"]
    if result.get("overridden_limit_keys") and windows:
        # Alle Fenster teilen dieselbe Risikostufe/dieselben Limits (ein
        # einzelner Walk-Forward-Lauf, keine Stufen-Variation) - das erste
        # Fenster mit gueltigen Limits genuegt als Referenz.
        ref = next((w for w in windows if "error" not in w), None)
        if ref:
            st.caption(_override_diff_caption(ref["risk_level"], ref["limits"],
                                              result["overridden_limit_keys"]))

    st.markdown("**Fenster**")
    rows = []
    for w in windows:
        if "error" in w:
            rows.append({"Fenster": w["index"], "Zeitraum": f"{w['start']:%d.%m.%y}–{w['stop']:%d.%m.%y}",
                        "Regime": "—", "Trades": "—", "Ergebnis (%)": "—", "Hinweis": w["error"]})
            continue
        m = w["metrics"]
        rows.append({
            "Fenster": w["index"], "Zeitraum": f"{w['start']:%d.%m.%y}–{w['stop']:%d.%m.%y}",
            "Regime": w["regime"], "Trades": m["trades"],
            "Ergebnis (%)": round(m["return_pct"], 1) if m["return_pct"] is not None else None,
            "Hinweis": (next(iter(w.get("blocked_reasons", {})), "") if not m["trades"] else ""),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    by_regime = gate.get("by_regime") or {}
    if by_regime:
        st.markdown("**Nach Marktregime**")
        st.dataframe(pd.DataFrame([
            {"Regime": name, "Fenster": b["windows"], "Netto ($)": round(b["net_pnl_usd"], 2)}
            for name, b in sorted(by_regime.items())
        ]), width="stretch", hide_index=True)

    _render_walkforward_history()


def _render_walkforward_history():
    """Jeder je gerechnete Walk-Forward-Lauf (core.bot_config.
    log_walkforward_run), bestanden UND durchgefallen - sonst waere nicht
    nachvollziehbar, wie oft eine andere Fenstergroesse/Risikostufe zuerst
    durchgefallen ist, bevor eine Kombination bestand (siehe dessen
    Docstring)."""
    runs = db.list_bot_walkforward_runs(limit=20)
    if not runs:
        return
    with st.expander(f"Bisherige Läufe ({len(runs)})"):
        st.dataframe(pd.DataFrame([{
            "Zeitpunkt": clock.local_str(r["created_at"]),
            "Fenster (Tage)": r["window_days"], "Anzahl": r["num_windows"],
            "Risikostufe": r["risk_level"],
            "Ergebnis": "✅ bestanden" if r["gate_ok"] else "❌ durchgefallen",
        } for r in runs]), width="stretch", hide_index=True)


def _render_trade_card(p: dict):
    pnl = p["realized_pnl_usd"] or 0.0
    icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
    ts = clock.local_str(p["closed_at"])
    post = _trade_postmortem(p)
    r_txt = f" · {post['r_multiple']:+.2f} R" if post["r_multiple"] is not None else ""
    st.markdown(f"{icon} **{p['symbol']} {p['side']}** · {pnl:+,.4f} \\${r_txt}")
    st.caption(f"{ts} · Einstand {p['entry_px']:,.2f} \\$ → {p['close_px']:,.2f} \\$")

    with st.expander("Nachbetrachtung"):
        bits = []
        if post["hold_return_pct"] is not None:
            bits.append(f"Kursbewegung in Richtung der Wette: {post['hold_return_pct']:+.2f} %")
        if post["risk_usd"] is not None:
            bits.append(f"Beim Einstieg riskiert: {post['risk_usd']:,.2f} \\$ (1 R)")
        if post["r_multiple"] is not None:
            bits.append(f"Ergebnis in R: {post['r_multiple']:+.2f}")
        if post["fees_usd"]:
            bits.append(f"Gebühren (im Ergebnis bereits abgezogen): {post['fees_usd']:,.4f} \\$")
        if post["hold_hours"] is not None:
            bits.append(f"Haltedauer: {post['hold_hours']:,.1f} h")
        origin = p.get("origin") or "bot"
        bits.append(f"Herkunft: {_ORIGIN_LABEL.get(origin, origin)}")
        if p.get("origin_note"):
            bits.append(_md_escape(str(p["origin_note"])))
        reason = _exit_reason(p)
        if reason:
            bits.append(f"Ausstieg: {reason}")
        if p.get("strategy_version"):
            # Unter welcher Engine-Version/Konfiguration DIESER Trade
            # entstand (core.bot_config.config_fingerprint(), core.bot.
            # open_position()) - fehlte bislang komplett in der Anzeige,
            # obwohl bot_positions es seit Phase 2 mitschreibt.
            version_txt = f"Strategie-Version {p['strategy_version']}"
            if p.get("config_hash"):
                version_txt += f" (Konfig `{p['config_hash']}`)"
            bits.append(version_txt)
        for b in bits:
            st.caption(f"• {b}")


def _render_trade_history():
    positions = db.list_bot_positions(200)
    closed = [p for p in positions if p["status"] == "closed"]
    st.markdown("#### Trade-Historie")
    if not closed:
        st.caption("Noch keine geschlossenen Trades.")
        return
    wins = sum(1 for p in closed if (p["realized_pnl_usd"] or 0) > 0)
    total_pnl = sum(p["realized_pnl_usd"] or 0 for p in closed)
    win_rate = wins / len(closed) * 100
    st.caption(f"{len(closed)} geschlossene Trades · Trefferquote {win_rate:.0f}% · "
               f"realisiert gesamt {total_pnl:+,.2f} $")
    visible, rest = closed[:_TRADES_VISIBLE], closed[_TRADES_VISIBLE:]
    for p in visible:
        _render_trade_card(p)
    if rest:
        with st.expander(f"🗂️ Ältere Trades ({len(rest)})"):
            for p in rest:
                _render_trade_card(p)


def _render_order_card(o: dict):
    label = _INTENT_LABEL.get(o["intent"], o["intent"])
    ts = clock.local_str(o["created_at"])
    status_icon = "✅" if o["status"] == "filled" else ("⛔" if o["status"] == "blocked" else "❌")
    fill_txt = f" · {o['fill_px']:,.2f} \\$" if o.get("fill_px") else ""
    st.markdown(f"{status_icon} **{label}: {o['symbol']}**{fill_txt}")
    detail = f"{ts} · {o['status']}"
    if o.get("fee_usd"):
        detail += f" · Gebühr {o['fee_usd']:.4f} \\$"
    if o.get("exchange_oid"):
        detail += f" · Stop-OID {o['exchange_oid']}"
    st.caption(detail)
    if o.get("error"):
        # Freitext aus core.bot_guards/data.hyperliquid kann selbst '$'
        # enthalten (z.B. "12.34$ ... 56.78$" bei mehreren Guard-Gruenden) -
        # _md_escape() verhindert, dass Streamlit das als LaTeX liest.
        st.caption(f"↳ _{_md_escape(str(o['error'])[:200])}_")


def _render_decision_card(d: dict):
    label = _ACTION_LABEL.get(d["action"], d["action"] or "—")
    ts = clock.local_str(d["created_at"])
    st.markdown(f"{'✅' if d['executed'] else '—'} **{label}**")
    st.caption(f"{ts} · {_md_escape((d['reason'] or '')[:200])}")


def _render_audit_log():
    st.markdown("#### Audit-Trail")
    tab_orders, tab_decisions = st.tabs(["Orders", "Entscheidungen"])
    with tab_orders:
        orders = db.list_bot_orders(200)
        if not orders:
            st.caption("Noch keine Orderversuche.")
        else:
            visible, rest = orders[:_ORDERS_VISIBLE], orders[_ORDERS_VISIBLE:]
            for o in visible:
                _render_order_card(o)
            if rest:
                with st.expander(f"🗂️ Ältere Orders ({len(rest)})"):
                    for o in rest:
                        _render_order_card(o)
    with tab_decisions:
        decisions = db.list_bot_decisions(200)
        if not decisions:
            st.caption("Noch keine Entscheidungen.")
        else:
            visible, rest = decisions[:_DECISIONS_VISIBLE], decisions[_DECISIONS_VISIBLE:]
            for d in visible:
                _render_decision_card(d)
            if rest:
                with st.expander(f"🗂️ Ältere Entscheidungen ({len(rest)})"):
                    for d in rest:
                        _render_decision_card(d)


_RISK_LEVEL_LABELS = {1: "sehr zurückhaltend", 2: "sehr zurückhaltend",
                      3: "zurückhaltend", 4: "zurückhaltend",
                      5: "ausgewogen", 6: "ausgewogen",
                      7: "risikofreudig", 8: "risikofreudig",
                      9: "sehr risikofreudig", 10: "sehr risikofreudig"}

# Anzeige-Formatierung der vom Regler abgeleiteten (Risiko-)Limits. Die
# Beschriftungen selbst stehen in core.bot_config.LIMIT_LABELS, damit ein
# neuer Anker-Schluessel nicht an zwei Stellen gepflegt werden muss. Enthaelt
# bewusst NUR Risiko-Groessen (core.bot_config._RISK_ANCHORS) - Strategie-
# Parameter stehen getrennt in _STRATEGY_ROWS, weil sie sich mit DIESEM
# Regler nicht mehr aendern (siehe core/bot_config.py-Modul-Docstring).
_PREVIEW_ROWS = (
    ("risk_per_trade_pct", lambda v: f"{v:.2f} %"),
    ("max_position_pct", lambda v: f"{v:.0f} %"),
    ("max_total_exposure_pct", lambda v: f"{v:.0f} %"),
    ("max_portfolio_heat_pct", lambda v: f"{v:.1f} %"),
    ("max_leverage", lambda v: f"{v:g}x"),
    ("max_positions", lambda v: f"{v:.0f}"),
    ("max_same_side_positions", lambda v: f"{v:.0f}"),
    ("daily_loss_limit_pct", lambda v: f"{v:.0f} %"),
    ("equity_floor_pct", lambda v: f"{v:.0f} %"),
)

# Feste Strategie-Parameter (core.bot_signals ueber core.bot_config.
# STRATEGY_LABELS/_strategy_constants()) - AUSSERHALB des Reglers, aendern
# sich mit KEINER Slider-Stellung. Eigene Tabelle, damit die Zahlen nicht
# faelschlich als "von der Risikostufe abhaengig" erscheinen wie vor diesem
# Umbau (CLAUDE.md-Feedback: bei Stufe 9 lief die Engine mit abgesenkter
# Schwelle UND engerem Stop UND kuerzerem Cooldown, nicht nur mit groesseren
# Positionen).
_STRATEGY_ROWS = (
    ("atr_stop_mult", lambda v: f"{v:g} × ATR"),
    ("max_hold_hours", lambda v: f"{v:.0f} h"),
    ("symbol_cooldown_hours", lambda v: f"{v:.0f} h"),
    ("max_trades_per_day", lambda v: f"{v:.0f}"),
    ("loss_streak_limit", lambda v: f"{v:.0f} Verlierer in Folge"),
)

# Die Limits, die direkt unter dem Regler stehen - der Rest wandert in den
# Aufklapper, damit die Vorschau keine Zahlenwand wird.
_CORE_LIMIT_KEYS = {"risk_per_trade_pct", "max_position_pct", "max_total_exposure_pct",
                    "max_leverage", "daily_loss_limit_pct"}


def _render_risk_slider():
    st.markdown("#### Risikobereitschaft")
    st.caption("Steuert ausschließlich Positionsgröße, Hebel, Exposure und Verlustgrenzen - "
               "nie Signalschwelle, Stop-Abstand, Haltedauer oder Cooldown (siehe unten).")
    current = bot_config.risk_level()
    overridden = bot_config.overridden_keys()
    if overridden:
        st.info("Experten-Overrides aktiv für: "
                f"{', '.join(bot_config.LIMIT_LABELS.get(k, k) for k in overridden)}.")

    level = st.slider("Risikobereitschaft (1 = vorsichtig, 10 = aggressiv)", 1, 10, current,
                      key="bot_risk_slider")
    st.caption(f"Stufe {level}: {_RISK_LEVEL_LABELS[level]}.")
    # `preview` ist die REINE Stufen-Rechnung (derive_limits), `effective`
    # das, was bei "Risikostufe übernehmen" tatsächlich gelten würde
    # (limits_for_risk_level() legt gespeicherte Experten-Overrides
    # darüber). Vorher zeigte diese Vorschau AUSSCHLIESSLICH `preview` an -
    # bei aktiven Overrides (wie sie diese Session am Live-Konto vorfand:
    # max_same_side_positions=1 unabhängig von der Stufe) verschob der
    # Regler sichtbar Zahlen, die real längst überschrieben waren, ohne
    # dass das an der einzelnen Kennzahl erkennbar war - nur der pauschale
    # Hinweis oben nannte die BETROFFENEN Schlüssel, nicht den Unterschied.
    preview = bot_config.derive_limits(level)
    effective = bot_config.limits_for_risk_level(level)
    equity = (bot.performance_summary() or {}).get("current_usd")
    check = bot_config.feasibility(equity or 0.0, effective) if equity else None

    def _metric_value(key, fmt):
        if key in overridden and effective[key] != preview[key]:
            return f"{fmt(preview[key])} → {fmt(effective[key])} (Override)"
        return fmt(preview[key])

    core_rows = [row for row in _PREVIEW_ROWS if row[0] in _CORE_LIMIT_KEYS]
    more_rows = [row for row in _PREVIEW_ROWS if row[0] not in _CORE_LIMIT_KEYS]
    cols = st.columns(len(core_rows))
    for i, (key, fmt) in enumerate(core_rows):
        cols[i].metric(bot_config.LIMIT_LABELS[key], _metric_value(key, fmt))
    with st.expander("Weitere abgeleitete Limits"):
        more_cols = st.columns(4)
        for i, (key, fmt) in enumerate(more_rows):
            more_cols[i % 4].metric(bot_config.LIMIT_LABELS[key], _metric_value(key, fmt))

    # Warnt, wenn die Equity bei dieser Stufe unter die Hyperliquid-
    # Mindestordergroesse faellt - sonst handelt der Bot still gar nicht.
    if check and not check["ok"]:
        st.warning(_md_escape(check["hint"]))
    elif check:
        st.caption(f"Bei aktueller Equity ({equity:,.2f} \\$) erlaubt diese Stufe bis zu "
                   f"{check['max_notional_usd']:,.2f} \\$ je Position.")

    if st.button("Risikostufe übernehmen", type="primary", key="bot_risk_apply",
                 disabled=level == current and not overridden):
        bot_config.save_risk_level(level)
        st.success(f"Risikostufe {level} aktiv.")
        st.rerun()

    with st.expander("Feste Strategie-Parameter (unabhängig vom Regler)"):
        st.caption("Ändert sich nur per Code-Änderung (core/bot_signals.py), nie über diese "
                   "Seite - siehe core.bot_config.config_fingerprint() für den Nachweis, unter "
                   "welcher Konfiguration ein bestimmter Trade tatsächlich entstand.")
        strategy = bot_config.strategy_constants()
        strategy_cols = st.columns(3)
        for i, (key, fmt) in enumerate(_STRATEGY_ROWS):
            strategy_cols[i % 3].metric(bot_config.STRATEGY_LABELS[key], fmt(strategy[key]))


def _render_limits():
    _render_risk_slider()
    with st.expander("Expertenmodus: einzelne Risiko-Limits überschreiben"):
        limits = bot_config.bot_limits()
        st.caption("Nur ausdrücklich gesetzte Werte weichen vom Regler ab; Hebel bleibt "
                   "immer auf 2x begrenzt. Nur Risiko-Größen - Strategie-Parameter (oben) "
                   "sind hier bewusst nicht wählbar.")
        with st.form("bot_limits_form"):
            c1, c2, c3 = st.columns(3)
            max_position = c1.number_input("Max. Position (%)", 1.0, 100.0,
                                           float(limits["max_position_pct"]), 1.0)
            risk_per_trade = c2.number_input("Risiko je Trade (%)", 0.05, 5.0,
                                             float(limits["risk_per_trade_pct"]), 0.05)
            max_exposure = c3.number_input("Max. Gesamt-Exposure (%)", 5.0, 300.0,
                                           float(limits["max_total_exposure_pct"]), 5.0)
            daily_loss = c1.number_input("Tagesverlust-Limit (%)", 1.0, 50.0,
                                         float(limits["daily_loss_limit_pct"]), 1.0)
            max_positions = c2.number_input("Max. Positionen", 1, 20,
                                            int(limits["max_positions"]), 1)
            max_same_side = c3.number_input("Max. gleichgerichtete Positionen", 1, 20,
                                            int(limits["max_same_side_positions"]), 1)
            save = st.form_submit_button("Overrides speichern")
        if save:
            saved = bot_config.save_bot_limits(
                max_position_pct=max_position, risk_per_trade_pct=risk_per_trade,
                max_total_exposure_pct=max_exposure, daily_loss_limit_pct=daily_loss,
                max_positions=max_positions, max_same_side_positions=max_same_side)
            st.success(f"Overrides gespeichert. Max. Hebel: {saved['max_leverage']:.0f}x · "
                       f"Equity-Boden: {saved['equity_floor_pct']:.0f} %.")
            st.rerun()
        if bot_config.overridden_keys():
            if st.button("Alle Overrides entfernen (zurück zum reinen Regler)",
                         key="bot_clear_overrides"):
                bot_config.clear_overrides()
                st.success("Overrides entfernt.")
                st.rerun()


def _render_reset(state: dict):
    with st.expander("⚠️ Bot-Experiment zurücksetzen"):
        st.caption("Löscht Positionen, Audit-Trail, Equity-Verlauf, Kosten und "
                   "Kill-Switch – Runner vorher beenden.")
        confirm = st.checkbox("Ich habe den Runner beendet und möchte alle Bot-Daten löschen.",
                              key="bot_reset_confirm")
        if st.button("Bot-Experiment zurücksetzen", disabled=state["running"] or not confirm,
                     key="bot_reset"):
            bot.reset()
            st.success("Bot-Experiment zurückgesetzt.")
            st.rerun()
