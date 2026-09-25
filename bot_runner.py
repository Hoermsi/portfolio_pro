"""Trading-Bot: eigenständiger Prozess, KEIN Streamlit-Import.

Die App selbst hat keinen Scheduler (Snapshots entstehen nur, wenn jemand
das Dashboard öffnet) - für einen Bot, der auch nachts auf einen Stop
reagieren muss, reicht das nicht. Dieses Skript läuft unabhängig vom
Streamlit-Server als eigener Prozess, gestartet über "Start Trading Bot.bat".

Dry-Run ist der Standard (core.bot_config.dry_run_enabled() == True) - dann
handelt dieses Skript gegen eine PaperExchange, selbst wenn Live-Zugangsdaten
in der Umgebung stehen. LiveExchange darf erst nach dem vorgeschriebenen
Scharfschuss-Test (Phase F) und einer bewussten Konfigurationsänderung genutzt
werden.

Die Börsenanbindung (Paper wie Live) wird EINMAL vor der Schleife gebaut, nicht
pro Zyklus: PaperExchange hält ihren Zustand (Positionen, Cash) nur im
Prozessspeicher, nicht in der DB - ein Neuaufbau pro Zyklus würde bei jedem
Tick ein leeres Depot erzeugen. Ein Neustart des GESAMTEN Prozesses (Absturz,
Neustart des Rechners) beginnt die Papier-Simulation dagegen bewusst neu bei
core.bot.start_info()'s eingefrorenem Startkapital - für LiveExchange ist das
irrelevant, dort ist die Börse selbst der einzige Zustand.
"""
import json
import msvcrt
import os
import sys
import time
from datetime import datetime, timedelta

from core import bot, bot_config, bot_universe, clock, config, db, notify
from data.hyperliquid import LiveExchange, PaperExchange
from data.hyperliquid import Position as _HLPosition

CYCLE_SECONDS = 15 * 60
_SLEEP_CHUNK_SECONDS = 5
_PAPER_STARTING_EQUITY_USD = 250.0
_LAST_LLM_REVIEW_META_KEY = "bot_last_llm_review_at"

# --- SINGLETON-SPERRE ---
#
# core.bot_process.start()s PID-Pruefung ist eine reine UI-Vorabpruefung
# (check-then-act, nicht atomar) und greift ausserdem nicht, wenn dieses
# Skript direkt ueber "Start Trading Bot.bat" ohne die UI gestartet wird -
# zwei fast gleichzeitige Starts koennten beide die PID-Pruefung passieren
# und doppelt scannen/handeln. Diese Sperre haelt der Prozess SELBST, direkt
# hier, unabhaengig davon, wie er gestartet wurde. `msvcrt.locking()` ist
# eine Betriebssystem-Sperre auf einen offenen Datei-Handle - stirbt der
# haltende Prozess (Absturz, harter Kill), schliesst Windows den Handle
# automatisch und gibt die Sperre frei, kein Aufraeum-Code noetig fuer diesen
# Fall. `_lock_handle` haelt die Referenz bewusst als Modul-Global: ein
# Garbage-Collect des Handles wuerde die Sperre sonst vorzeitig freigeben.
_LOCK_PATH = config.DATA_DIR / "bot_runner.lock"
_lock_handle = None


def _acquire_singleton_lock(lock_path=_LOCK_PATH) -> bool:
    """True, wenn die Sperre erfolgreich exklusiv erworben wurde - False,
    wenn ein anderer Prozess sie bereits haelt (kein Doppelstart)."""
    global _lock_handle
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        lock_path.write_bytes(b"0")
    try:
        handle = open(lock_path, "r+b")
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    _lock_handle = handle
    return True


def _release_singleton_lock():
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        _lock_handle.seek(0)
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    _lock_handle.close()
    _lock_handle = None
_LAST_COST_REFRESH_META_KEY = "bot_last_cost_refresh_date"
# Kapitalfluesse haben ein EIGENES, engeres Gate als Gebuehren/Funding (siehe
# _maybe_refresh_flows): sie sind sicherheitsrelevant (adjusted_start_usd()
# -> Equity-Boden), Gebuehren/Funding sind reine Anzeige-/Nachvollzieh-
# barkeits-Werte. Ein gemeinsames Tages-Gate liess einen erfolgreichen
# Kosten-Abruf faelschlich auch einen fehlgeschlagenen Kapitalfluss-Abruf
# fuer den Rest des Tages als "erledigt" gelten.
_LAST_FLOWS_REFRESH_META_KEY = "bot_last_flows_refresh_at"
_FLOWS_REFRESH_INTERVAL_HOURS = 1
_STOP_REQUESTED_META_KEY = "bot_runner_stop_requested"
_CLOSE_ALL_REQUESTED_META_KEY = "bot_close_all_requested"
_LAST_ERROR_META_KEY = "bot_runner_last_error"

# Tagesdeckel fuer den KI-Bericht. Der Review-Takt kommt aus dem
# Risikoregler (bis zu alle 2 h bei Stufe 10) - ohne Deckel koennte eine
# ungluecklich gesetzte Stufe die Kosten unbemerkt vervielfachen.
_LLM_DAILY_COST_CAP_USD = 0.50


# Zeitfenster, nach denen ein protokolliertes Signal nachtraeglich bewertet
# wird. Bewusst mehrere: ein Signal, das nach 6 h gut und nach 96 h schlecht
# aussieht, sagt etwas anderes als eines, das durchgehend laeuft.
_OUTCOME_HORIZONS_H = (6, 12, 24, 48, 96)
# Deckel je Takt. Der Nachtrag ist Buchhaltung, kein Handel - er darf den
# 15-Minuten-Takt nie aufhalten.
_OUTCOME_ROWS_PER_TICK = 200


def _log(message: str):
    # Lokale Wanduhr fuers Konsolen-Log (Operator-Komfort), aber mit
    # explizitem Offset (astimezone()) - unverwechselbar von den UTC-
    # Zeitstempeln in der DB, die dieselbe Uhrzeit doppeldeutig aussehen
    # liessen, wenn hier ein nackter naiver Wert stuende.
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def _heartbeat(mode: str):
    """Kleiner DB-Herzschlag für die Statusanzeige in Streamlit.

    Er enthält bewusst weder Zugangsdaten noch Kontostände. Ein alter Zeitwert
    bedeutet für die UI nur "Prozess nicht mehr nachweisbar", nie "sicher
    beendet" - deshalb bleibt der Kill-Switch dort jederzeit erreichbar.
    """
    db.set_meta("bot_runner_heartbeat", clock.iso_utc())
    db.set_meta("bot_runner_mode", mode)


