"""core/bot_process.py: nie einen echten Prozess starten/killen - subprocess.
Popen/run sind vollständig gemockt (Vorbild: tests/test_updater.py's
Umgang mit core/updater.py:_launch_detached())."""
from types import SimpleNamespace

import pytest

from core import bot_process, db


def test_is_running_false_without_pid(tmp_db):
    assert bot_process.is_running() is False


def test_is_running_true_when_tasklist_contains_pid(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")
    monkeypatch.setattr(bot_process.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="... 4242 Console ..."))
    assert bot_process.is_running() is True


def test_is_running_false_when_pid_not_in_tasklist_output(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")
    monkeypatch.setattr(bot_process.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="INFO: Keine Tasks gefunden."))
    assert bot_process.is_running() is False


def test_is_running_does_not_match_pid_as_substring(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")
    monkeypatch.setattr(bot_process.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="... 14242 Console ..."))
    assert bot_process.is_running() is False


def test_is_running_false_on_subprocess_error(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")

    def boom(*a, **k):
        raise OSError("tasklist nicht gefunden")
    monkeypatch.setattr(bot_process.subprocess, "run", boom)
    assert bot_process.is_running() is False


def test_start_refuses_when_already_running(tmp_db, monkeypatch):
    monkeypatch.setattr(bot_process, "is_running", lambda: True)
    ok, msg = bot_process.start()
    assert ok is False
    assert "bereits" in msg


def test_start_refuses_when_script_missing(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", tmp_path / "does_not_exist.py")
    ok, msg = bot_process.start()
    assert ok is False
    assert "nicht gefunden" in msg


def test_start_succeeds_and_uses_current_interpreter(tmp_db, monkeypatch, tmp_path):
    script = tmp_path / "bot_runner.py"
    script.write_text("# stub")
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", script)
    # Eigene Log-Datei statt der echten (core.config.DATA_DIR) - ein Test
    # darf nie in den realen Anwendungsordner des Nutzers schreiben.
    monkeypatch.setattr(bot_process, "BOT_RUNNER_LOG", tmp_path / "bot_runner.log")

    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=999)

    monkeypatch.setattr(bot_process.subprocess, "Popen", fake_popen)

    ok, msg = bot_process.start()

    assert ok is True
    assert "999" in msg
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == bot_process.sys.executable
    assert args[1] == str(script)
    # Ohne Umleitung verschwindet jede _log()-Zeile des DETACHED_PROCESS
    # spurlos (kein Konsolenfenster) - siehe BOT_RUNNER_LOG-Kommentar.
    assert kwargs["stdout"].name == str(tmp_path / "bot_runner.log")
    assert kwargs["stderr"] == bot_process.subprocess.STDOUT


def test_start_persists_pid_immediately(tmp_db, monkeypatch, tmp_path):
    """core/bot_process.py:start() muss die PID selbst speichern, nicht erst
    warten bis bot_runner.py sich meldet - sonst erkennt weder der
    Doppelstart-Schutz noch der Runner-Toggle in der UI den frisch
    gestarteten Prozess in der Zeitspanne bis zum ersten Herzschlag."""
    script = tmp_path / "bot_runner.py"
    script.write_text("# stub")
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", script)
    monkeypatch.setattr(bot_process.subprocess, "Popen",
                        lambda *a, **k: SimpleNamespace(pid=999))

    ok, msg = bot_process.start()

    assert ok is True
    assert db.get_meta("bot_runner_pid") == "999"


def test_start_failure_leaves_no_pid(tmp_db, monkeypatch, tmp_path):
    script = tmp_path / "bot_runner.py"
    script.write_text("# stub")
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", script)

    def boom(*a, **k):
        raise OSError("kein Prozess startbar")
    monkeypatch.setattr(bot_process.subprocess, "Popen", boom)

    ok, msg = bot_process.start()

    assert ok is False
    assert db.get_meta("bot_runner_pid") is None


def test_start_falls_back_without_breakaway_on_oserror(tmp_db, monkeypatch, tmp_path):
    script = tmp_path / "bot_runner.py"
    script.write_text("# stub")
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", script)

    attempts = []

    def fake_popen(args, creationflags=0, **kwargs):
        attempts.append(creationflags)
        if len(attempts) == 1:
            raise OSError("Breakaway nicht erlaubt")
        return SimpleNamespace(pid=1000)

    monkeypatch.setattr(bot_process.subprocess, "Popen", fake_popen)

    ok, msg = bot_process.start()

    assert ok is True
    assert len(attempts) == 2


def test_start_reports_failure_when_both_attempts_fail(tmp_db, monkeypatch, tmp_path):
    script = tmp_path / "bot_runner.py"
    script.write_text("# stub")
    monkeypatch.setattr(bot_process, "is_running", lambda: False)
    monkeypatch.setattr(bot_process, "BOT_RUNNER_SCRIPT", script)

    def boom(*a, **k):
        raise OSError("kein Prozess startbar")
    monkeypatch.setattr(bot_process.subprocess, "Popen", boom)

    ok, msg = bot_process.start()
    assert ok is False
    assert "fehlgeschlagen" in msg


def test_request_stop_sets_meta_flag(tmp_db):
    bot_process.request_stop()
    assert db.get_meta("bot_runner_stop_requested") == "1"


def test_force_kill_without_pid_clears_stuck_stop_state(tmp_db):
    """Ohne bekannte PID gibt es nichts zu tracken - aber ein haengendes
    Stop-Flag (state['stop_pending'] in views/trading_bot.py kennt nur das
    DB-Flag) darf die UI trotzdem nicht fuer immer im "Stoppt..."-Zustand
    festhalten. 'Hart beenden' muss also auch OHNE PID den Zustand raeumen."""
    db.set_meta("bot_runner_stop_requested", "1")
    ok, msg = bot_process.force_kill()
    assert ok is True
    assert db.get_meta("bot_runner_stop_requested") is None


def test_force_kill_clears_markers_on_success(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")
    db.set_meta("bot_runner_heartbeat", "2026-01-01T00:00:00")
    db.set_meta("bot_runner_stop_requested", "1")
    monkeypatch.setattr(bot_process.subprocess, "run", lambda *a, **k: SimpleNamespace())

    ok, msg = bot_process.force_kill()

    assert ok is True
    assert db.get_meta("bot_runner_pid") is None
    assert db.get_meta("bot_runner_heartbeat") is None
    assert db.get_meta("bot_runner_stop_requested") is None


def test_force_kill_reports_failure_on_exception(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")

    def boom(*a, **k):
        raise OSError("taskkill nicht gefunden")
    monkeypatch.setattr(bot_process.subprocess, "run", boom)

    ok, msg = bot_process.force_kill()
    assert ok is False
    # PID-Marker bleiben stehen - kein falscher "erfolgreich beendet"-Eindruck
    assert db.get_meta("bot_runner_pid") == "4242"


def test_force_kill_clears_state_when_process_is_already_gone(tmp_db, monkeypatch):
    """Der real gemeldete Bug: der Runner-Prozess war laengst abgestuerzt/weg
    (z.B. weil ein frueherer 'Hart beenden'-Versuch schon griff, ohne die
    Flags zu raeumen, oder ein harter Absturz ausserhalb von
    _clear_runner_markers()). taskkill meldet dann 'Process not found' -
    Exitcode 128, das MUSS als bereits erreichter Endzustand gelten, nicht
    als Kill-Fehlschlag. Ohne diese Unterscheidung blieb die UI fuer immer im
    '⏳ Stoppt...'-Zustand haengen: jeder erneute Klick auf 'Hart beenden'
    scheiterte identisch, und 'Alle Positionen schliessen' liess sich nicht
    mehr ausloesen (das haengt am selben tot geglaubten Runner)."""
    db.set_meta("bot_runner_pid", "4242")
    db.set_meta("bot_runner_heartbeat", "2026-01-01T00:00:00")
    db.set_meta("bot_runner_stop_requested", "1")
    monkeypatch.setattr(
        bot_process.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=128, stderr='ERROR: The process "4242" not found.',
                                        stdout=""),
    )

    ok, msg = bot_process.force_kill()

    assert ok is True
    assert db.get_meta("bot_runner_pid") is None
    assert db.get_meta("bot_runner_heartbeat") is None
    assert db.get_meta("bot_runner_stop_requested") is None


def test_force_kill_keeps_markers_when_taskkill_returns_error(tmp_db, monkeypatch):
    db.set_meta("bot_runner_pid", "4242")
    monkeypatch.setattr(
        bot_process.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="Zugriff verweigert", stdout=""),
    )

    ok, msg = bot_process.force_kill()

    assert ok is False
    assert "Zugriff verweigert" in msg
    assert db.get_meta("bot_runner_pid") == "4242"
