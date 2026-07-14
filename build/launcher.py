"""Launcher für Portfolio Pro.

Startet die gebündelte Python-Laufzeit mit `streamlit run app/app.py`, wartet bis
der lokale Server erreichbar ist, und öffnet die App im Standardbrowser. Wird mit
PyInstaller zu einer schlanken `PortfolioPro.exe` gebaut (nur stdlib) und liegt im
Installationsordner neben `app/` und `runtime/`.

Installations-Layout (durch den Installer erzeugt):
    <InstallDir>/PortfolioPro.exe        (diese Datei, eingefroren)
    <InstallDir>/app/app.py              (App-Code)
    <InstallDir>/runtime/python/pythonw.exe  (gebündelte Laufzeit inkl. Bibliotheken)

Im Entwicklungs-Betrieb (direkt `python build/launcher.py` aus dem Repo) wird das
Repo-Layout und der aktuelle Interpreter verwendet.
"""
import atexit
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    ROOT = Path(sys.executable).resolve().parent
    APP_DIR = ROOT / "app"
    PYTHON = ROOT / "runtime" / "python" / "pythonw.exe"
else:
    ROOT = Path(__file__).resolve().parent.parent
    APP_DIR = ROOT
    PYTHON = Path(sys.executable)

APP_SCRIPT = APP_DIR / "app.py"


def _free_port(preferred: int = 8501) -> int:
    """Bevorzugten Port nehmen, sonst einen freien vom OS zuweisen lassen."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def main() -> int:
    if not APP_SCRIPT.exists():
        print(f"FEHLER: app.py nicht gefunden unter {APP_SCRIPT}")
        return 1
    if FROZEN and not PYTHON.exists():
        print(f"FEHLER: gebündelte Python-Laufzeit fehlt: {PYTHON}")
        return 1

    port = _free_port()
    cmd = [
        str(PYTHON), "-m", "streamlit", "run", str(APP_SCRIPT),
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    env = dict(os.environ)
    print("Portfolio Pro startet …")
    proc = subprocess.Popen(cmd, cwd=str(APP_DIR), env=env)

    def _cleanup():
        if proc.poll() is None:
            proc.terminate()
    atexit.register(_cleanup)

    url = f"http://127.0.0.1:{port}"
    if _wait_until_up(port):
        webbrowser.open(url)
        print(f"\nPortfolio Pro läuft: {url}")
    else:
        print("Server nicht rechtzeitig erreichbar – bitte Fenster prüfen.")

    print("\nDieses Fenster offen lassen, solange du Portfolio Pro nutzt.")
    print("Zum Beenden dieses Fenster schließen.")
    try:
        proc.wait()
    except KeyboardInterrupt:
        _cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