def _stop_requested() -> bool:
    return db.get_meta(_STOP_REQUESTED_META_KEY) == "1"


def _close_all_requested() -> bool:
    return db.get_meta(_CLOSE_ALL_REQUESTED_META_KEY) == "1"


def _sleep_with_stop_check(total_seconds: int) -> str | None:
    """Schläft in kleinen Schritten statt am Stück (core.bot_process.
    request_stop() aus der UI, Teil 2 des Plans), damit ein Stop-Wunsch
    innerhalb weniger Sekunden greift statt bis zu 15 Minuten zu brauchen.
    Reagiert aus demselben Grund auch auf eine "alle Positionen schließen"-
    Anfrage (_close_all_requested) - beide sollen den Bot so schnell wie
    möglich aus dem Schlaf holen.

    Gibt den GRUND zurück ("stop"/"close_all") statt eines bloßen bool -
    eine frühere Fassung gab nur True/False zurück und verließ sich darauf,
    dass der Aufrufer den Grund selbst erneut prüft. Das passierte nie: der
    Aufrufer behandelte jeden vorzeitigen Abbruch als Stop-Wunsch und beendete
    den Prozess, auch wenn nur "Alle Positionen schließen" gedrückt wurde -
    mit allen Positionen weiterhin offen und dem Flag weiterhin gesetzt,
    während die UI bereits Vollzug meldete. Ein reiner Schliessungs-Wunsch
    darf den Runner nicht beenden, weil NUR dieser Prozess die aktive Börsen-
    /Paper-Verbindung hält (siehe _maybe_close_all) - run_forever() prüft
    diesen Fall danach selbst noch einmal per erneutem _close_all_requested(),
    dieser Rückgabewert ist nur die Aufwach-Ursache."""
    elapsed = 0
    while elapsed < total_seconds:
        if _stop_requested():
            return "stop"
        if _close_all_requested():
            return "close_all"
        if bot.manual_stop_requests():
            return "manual_stop"
        if bot.close_position_requests():
            return "close_position"
        time.sleep(min(_SLEEP_CHUNK_SECONDS, total_seconds - elapsed))
        elapsed += _SLEEP_CHUNK_SECONDS
    return None


def _maybe_close_all(exchange, runner_mode: str = "paper"):
    """Reagiert auf den manuellen "Alle Positionen schließen"-Knopf
    (views/trading_bot.py) - als Meta-Flag statt eines direkten Aufrufs aus
    der UI, weil NUR dieser Prozess die aktive Börsen-/Paper-Verbindung
    hält (Moduldoc oben: PaperExchange-Zustand lebt ausschließlich hier im
    Prozessspeicher, ein UI-seitig frisch gebautes Exchange-Objekt kennt die
    offenen Paper-Positionen gar nicht).

    Das Flag wird erst gelöscht, wenn db.list_open_bot_positions() DANACH
    tatsächlich leer ist - nicht schon nach dem blossen Versuch. Vorher
    löschte diese Funktion das Flag UNBEDINGT, auch wenn eine Schliessung
    mit "blocked"/"error" scheiterte oder nur teilweise gefüllt wurde
    (close_position()'s "partial"-Status) - die Anfrage verschwand dann
    spurlos, mit Positionen weiterhin offen und der UI ohne jeden Hinweis,
    dass "Alle Positionen schließen" real nichts erreicht hatte. Gleiches
    "nur bei Erfolg voranschreiten"-Muster wie _maybe_refresh_costs()'s
    Tages-Gate: ein fehlgeschlagener Versuch lässt den nächsten 15-Minuten-
    Takt automatisch erneut versuchen.

    `runner_mode` als Default-Parameter (nicht Pflicht) - bestehende Tests
    rufen diese Funktion ohne zweites Argument auf und bleiben so
    unverändert lauffähig; der reale Aufruf in der Zyklus-Schleife übergibt
    den echten Modus, damit Telegram-Benachrichtigungen (core/notify.py) im
    Dry-Run stumm bleiben (Nutzerentscheidung: nur Live-Trades benachrichtigen)."""
    if not _close_all_requested():
        return
    _log("Manuelle Anfrage: alle offenen Positionen schließen.")
    for r in bot.close_all_positions(exchange, reason="manual_close_all"):
        _log(f"Geschlossen: {r['symbol']} -> {r['result'].get('status')} "
             f"{r['result'].get('reason') or ''}")
        if runner_mode == "live" and r["result"].get("status") == "filled":
            pnl = r["result"].get("realized_pnl_usd")
            pnl_txt = f"\nPnL: {pnl:+,.2f} $" if pnl is not None else ""
            notify.send(f"🚪 Manuell geschlossen: {r['symbol']}{pnl_txt}")
    remaining = db.list_open_bot_positions()
    if remaining:
        symbols = [p["symbol"] for p in remaining]
        _log(f"WARNUNG: Alle-Positionen-schließen bleibt angefordert - {len(remaining)} "
             f"Position(en) weiterhin offen: {symbols}. Nächster Takt versucht erneut.")
    else:
        db.delete_meta(_CLOSE_ALL_REQUESTED_META_KEY)
        _log("Alle offenen Positionen erfolgreich geschlossen.")


_BACKFILL_OPENED_AT_REQUESTED_META_KEY = "bot_backfill_opened_at_requested"


def _maybe_backfill_exchange_opened_at(exchange):
    """Reagiert auf den manuellen "exchange_opened_at nachtragen"-Knopf
    (views/trading_bot.py) - genau wie _maybe_close_all() als Meta-Flag statt
    eines direkten Aufrufs aus der UI, aus demselben Grund: nur dieser
    Prozess haelt die aktive Boersen-Verbindung. Einmaliger, manuell
    ausgeloester Nachtrag fuer VOR core.bot.backfill_exchange_opened_at()
    uebernommene Positionen (siehe deren Docstring) - kein Teil des
    regulaeren Takts, weil eine Fill-Historien-Abfrage je betroffenes Symbol
    nicht in jeden 15-Minuten-Zyklus gehoert."""
    if db.get_meta(_BACKFILL_OPENED_AT_REQUESTED_META_KEY) != "1":
        return
    _log("Manuelle Anfrage: exchange_opened_at für übernommene Positionen nachtragen.")
    for r in bot.backfill_exchange_opened_at(exchange):
        if r["updated"]:
            _log(f"{r['symbol']}: exchange_opened_at auf {r['exchange_opened_at']} gesetzt.")
        else:
            _log(f"{r['symbol']}: nicht ermittelbar - {r.get('reason', '')}")
    db.delete_meta(_BACKFILL_OPENED_AT_REQUESTED_META_KEY)


