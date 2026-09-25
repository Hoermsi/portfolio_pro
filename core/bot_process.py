"""Trading-Bot-Prozess (bot_runner.py) aus der Streamlit-UI heraus starten,
stoppen und seinen Laufstatus prüfen.

Vorbild core/updater.py:_launch_detached() - dieselben Windows-Flags, aus
demselben Grund: die als PyInstaller-EXE gebaute App kann ihre Kindprozesse
in ein Windows-Job-Object stecken, das sie beim Beenden mit-killt, wenn sie
nicht explizit per CREATE_BREAKAWAY_FROM_JOB ausbrechen. Anders als der
Updater ist kein .bat-Zwischenskript nötig - bot_runner.py wird direkt mit
demselben Python-Interpreter gestartet, der auch Streamlit ausführt.

PID-Erkennung über `tasklist` (Windows-Bordmittel, wie core/updater.py's
Swap-Skript es für sich selbst schon nutzt) statt einer neuen psutil-
Abhängigkeit.
"""
import subprocess
import sys
import re

from core import config, db

BOT_RUNNER_SCRIPT = config.BASE_DIR / "bot_runner.py"
# bot_runner.py laeuft als DETACHED_PROCESS (kein Konsolenfenster) - ohne
# explizite Umleitung verschwindet jede _log()-Zeile (inklusive einer
# Ausnahme, die run_deterministic_cycle() abfaengt und nur noch loggt statt
# sie hochzureichen) spurlos ins Leere. Reines Anhaengen (nicht Ueberschreiben),
# damit ein Neustart die Spur eines vorherigen Absturzes nicht loescht.
BOT_RUNNER_LOG = config.DATA_DIR / "bot_runner.log"


def is_running() -> bool:
    """True, wenn die zuletzt gespeicherte PID laut Windows-Prozessliste
    tatsächlich noch existiert. Liefert False (nicht: unbekannt) bei jedem
    Fehler - eine hängende/unklare Prüfung darf einen Start nicht dauerhaft
    verhindern; die UI zeigt den Heartbeat separat als zweites Signal."""
    pid = str(db.get_meta("bot_runner_pid") or "").strip()
    if not pid.isdigit():
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, errors="ignore", timeout=10,
        )
    except Exception:
        return False
    return re.search(rf"\b{re.escape(pid)}\b", getattr(result, "stdout", "") or "") is not None


def start() -> tuple[bool, str]:
    """Startet bot_runner.py als eigenständigen, vom Streamlit-Prozess
    losgelösten Prozess. Verweigert einen Doppelstart, wenn laut PID schon
    ein Runner läuft - zwei gleichzeitige Runner würden unabhängig in
    dieselbe DB schreiben und sich gegenseitig widersprechen (z.B. zwei
    PaperExchange-Instanzen mit unterschiedlichem Cash-Stand)."""
    if is_running():
        return False, "Ein Runner läuft laut Prozessliste bereits - kein Doppelstart."
    if not BOT_RUNNER_SCRIPT.exists():
        return False, f"bot_runner.py nicht gefunden unter {BOT_RUNNER_SCRIPT}."

    base_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    args = [sys.executable, str(BOT_RUNNER_SCRIPT)]
    with open(BOT_RUNNER_LOG, "a", encoding="utf-8", errors="replace") as log_file:
        try:
            proc = subprocess.Popen(args, cwd=str(config.BASE_DIR),
                                    creationflags=base_flags | breakaway,
                                    stdout=log_file, stderr=subprocess.STDOUT)
        except OSError:
            # Manche Jobs erlauben kein Breakaway (CreateProcess schlägt dann
            # komplett fehl statt das Flag zu ignorieren) - ohne erneut versuchen,
            # exakt das core/updater.py:_launch_detached()-Muster.
            try:
                proc = subprocess.Popen(args, cwd=str(config.BASE_DIR),
                                        creationflags=base_flags,
                                        stdout=log_file, stderr=subprocess.STDOUT)
            except OSError as e:
                return False, f"Start fehlgeschlagen: {e}"
        # Der Kindprozess haelt seit Popen() ein eigenes dupliziertes Handle -
        # das Elternhandle darf hier (Ende des `with`-Blocks) schliessen,
        # ohne die Umleitung im Kind zu beeinflussen.
    # Sofort merken, nicht erst wenn bot_runner.py selbst dazu kommt (dort erst
    # nach dem Aufbau der Boersenanbindung, s. bot_runner.py:258) - sonst
    # erkennt weder der Doppelstart-Schutz oben noch der Runner-Toggle in der
    # UI (views/trading_bot.py) den frisch gestarteten Prozess sofort.
    db.set_meta("bot_runner_pid", str(proc.pid))
    return True, f"Runner gestartet (PID {proc.pid}). Der erste Herzschlag erscheint in Kürze."


