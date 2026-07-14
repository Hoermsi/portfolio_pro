"""In-App-Auto-Update über GitHub Releases.

Ablauf:
1. `check_for_update()` fragt das neueste Release der GitHub-API ab und vergleicht
   dessen Version mit `APP_VERSION`.
2. Ist eine neuere Version verfügbar, lädt `apply_update()` das Code-Zip herunter,
   entpackt es in einen Staging-Ordner und startet einen kleinen externen Helfer
   (`.bat`), der - NACH Beenden der laufenden App - den Code-Ordner ersetzt, bei
   geänderten Requirements `pip install` ausführt und die App neu startet.

Weil alle Nutzerdaten (DB, .env) in `config.DATA_DIR` liegen - getrennt vom Code -
tauscht das Update ausschließlich den Programmcode. Positionen, Cash und API-Keys
bleiben unberührt.
"""
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import requests
from packaging.version import InvalidVersion, Version

from core import config
from core.cache import ttl_cache
from core.version import APP_VERSION

# --- Vom Betreiber (dir) zu setzen: öffentliches GitHub-Repo "user/repo". -------
# Solange der Platzhalter steht, ist die Update-Prüfung deaktiviert (gibt None
# zurück), sodass die App auch ohne konfigurierten Update-Kanal normal läuft.
GITHUB_REPO = "Hoermsi/portfolio_pro"
_PLACEHOLDER = "DEIN-GITHUB-USER/portfolio_pro"

_API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_TIMEOUT = 10


def is_configured() -> bool:
    """True, wenn ein echtes Repo hinterlegt wurde (nicht der Platzhalter)."""
    return bool(GITHUB_REPO) and GITHUB_REPO != _PLACEHOLDER


def _tag_to_version(tag: str) -> Version | None:
    """'v1.2.0' oder '1.2.0' -> Version; bei Unfug None."""
    try:
        return Version(tag.lstrip("vV").strip())
    except (InvalidVersion, AttributeError):
        return None


def _pick_asset(release: dict) -> str | None:
    """URL des Code-Zip-Assets wählen (bevorzugt *.zip); sonst der Zipball."""
    for asset in release.get("assets", []) or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip") and asset.get("browser_download_url"):
            return asset["browser_download_url"]
    return release.get("zipball_url")


@ttl_cache(300)
def check_for_update() -> dict | None:
    """Neuestes Release prüfen. Gibt bei verfügbarem Update
    {version, notes, asset_url} zurück, sonst None (auch offline/Fehler/nicht konfiguriert).
    """
    if not is_configured():
        return None
    try:
        resp = requests.get(
            _API_LATEST.format(repo=GITHUB_REPO),
            headers={"Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        release = resp.json()
    except (requests.RequestException, ValueError):
        return None

    latest = _tag_to_version(release.get("tag_name", ""))
    current = _tag_to_version(APP_VERSION)
    if latest is None or current is None or latest <= current:
        return None

    asset_url = _pick_asset(release)
    if not asset_url:
        return None
    return {
        "version": str(latest),
        "notes": release.get("body") or "",
        "asset_url": asset_url,
    }


def _updates_dir() -> Path:
    d = config.DATA_DIR / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_update(asset_url: str, version: str) -> Path:
    """Release-Zip herunterladen und den Pfad zurückgeben."""
    target = _updates_dir() / f"portfolio_pro-{version}.zip"
    with requests.get(asset_url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
    return target


def stage_update(zip_path: Path, version: str) -> Path:
    """Zip in einen Staging-Ordner entpacken; den Ordner mit dem eigentlichen
    Code (der ein `app.py` enthält) zurückgeben. GitHub-Zipballs verpacken den
    Inhalt in einen Unterordner - deshalb wird nach `app.py` gesucht.
    """
    staging = _updates_dir() / f"staging-{version}"
    if staging.exists():
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    if (staging / "app.py").exists():
        return staging
    for sub in staging.iterdir():
        if sub.is_dir() and (sub / "app.py").exists():
            return sub
    raise FileNotFoundError("Im Update-Paket wurde keine app.py gefunden.")


def _swap_script_path() -> Path:
    return _updates_dir() / "apply_update.bat"


def _write_swap_script(source_dir: Path, install_dir: Path, relaunch: Path | None) -> Path:
    """Erzeugt eine .bat, die nach Beenden der App den Code ersetzt und neu startet.

    - wartet, bis der aktuelle Prozess beendet ist (robocopy sperrt sonst Dateien),
    - spiegelt `source_dir` nach `install_dir` (ohne den Nutzerdaten-Ordner),
    - startet danach optional den Launcher neu.
    """
    pid = os.getpid()
    relaunch_line = f'start "" "{relaunch}"' if relaunch else "rem kein Neustart konfiguriert"
    script = f"""@echo off
rem Warten, bis Portfolio Pro (PID {pid}) beendet ist
:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)
rem Code spiegeln (Nutzerdaten liegen getrennt und werden nicht angefasst)
robocopy "{source_dir}" "{install_dir}" /MIR /XD .git __pycache__ .pytest_cache >NUL
{relaunch_line}
"""
    path = _swap_script_path()
    path.write_text(script, encoding="utf-8")
    return path


def apply_update(asset_url: str, version: str, install_dir: Path | None = None,
                 relaunch: Path | None = None) -> Path:
    """Update herunterladen, stagen und den Swap-Helfer detached starten.

    Nach dem Aufruf sollte die App beendet werden, damit der Helfer die Dateien
    ersetzen kann. Gibt den Pfad zum Swap-Skript zurück.
    """
    install_dir = install_dir or config.BASE_DIR
    zip_path = download_update(asset_url, version)
    source_dir = stage_update(zip_path, version)
    script = _write_swap_script(source_dir, install_dir, relaunch)
    # Detached starten, damit er den Beenden der App überlebt.
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd", "/c", str(script)], cwd=str(config.DATA_DIR),
                     creationflags=creationflags, close_fds=True)
    return script


def shutdown_app():
    """Beendet den laufenden App-Prozess, damit der Swap-Helfer greifen kann."""
    os._exit(0)