def _maybe_apply_manual_stops(exchange, runner_mode: str = "paper"):
    """Reagiert auf den "Stop nachziehen"-Knopf je Position (views/
    trading_bot.py) - dasselbe Meta-Flag-Muster wie _maybe_close_all(), aus
    demselben Grund: nur dieser Prozess haelt die Boersen-Verbindung. Die
    Logik selbst liegt in core.bot.process_manual_stop_requests()."""
    for r in bot.process_manual_stop_requests(exchange):
        _log(f"Manueller Stop {r['symbol']}: {r['status']} - {r.get('reason') or ''}")
        if runner_mode == "live":
            icon = "🎯" if r["status"] == "ok" else "⚠️"
            notify.send(f"{icon} Manueller Stop: {r.get('reason') or r['symbol']}")


def _maybe_close_requested_positions(exchange, runner_mode: str = "paper"):
    """Reagiert auf den "Position schließen"-Knopf je Position (views/
    trading_bot.py, "Positionen bearbeiten") - dasselbe Meta-Flag-Muster wie
    _maybe_close_all(), nur für EIN Symbol statt aller offenen Positionen.
    Die Logik (inkl. Retry bei Fehlschlag) liegt in core.bot.
    process_close_position_requests()."""
    for r in bot.process_close_position_requests(exchange):
        _log(f"Manuell geschlossen: {r['symbol']} -> {r.get('status')} {r.get('reason') or ''}")
        if runner_mode == "live" and r.get("status") == "filled":
            pnl = r.get("realized_pnl_usd")
            pnl_txt = f"\nPnL: {pnl:+,.2f} $" if pnl is not None else ""
            notify.send(f"🚪 Manuell geschlossen: {r['symbol']}{pnl_txt}")


def _clear_runner_markers():
    """Bei einem SAUBEREN Stop (angefordert über die UI) Heartbeat/PID sofort
    löschen, statt die UI bis zu 35 Minuten "vermutlich noch aktiv" zeigen zu
    lassen. Bei einem Absturz/Kill von außen läuft das bewusst NICHT - die
    Alterserkennung über den zuletzt bekannten Heartbeat bleibt dafür der
    einzig verlässliche Signalweg (core.bot_process, Teil 2 des Plans)."""
    db.delete_meta("bot_runner_heartbeat")
    db.delete_meta("bot_runner_pid")
    db.delete_meta(_STOP_REQUESTED_META_KEY)


def _log_open_positions(exchange):
    """Kompakte Positionsübersicht bei JEDEM Takt im Runner-Log.

    Wer das Konsolenfenster beobachtet ("Start Trading Bot.bat"), soll auf
    einen Blick sehen, was gerade gehalten wird - Seite, Größe, Hebel,
    Einstieg, uPnL - ohne zusätzlich die Streamlit-Oberfläche zu öffnen. Nutzt
    `account_state()` statt der DB-Rohdaten, weil die Position dort bereits
    den aktuellen unrealisierten Gewinn/Verlust mitführt (kein zweiter
    Kursabruf nötig).
    """
    positions = exchange.account_state().positions
    if not positions:
        _log("Keine offenen Positionen.")
        return
    for p in positions:
        _log(f"Position {p.symbol}: {p.side.upper()} · Größe {p.size:.6g} · Hebel {p.leverage:g}x · "
             f"Einstieg {p.entry_px:,.4f} $ · uPnL {p.unrealized_pnl_usd:+,.4f} $")


def _maybe_refresh_costs(exchange):
    """Einmal täglich die laufenden Kosten-Schätzungen durch die ECHTEN
    Werte von der Börse ersetzen (core.bot.refresh_live_costs) - siehe dort
    für die Begründung, warum eine Schätzung allein nicht reicht. NUR
    Gebühren/Funding - Kapitalflüsse haben ihr eigenes, engeres Gate
    (_maybe_refresh_flows unten), weil sie sicherheitsrelevant sind und
    Gebühren/Funding nicht.

    Das Tages-Gate wird NUR bei Erfolg gesetzt - ODER wenn die Börse
    ohnehin keine Live-Fähigkeit hat (PaperExchange: `refresh_live_costs`
    liefert per Duck-Typing-Miss immer False, ein täglicher Versuch bliebe
    sonst für immer erfolglos). Bei LiveExchange darf ein VORÜBERGEHENDER
    Ausfall (Netzwerk, Rate-Limit) nicht den einzigen Versuch des Tages
    verbrauchen - ohne das Gate wird beim nächsten 15-Minuten-Takt erneut
    versucht."""
    today = clock.now_utc().date().isoformat()
    if db.get_meta(_LAST_COST_REFRESH_META_KEY) == today:
        return
    if bot.refresh_live_costs(exchange):
        _log("Echte Gebühren/Funding von der Börse übernommen.")
        db.set_meta(_LAST_COST_REFRESH_META_KEY, today)
    elif getattr(exchange, "recent_fills", None) is None:
        db.set_meta(_LAST_COST_REFRESH_META_KEY, today)   # keine Live-Faehigkeit, kein Nutzen im Wiederholen
    else:
        _log("Gebühren-/Funding-Abgleich fehlgeschlagen - nächster Takt versucht erneut.")


_UNIVERSE_REFRESH_REQUESTED_META_KEY = "bot_universe_refresh_requested"


def _maybe_refresh_universe():
    """Einmal täglich den dynamischen Kandidaten-Pool (core.bot_universe)
    neu ermitteln - gleiches Gating-Muster wie _maybe_refresh_costs: Gate
    rückt nur bei Erfolg vor, ein Fehlschlag (CoinGecko down) lässt den
    nächsten Takt sofort erneut versuchen statt einen vollen Tag zu warten.
    Reine Anzeige-/Auswahlgrundlage, nicht sicherheitsrelevant - deshalb
    (anders als _maybe_refresh_flows) nur täglich, nicht stündlich.

    Reagiert zusätzlich auf den manuellen "Pool jetzt aktualisieren"-Knopf
    (views/trading_bot.py) über ein Meta-Flag - dasselbe Muster wie
    _maybe_close_all(): nur dieser Prozess soll den (rate-limitierten)
    CoinGecko-Abruf auslösen, nicht ein UI-Klick direkt aus der
    Streamlit-Session heraus."""
    force = db.get_meta(_UNIVERSE_REFRESH_REQUESTED_META_KEY) == "1"
    try:
        result = bot_universe.refresh_pool(force=force)
    except Exception as e:
        _log(f"Kandidaten-Pool-Aktualisierung fehlgeschlagen: {e}")
        return
    finally:
        if force:
            db.delete_meta(_UNIVERSE_REFRESH_REQUESTED_META_KEY)
    if result.get("skipped"):
        return
    _log(f"Kandidaten-Pool aktualisiert: {len(result['symbols'])} Symbole "
        f"qualifiziert, {result['rejected_count']} abgelehnt.")