def request_stop() -> str:
    """Setzt das Stop-Flag, das bot_runner.py's Hauptschleife spätestens
    nach wenigen Sekunden sieht (dort: _sleep_with_stop_check). Kein harter
    Kill - der laufende Zyklus wird noch zu Ende geführt, bevor sich der
    Prozess selbst beendet und seine Heartbeat-/PID-Marker löscht."""
    db.set_meta("bot_runner_stop_requested", "1")
    return "Stop angefordert - der Runner beendet sich innerhalb weniger Sekunden."


def _clear_runner_state():
    db.delete_meta("bot_runner_heartbeat")
    db.delete_meta("bot_runner_pid")
    db.delete_meta("bot_runner_stop_requested")


def force_kill() -> tuple[bool, str]:
    """Harter Fallback, falls der Prozess auf request_stop() nicht reagiert
    (z.B. hängt in einem einzelnen Zyklus fest, etwa bei einem blockierenden
    Netzwerk-Timeout). `/T` beendet auch etwaige Kindprozesse, `/F` erzwingt
    ohne Rückfrage - beides Windows-Bordmittel wie is_running()'s tasklist.

    Ohne bekannte PID gibt es nichts zu tracken (Zustand wird trotzdem
    geräumt - siehe unten). Ist der Prozess laut taskkill bereits weg
    (Exitcode 128 bzw. "not found"/"nicht gefunden" in der Meldung - beides
    je nach Windows-Sprache), gilt das als ERREICHTER Endzustand, kein
    Fehlschlag: die alte Fassung wertete "taskkill findet den Prozess nicht"
    als Kill-FEHLSCHLAG, wodurch ein bereits abgestürzter Runner (oder ein
    früherer Kill-Versuch, der schon griff, ohne die Flags zu räumen) die UI
    für IMMER im "⏳ Stoppt …"-Zustand hängen liess - jeder erneute Versuch
    scheiterte identisch, und offene Positionen liessen sich auch über
    "Alle Positionen schließen" nicht mehr auflösen (das setzt nur ein Flag,
    das ebenfalls nur die - tote - Runner-Schleife abarbeitet). Ein ECHTER
    Kill-Fehlschlag (z.B. "Zugriff verweigert", der Prozess läuft also noch)
    bleibt bewusst ein Fehlschlag - nur "nicht gefunden" gilt als Erfolg."""
    pid = str(db.get_meta("bot_runner_pid") or "").strip()
    if not pid.isdigit():
        _clear_runner_state()
        return True, "Keine PID bekannt - Zustand zurückgesetzt."
    try:
        result = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                                capture_output=True, text=True, timeout=10)
    except Exception as e:
        return False, f"taskkill fehlgeschlagen: {e}"
    if getattr(result, "returncode", 0) != 0:
        output = ((getattr(result, "stderr", "") or "") + " "
                  + (getattr(result, "stdout", "") or "")).lower()
        if result.returncode == 128 or "not found" in output or "nicht gefunden" in output:
            _clear_runner_state()
            return True, "Prozess war bereits beendet - Zustand zurückgesetzt."
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or
                  "unbekannter Fehler").strip()
        return False, f"taskkill fehlgeschlagen: {detail}"
    if is_running():
        return False, "taskkill meldet Erfolg, der Runner ist aber noch aktiv."
    _clear_runner_state()
    return True, f"Prozess (PID {pid}) hart beendet."