def _maybe_refresh_flows(exchange, now: datetime | None = None):
    """Ein-/Auszahlungen deutlich haeufiger als Gebühren aktualisieren
    (core.bot.refresh_live_flows) - STÜNDLICH statt täglich, mit einem
    EIGENEN Gate. Kapitalflüsse sind sicherheitsrelevant
    (adjusted_start_usd() -> Equity-Boden, check_equity_floor): ein
    Tagesabstand ließ eine Auszahlung bis zu 24 h lang wie einen starken
    Handelsverlust aussehen. Aufgerufen VOR jedem Zyklus (also vor jeder
    Equity-Boden-Prüfung), macht bei Erfolg oder fehlender Live-Fähigkeit
    aber selbst nur stündlich tatsächlich etwas."""
    now = now or clock.now_utc()
    # clock.parse_utc() statt datetime.fromisoformat(): ein Meta-Wert aus der
    # Zeit VOR core/clock.py (Phase 1) ist tz-NAIV und liess diese Zeile bei
    # JEDEM Takt mit "can't subtract offset-naive and offset-aware datetimes"
    # abstuerzen - core.bot_process.clear_bot_state() raeumt diesen
    # Schluessel nicht (siehe core/db.py), ein reines "Bot-Experiment
    # zuruecksetzen" liess den alten Wert also unbemerkt liegen.
    last_refresh = clock.parse_utc(db.get_meta(_LAST_FLOWS_REFRESH_META_KEY))
    if last_refresh is not None:
        if (now - last_refresh).total_seconds() < _FLOWS_REFRESH_INTERVAL_HOURS * 3600:
            return
    if bot.refresh_live_flows(exchange):
        _log("Ein-/Auszahlungen von der Börse übernommen.")
        db.set_meta(_LAST_FLOWS_REFRESH_META_KEY, now.isoformat(timespec="seconds"))
    elif getattr(exchange, "deposit_withdrawal_usd", None) is None:
        db.set_meta(_LAST_FLOWS_REFRESH_META_KEY, now.isoformat(timespec="seconds"))
    else:
        _log("Kapitalfluss-Abgleich fehlgeschlagen - nächster Takt versucht erneut.")


def _llm_review_due() -> bool:
    """Fällig nach dem in den Bot-Einstellungen gewählten Berichtsintervall
    (core.bot_config.ai_report_interval_hours(), 6/12/24/48/168 h) -
    unabhängig vom Risikoregler, denn der KI-Bericht ist reine
    Berichterstattung ohne Einfluss auf eine einzige Handelsentscheidung.

    Vorher war das ein reiner Kalendertag-Vergleich: EIN Aufruf pro Tag, und
    weil das die einzige handelnde Instanz war, gab es auch nur eine
    Entscheidungsgelegenheit pro Tag. Seit die Regel-Engine handelt, ist der
    Takt hier nur noch der Rhythmus der Aufsicht - deshalb darf er feiner und
    frei wählbar sein.
    """
    hours = bot_config.ai_report_interval_hours()
    last = clock.parse_utc(db.get_meta(_LAST_LLM_REVIEW_META_KEY))
    if last is None:
        return True
    return (clock.now_utc() - last).total_seconds() >= hours * 3600


def _llm_budget_left_usd() -> float:
    """Was heute noch fuer den KI-Bericht ausgegeben werden darf."""
    today = clock.now_utc().date().isoformat()
    if db.get_meta("bot_llm_cost_day") != today:
        return _LLM_DAILY_COST_CAP_USD
    spent = float(db.get_meta("bot_llm_cost_usd") or 0.0)
    return max(0.0, _LLM_DAILY_COST_CAP_USD - spent)


def _record_llm_cost(cost_usd: float):
    today = clock.now_utc().date().isoformat()
    spent = 0.0 if db.get_meta("bot_llm_cost_day") != today else float(
        db.get_meta("bot_llm_cost_usd") or 0.0)
    db.set_meta("bot_llm_cost_day", today)
    db.set_meta("bot_llm_cost_usd", str(spent + max(0.0, cost_usd)))


def _maybe_run_llm_review(exchange, cycle_result: dict):
    """KI-BERICHT (reine Berichterstattung, keine Entscheidung): läuft im Regler-Takt sowie
    zusätzlich sofort nach einem ausgelösten Stop.

    Ohne ANTHROPIC_API_KEY läuft der Bot vollständig regelbasiert weiter -
    Signal-Engine, Guards und Stop-Kontrolle brauchen kein Modell. Das ist
    kein Fehler und keine Blockade, nur eine fehlende zweite Meinung.
    """
    if not config.anthropic_api_key():
        return
    if not bot_config.ai_report_enabled():
        return
    from agents import trader

    due = _llm_review_due()
    stop_triggered = cycle_result["stop_events"] and not cycle_result["kill_switch_active"]
    if not due and not stop_triggered:
        return

    budget = _llm_budget_left_usd()
    if budget <= 0:
        _log(f"KI-Bericht heute übersprungen: Tagesbudget von "
             f"{_LLM_DAILY_COST_CAP_USD:.2f} $ ausgeschöpft.")
        return

    model = config.DEFAULT_SENIOR_MODEL if due else config.DEFAULT_SPECIALIST_MODEL
    _log("KI-Bericht startet ..." if due else
         "Stop wurde ausgelöst - hole ereignisgetriggerten KI-Bericht ...")
    result = trader.run_trading_cycle(exchange, model)
    _record_llm_cost(float(result.get("total_cost_usd") or 0.0))
    if result.get("error"):
        _log(f"KI-Bericht fehlgeschlagen: {result['error']}")
        return
    # Nur bei Erfolg vormerken - ein transienter API-Fehler soll beim naechsten
    # Takt erneut versucht werden, nicht erst nach dem vollen Intervall.
    if due:
        db.set_meta(_LAST_LLM_REVIEW_META_KEY, clock.iso_utc())
    _log(f"KI-Bericht abgeschlossen: {len(result.get('post_mortems', []))} Post-Mortems, "
         f"${result.get('total_cost_usd', 0):.4f}.")


def _signal_outcome(row: dict, df, horizon_h: int, now: datetime | None = None) -> dict | None:
    """Wie WAERE dieses Signal ueber `horizon_h` Stunden gelaufen?

    Bewusst auch fuer uebersprungene und blockierte Signale (sowie, aus der
    Zeit vor Phase 8, historische 'ai_veto'-Zeilen - die KI kann seither
    nichts mehr sperren, siehe agents/trader.py) - genau daraus entstehen die
    "Fehler der Untaetigkeit", die sonst nirgends sichtbar werden. Kosten
    werden geschaetzt (Round-Trip aus
    Taker-Gebuehr, Slippage UND geschaetztem Funding ueber den Horizont),
    nicht gemessen: der Trade hat ja nie stattgefunden.

    ZEITLICHE SAUBERKEIT (Start): Das Fenster startet beim `ts` (dem ECHTEN,
    auf die Sekunde genauen Entscheidungszeitpunkt) auf die naechste volle
    Stunde AUFGERUNDET - nicht bei `candle_ts` selbst und auch nicht erst bei
    dessen Nachfolgekerze. `candle_ts` ist die zum Entscheidungszeitpunkt
    bereits ABGESCHLOSSENE Kerze; ihr High/Low lag vollstaendig VOR der
    Entscheidung und darf nie einfliessen. Aber auch die UNMITTELBAR folgende
    Kerze ist zum Entscheidungszeitpunkt selbst noch am LAUFEN: bei einer
    Entscheidung um 00:31 waere das die 00:00-01:00-Kerze, deren erste
    31 Minuten ebenfalls VOR der Entscheidung lagen.

    ZEITLICHE SAUBERKEIT (Ende): das Fenster braucht GENAU `horizon_h`
    VOLLSTAENDIG ABGESCHLOSSENE Kerzen ab dem Start - nicht `horizon_h`
    Stunden Kalenderzeit. `df.loc[start:start+Nh]` waere INKLUSIVE beider
    Grenzen und haette bei Stundenkerzen `horizon_h + 1` Baelkchen geliefert;
    `candles_range()` liefert ausserdem (anders als candles_df/prepare())
    auch die aktuell noch LAUFENDE Kerze mit, deren High/Low sich noch
    aendern. Beides zusammen genommen. Ohne den Filter auf tatsaechlich
    geschlossene Kerzen haette ein Signal, das laut SQL-Vorfilter (ts +
    horizon_h Stunden vergangen) "faellig" war, aber dessen echtes Fenster
    (ab dem aufgerundeten Start) noch gar nicht vollstaendig verstrichen war,
    ein VORLAEUFIGES Ergebnis als ENDGUELTIG gespeichert bekommen - und
    outcome_last_h haette jede spaetere Korrektur verhindert. Ist die
    Kerzenzahl (noch) zu klein, wird bewusst None zurueckgegeben: der
    Aufrufer laesst outcome_last_h dann unveraendert, die Zeile bleibt
    "pending" und wird beim naechsten Takt erneut versucht.
    """
    import pandas as pd
    from data.hyperliquid import PAPER_SLIPPAGE_PCT, TAKER_FEE

    # tz-aware UTC, damit der Vergleich unten (df.index ist seit core.clock
    # ebenfalls tz-aware UTC) tatsaechlich dieselbe Uhr benutzt, statt zwei
    # gleich aussehende, aber verschiedene Uhrzeiten stillschweigend zu
    # vergleichen (siehe core/clock.py-Docstring).
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(clock.now_utc())
    entry = row.get("price")
    side = row.get("side")
    if not entry or side not in ("long", "short"):
        return None
    try:
        decided_at = pd.Timestamp(row["ts"])
    except (KeyError, ValueError, TypeError):
        return None
    window_start = decided_at.ceil("h")
    # Nur Kerzen, die bis `now` VOLLSTAENDIG abgeschlossen sind (Ende der
    # Stunde <= now) - schliesst sowohl die noch laufende Randkerze aus als
    # auch jeden Versuch, bevor das echte Fenster ueberhaupt verstrichen ist.
    future = df.loc[(df.index >= window_start) & (df.index + timedelta(hours=1) <= now)]
    if len(future) < horizon_h:
        return None
    start = future.index[0]
    # Positionsbasiert statt ueber .loc[start:ende] slicen - EXAKT
    # `horizon_h` Baelkchen, keine inklusive Grenzverschiebung.
    window = future.iloc[:horizon_h]

    entry = float(entry)
    direction = 1 if side == "long" else -1
    funding_hourly = row.get("funding_hourly")
    # Dieselbe Vorzeichenkonvention wie core.bot_signals.score_side: Longs
    # zahlen bei positiver Rate, Shorts bekommen - ueber die Haltedauer
    # DIESES Fensters, nicht ueber max_hold_hours wie im Score selbst
    # (dort ist es eine Vorab-Schaetzung, hier die tatsaechliche Dauer).
    funding_pct = (float(funding_hourly) * len(window) * 100 * direction
                   if funding_hourly is not None else 0.0)
    cost_pct = 2 * (TAKER_FEE + PAPER_SLIPPAGE_PCT) * 100 + funding_pct
    gross_pct = (float(window["Close"].iloc[-1]) / entry - 1) * 100 * direction
    high, low = float(window["High"].max()), float(window["Low"].min())
    # MFE/MAE aus Sicht der WETTE, nicht des Kurses: fuer einen Short ist ein
    # fallender Kurs der guenstigste Verlauf.
    mfe_pct = ((high / entry - 1) if direction == 1 else (entry / low - 1)) * 100
    mae_pct = ((low / entry - 1) if direction == 1 else (entry / high - 1)) * 100

    first_r = None
    stop_pct = row.get("stop_pct")
    if stop_pct:
        target = entry * (1 + direction * float(stop_pct) / 100.0)
        stop = entry * (1 - direction * float(stop_pct) / 100.0)
        for _, bar in window.iterrows():
            hit_target = (bar["High"] >= target) if direction == 1 else (bar["Low"] <= target)
            hit_stop = (bar["Low"] <= stop) if direction == 1 else (bar["High"] >= stop)
            if hit_target and hit_stop:
                # Innerhalb einer Stundenkerze ist die Reihenfolge nicht
                # rekonstruierbar - ehrlich benennen statt raten.
                first_r = "unentscheidbar"
                break
            if hit_target:
                first_r = "+1R"
                break
            if hit_stop:
                first_r = "-1R"
                break

    return {"net_return_pct": round(gross_pct - cost_pct, 4),
            "gross_return_pct": round(gross_pct, 4),
            "mfe_pct": round(mfe_pct, 4), "mae_pct": round(mae_pct, 4),
            "first_r": first_r, "bars": int(len(window))}


def backfill_signal_outcomes(candles_fn=None, now: datetime | None = None) -> int:
    """Ergebnisse zu bereits protokollierten Signalen nachtragen.

    Reine Datensammlung fuer spaetere Auswertungen (core.db.bot_signal_log);
    sie veraendert WEDER Limits NOCH Scores. Das ist Absicht: eine Strategie,
    die sich nach jedem Lauf selbst nachzieht, lernt vor allem Rauschen.
    """
    now = now or clock.now_utc()
    if candles_fn is None:
        from data.hyperliquid import candles_range as _range

        def candles_fn(symbol, start_ms, end_ms):
            return _range(symbol, start_ms, end_ms, "1h")

    updated = 0
    budget = _OUTCOME_ROWS_PER_TICK
    for horizon in _OUTCOME_HORIZONS_H:
        if budget <= 0:
            break
        cutoff = (now - timedelta(hours=horizon)).isoformat(timespec="seconds")
        rows = db.list_bot_signal_log_pending(horizon, cutoff, limit=budget)
        if not rows:
            continue
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)
        for symbol, symbol_rows in by_symbol.items():
            oldest = min(str(r["candle_ts"] or r["ts"]) for r in symbol_rows)
            try:
                import pandas as pd
                start_ms = int(pd.Timestamp(oldest).timestamp() * 1000)
                df = candles_fn(symbol, start_ms, int(now.timestamp() * 1000))
            except Exception as e:
                _log(f"Outcome-Nachtrag {symbol}: Kursdaten nicht abrufbar ({e!r}).")
                continue
            if df is None or df.empty:
                continue
            for row in symbol_rows:
                outcome = _signal_outcome(row, df, horizon, now=now)
                if outcome is None:
                    continue
                try:
                    existing = json.loads(row["outcome_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    existing = {}
                existing[str(horizon)] = outcome
                db.set_bot_signal_outcome(row["id"], json.dumps(existing), horizon)
                updated += 1
                budget -= 1
    return updated


def _maybe_backfill_outcomes():
    """Ein Fehler hier darf den Handel nie stoppen - es ist Buchhaltung."""
    try:
        count = backfill_signal_outcomes()
    except Exception as e:
        _log(f"Outcome-Nachtrag fehlgeschlagen: {e!r}")
        return
    if count:
        _log(f"Signal-Ergebnisse nachgetragen: {count} Zeile(n).")


def _record_fatal(message: str):
    """Abbruchgrund für die Oberfläche festhalten.

    Genau das fehlte: Der Runner beendete sich bei einem Anker-Konflikt still
    mit `return`. Die Meldung stand nur im Konsolenfenster der .bat, die
    Streamlit-Seite zeigte lediglich "Runner nicht nachweisbar" - der Bot
    schien zu laufen und tat einen ganzen Tag lang nichts.
    """
    db.set_meta(_LAST_ERROR_META_KEY, message)
    db.set_meta("bot_runner_last_error_at", clock.iso_utc())
    _log(message)


def _seed_paper_state() -> tuple[float, dict]:
    """Zustand fuer eine neu gebaute PaperExchange aus der DB rekonstruieren,
    damit ein Prozessneustart nicht mehr bei einem leeren Depot beginnt.

    `cash_usd` kommt aus der letzten Equity-Momentaufnahme: dort speichert
    core.bot.record_equity_snapshot() `state.withdrawable_usd`, und das ist
    fuer PaperExchange exakt ihr internes `_cash_usd` (account_state() gibt
    withdrawable_usd=self._cash_usd zurueck) - also derselbe Wert, mit dem
    die letzte Sitzung endete. Ohne Momentaufnahme (allererster Start) faellt
    es auf das eingefrorene Startkapital zurueck.

    Offene Positionen kommen 1:1 aus bot_positions; unrealized_pnl_usd wird
    beim ersten account_state()-Aufruf ohnehin aus dem aktuellen Kurs neu
    berechnet, der hier gespeicherte Wert ist nur ein Platzhalter.
    """
    info = bot.start_info()
    starting = info["equity_start_usd"] if info else _PAPER_STARTING_EQUITY_USD
    latest = db.latest_bot_equity()
    cash = (float(latest["cash_usd"])
           if latest and latest.get("cash_usd") is not None else starting)
    positions = {
        p["symbol"]: _HLPosition(symbol=p["symbol"], side=p["side"], size=p["size"],
                                entry_px=p["entry_px"], leverage=p["leverage"],
                                unrealized_pnl_usd=0.0)
        for p in db.list_open_bot_positions()
    }
    return cash, positions


def _build_exchange():
    if bot_config.dry_run_enabled() or not bot_config.live_trading_allowed():
        cash, positions = _seed_paper_state()
        if positions:
            _log(f"Dry-Run aktiv - PaperExchange fortgesetzt mit {cash:,.2f} $ Cash und "
                 f"{len(positions)} offene(r/n) Position(en) aus der DB.")
        else:
            _log(f"Dry-Run aktiv - PaperExchange mit {cash:,.2f} $ Startkapital.")
        return PaperExchange(starting_equity_usd=cash, positions=positions)
    # Der frueher verpflichtende Phase-F-Scharfschuss ist keine Sperre mehr
    # (bewusste Entscheidung des Nutzers). An seine Stelle tritt die auf
    # core.bot_config.FIRST_LIVE_ORDER_CAP_USD gedeckelte erste Live-Order,
    # die core.bot.open_position selbst durchsetzt.
    _log("Live-Handel aktiviert - verbinde mit dem echten Hyperliquid-Konto.")
    if bot_config.first_live_order_pending():
        _log(f"Erste Live-Eröffnung wird auf {bot_config.FIRST_LIVE_ORDER_CAP_USD:,.0f} $ "
             f"Nominale gedeckelt (Nachweis, dass Fill, Stop-Order und Protokoll stimmen).")
    return LiveExchange()


def _record_signal_cycle_outcome(signals: dict, runner_mode: str):
    """Zaehlt den Zyklus als Erfolg oder Fehlschlag - AUSGELAGERT aus
    run_forever(), damit dies isoliert testbar ist (run_forever() selbst
    bleibt bewusst ungetestet, siehe Moduldoc).

    core.bot.run_deterministic_cycle() faengt einen Fehler im Signal-Zyklus
    SELBST ab (signal_cycle_failed) und gibt normal zurueck, damit der
    bereits gelaufene Sicherheitsteil (Stop-Trigger, Reconciliation,
    Equity-Boden) nicht entwertet wird - der try/except in run_forever()
    sieht diesen Fehler deshalb NIE. Ohne diese Pruefung zaehlte ein
    dauerhaft fehlschlagender Signalzyklus trotzdem als voller Erfolg, und
    core.bot_config.runner_degraded() (der check_runner_health-Guard, sperrt
    neue Einstiege) haette ihn nie erkannt (gefunden von einer externen
    Pruefung, 13.09.2026)."""
    if not signals.get("error"):
        bot_config.record_cycle_success()
        return
    failures = bot_config.record_cycle_failure()
    _log(f"Signal-Zyklus fehlgeschlagen ({failures}. in Folge): {signals['error']}")
    if failures == bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES:
        _log(f"WARNUNG: {failures} Zyklen in Folge fehlgeschlagen - neue Einstiege "
             f"sind pausiert, bis wieder ein Zyklus sauber durchläuft.")
        if runner_mode == "live":
            notify.send(f"⚠️ Runner gestört: {failures} Zyklen in Folge fehlgeschlagen "
                       f"- neue Einstiege pausiert. Letzter Fehler (Signalzyklus): "
                       f"{signals['error']}")


def run_forever():
    db.init_db()
    if not _acquire_singleton_lock():
        _record_fatal(f"ABBRUCH: Ein anderer Runner-Prozess hält bereits die Sperrdatei "
                      f"({_LOCK_PATH}) - kein Doppelstart, unabhängig davon, wie der zweite "
                      f"Prozess gestartet wurde.")
        return
    if not bot_config.live_trading_allowed():
        _log("WARNUNG: Demo-Modus aktiv (config.DB_PATH == DEMO_DB_PATH) - "
             "core.bot_guards blockiert dort jede Order, auch im Dry-Run.")

    try:
        exchange = _build_exchange()
    except Exception as e:
        _record_fatal(f"ABBRUCH: Börsenanbindung konnte nicht aufgebaut werden: {e}")
        return
    # Der Modus gehört zur beim Start gebauten Exchange-Instanz. Eine spätere
    # Meta-Änderung darf eine noch laufende PaperExchange nicht fälschlich als
    # Live-Prozess in der UI ausweisen (und umgekehrt).
    runner_mode = "paper" if isinstance(exchange, PaperExchange) else "live"

    if bot.is_active() and bot.exchange_kind_mismatch(exchange):
        anchor = (bot.start_info() or {}).get("exchange_kind")
        _record_fatal(
            f"ABBRUCH: Der Start-Anker gehört zum Modus '{anchor}', gestartet wurde aber "
            f"'{runner_mode}'. Keine automatische Neuinitialisierung - das würde die "
            f"Vergleichsbasis heimlich verschieben. Auf der Trading-Bot-Seite entweder "
            f"den Anker bewusst auf den aktuellen Modus setzen oder den Bot zurücksetzen.")
        return

    # Ein Stop-Wunsch aus einem frueheren Lauf darf den neu gestarteten Prozess
    # nicht sofort wieder beenden. Ebenso ein alter Abbruchgrund: Ab hier laeuft
    # der Runner nachweislich, die Meldung waere sonst dauerhaft irrefuehrend.
    db.delete_meta(_STOP_REQUESTED_META_KEY)
    db.delete_meta(_LAST_ERROR_META_KEY)
    db.delete_meta("bot_runner_last_error_at")
    db.set_meta("bot_runner_pid", str(os.getpid()))
    # Session-Startkapital IMMER neu setzen, auch bei einem bereits aktiven
    # Bot - reine Anzeige-Baseline (core.bot.record_session_start), getrennt
    # vom sicherheitskritischen equity_start_usd-Anker oben.
    bot.record_session_start(exchange)
    _heartbeat(runner_mode)
    if not bot.is_active():
        info = bot.initialize(exchange)
        _log(f"Bot initialisiert: Startkapital {info['equity_start_usd']:,.2f} $ "
             f"({runner_mode}).")

    interval_hours = CYCLE_SECONDS / 3600
    _log(f"Zyklus startet, alle {CYCLE_SECONDS // 60} Minuten.")
    while True:
        if _stop_requested():
            _log("Stop angefordert - beende sauber.")
            break
        try:
            _maybe_close_all(exchange, runner_mode)
            _maybe_close_requested_positions(exchange, runner_mode)
            _maybe_apply_manual_stops(exchange, runner_mode)
            _maybe_backfill_exchange_opened_at(exchange)
            _heartbeat(runner_mode)
            if runner_mode != bot.current_mode():
                _log(f"WARNUNG: Dieser Prozess hält eine '{runner_mode}'-Börsenanbindung, die "
                     f"Einstellung steht aber auf '{bot.current_mode()}' - ein Dry-Run-/Live-"
                     f"Wechsel wirkt erst nach einem Neustart dieses Prozesses.")
            # VOR dem Zyklus statt danach: an dem Takt, an dem ein Refresh
            # tatsaechlich laeuft, sollen frisch geladene Ein-/Auszahlungen
            # (core.bot.refresh_live_flows) noch DIESEN Zyklus'
            # Risikopruefungen (Equity-Boden, Exposure) informieren, nicht
            # erst den naechsten Takt 15 Minuten spaeter. _maybe_refresh_flows
            # laeuft dadurch vor JEDER Equity-Boden-Pruefung, macht bei Erfolg
            # aber selbst nur stuendlich etwas (siehe Docstring dort) - das
            # taegliche Gate in _maybe_refresh_costs bleibt fuer Gebuehren/
            # Funding unveraendert, das ist eine bewusste Kostenbremse fuer
            # einen reinen Anzeigewert, keine Regression.
            _maybe_refresh_flows(exchange)
            _maybe_refresh_costs(exchange)
            _maybe_refresh_universe()
            # Vor dem Zyklus erfassen, um einen NEU ausgeloesten Kill-Switch zu
            # erkennen (siehe Telegram-Hook unten) - ein blosser Vergleich mit
            # result["kill_switch_active"] allein wuerde bei jedem Takt erneut
            # benachrichtigen, solange der Switch aktiv bleibt.
            was_killed = bot_config.is_killed()
            result = bot.run_deterministic_cycle(exchange, interval_hours=interval_hours)
            recon = result["reconciliation"]
            if recon["phantom_closed"]:
                _log(f"Reconciliation: Phantom-Position(en) DB-seitig geschlossen "
                     f"(auf der Börse nicht mehr offen): {recon['phantom_closed']}")
                # Deckt u.a. den Fall ab, dass der Stop-Loss-Trigger DIREKT auf
                # der Börse ausgelöst hat (Hyperliquid war schneller als der
                # naechste 15-Minuten-Takt) - ohne diesen Hook blieb genau der
                # haeufigste Stop-Auslöse-Weg stumm, weil check_stop_triggers()
                # die Position dann schon nicht mehr vorfindet und nur noch
                # die Reconciliation den echten Schluss bemerkt.
                if runner_mode == "live":
                    for d in recon.get("phantom_closed_details", []):
                        pnl = d.get("realized_pnl_usd")
                        pnl_txt = f"\nPnL: {pnl:+,.2f} $" if pnl is not None else ""
                        precision_note = "" if d.get("from_real_fills") else "\n(Kurs geschätzt, kein Börsen-Fill gefunden)"
                        notify.send(f"⚪ Position automatisch geschlossen: {d['symbol']}\n"
                                   f"Börse zeigte sie nicht mehr offen (Stop-Loss, Liquidation "
                                   f"oder manueller Eingriff) @ {d['price']:,.4f} ${pnl_txt}{precision_note}")
            if recon["adopted"]:
                _log(f"Reconciliation: unbekannte Börsen-Position(en) automatisch übernommen "
                     f"(frischer Stop gesetzt): {recon['adopted']}")
            if recon["unknown_positions"]:
                _log(f"WARNUNG: Börsen-Position(en) konnten nicht automatisch übernommen werden: "
                     f"{recon['unknown_positions']} - Kill-Switch ausgelöst, manuelle Prüfung nötig.")
            for warning in result.get("existing_position_limit_warnings", []):
                _log(f"WARNUNG: {warning}")
            if result["stop_events"]:
                symbols = [e["symbol"] for e in result["stop_events"]]
                _log(f"Stop ausgelöst: {symbols}")
                if runner_mode == "live":
                    for e in result["stop_events"]:
                        if not e["closed"]:
                            continue
                        pnl = e["result"].get("realized_pnl_usd")
                        pnl_txt = f"\nPnL: {pnl:+,.2f} $" if pnl is not None else ""
                        notify.send(f"🛑 Stop-Loss: {e['symbol']} @ {e['price']:,.4f} ${pnl_txt}")
            if result["failed_stop_closes"]:
                _log(f"WARNUNG: Stop-Schliessung fehlgeschlagen für "
                     f"{[e['symbol'] for e in result['failed_stop_closes']]} - nächster Zyklus versucht erneut.")
                if runner_mode == "live":
                    for e in result["failed_stop_closes"]:
                        notify.send(f"⚠️ Stop-Schliessung fehlgeschlagen: {e['symbol']} bleibt "
                                   f"ungeschützt offen - nächster Takt versucht erneut.")
            if result["kill_switch_active"]:
                info = bot_config.kill_switch_info()
                _log(f"Kill-Switch aktiv ({info['reason']}) - keine neuen Positionen, "
                     f"bis in der UI bewusst zurückgesetzt.")
                if runner_mode == "live" and not was_killed:
                    notify.send(f"🛑 Kill-Switch ausgelöst: {info['reason']}\n"
                               f"Keine neuen Positionen, bis in der UI bewusst zurückgesetzt.")
            for update in result.get("trailing_updates", []):
                _log(f"Stop nachgezogen: {update['symbol']} auf {update['new_stop']:,.4f} $"
                     f"{' (Börsenorder ersetzt)' if update['exchange_updated'] else ''}")
            signals = result.get("signals") or {}
            for exit_event in signals.get("exits", []):
                _log(f"Signal-Ausstieg {exit_event['symbol']}: {exit_event['reason']} "
                     f"-> {exit_event['result'].get('status')}")
                if runner_mode == "live" and exit_event["result"].get("status") == "filled":
                    pnl = exit_event["result"].get("realized_pnl_usd")
                    pnl_txt = f"\nPnL: {pnl:+,.2f} $" if pnl is not None else ""
                    notify.send(f"⚪ Position geschlossen: {exit_event['symbol']}\n"
                               f"Grund: {exit_event['reason']}{pnl_txt}")
            if signals.get("entry"):
                entry = signals["entry"]
                _log(f"Signal-Einstieg {entry['symbol']} {entry['side']} "
                     f"(Score {entry['score']:.0f}, {entry['notional_usd']:,.2f} $) "
                     f"-> {entry['result'].get('status')} "
                     f"{entry['result'].get('reason') or ''}")
                if runner_mode == "live":
                    entry_reason = entry["result"].get("reason") or ""
                    if entry["result"].get("status") == "filled":
                        notify.send(f"{'🟢' if entry['side'] == 'long' else '🔴'} Neue Position: "
                                   f"{entry['symbol']} {entry['side'].upper()}\n"
                                   f"Nominal: {entry['notional_usd']:,.2f} $ · Score {entry['score']:.0f}")
                    elif "NOTAUSSTIEG FEHLGESCHLAGEN" in entry_reason:
                        # Schwerster Fall: Eroeffnung gefuellt, Stop-Order UND
                        # Notausstieg fehlgeschlagen - die Position steht ohne
                        # jeden Schutz auf der Boerse (core.bot.open_position()).
                        notify.send(f"🛑 UNGESCHÜTZTE POSITION: {entry['symbol']}\n{entry_reason}")
            elif signals.get("skipped"):
                _log(f"Kein Einstieg: {signals['skipped']}")

            _log_open_positions(exchange)

            _maybe_run_llm_review(exchange, result)
            _maybe_backfill_outcomes()
            _record_signal_cycle_outcome(signals, runner_mode)
        except Exception as e:  # ein einzelner fehlgeschlagener Zyklus darf den Bot
            # nicht dauerhaft beenden - der naechste Tick versucht es wieder.
            # Ein echter Fehler wird trotzdem laut geloggt, nicht verschluckt.
            failures = bot_config.record_cycle_failure()
            _log(f"FEHLER im Zyklus ({failures}. in Folge): {e!r}")
            if failures == bot_config.MAX_CONSECUTIVE_CYCLE_FAILURES:
                _log(f"WARNUNG: {failures} Zyklen in Folge fehlgeschlagen - neue Einstiege "
                     f"sind pausiert, bis wieder ein Zyklus sauber durchläuft.")
                if runner_mode == "live":
                    notify.send(f"⚠️ Runner gestört: {failures} Zyklen in Folge fehlgeschlagen "
                               f"- neue Einstiege pausiert. Letzter Fehler: {e!r}")
        wake_reason = _sleep_with_stop_check(CYCLE_SECONDS)
        if wake_reason == "stop":
            _log("Stop angefordert - beende sauber.")
            break
        # wake_reason == "close_all"/"close_position"/"manual_stop" (oder
        # None nach vollem Schlaf): einfach weiter in die Schleife -
        # _maybe_close_all()/_maybe_close_requested_positions()/
        # _maybe_apply_manual_stops() ganz oben im naechsten Durchlauf lesen
        # das jeweilige Flag und fuehren tatsaechlich aus, statt dass der
        # blosse Aufwach-Grund den Runner beendet.
    _clear_runner_markers()


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        _clear_runner_markers()
        sys.exit(0)
    finally:
        _release_singleton_lock()
