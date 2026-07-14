# Build & Auslieferung – Portfolio Pro

Erzeugt eine **`Setup.exe`**, die beim Freund App-Code, eine **eigene, isolierte
Python-Laufzeit** und den Launcher installiert. Kein vorinstalliertes Python nötig,
keine Admin-Rechte (Installation nach `%LOCALAPPDATA%\Programs\PortfolioPro`).

## Einmalige Voraussetzungen (auf deiner Maschine)

1. **Python** auf dem PATH (nur zum Bauen – zum Einfrieren des Launchers).
2. **Inno Setup 6** – https://jrsoftware.org/isdl.php (liefert `ISCC.exe`).
3. Internetzugang (lädt die relocatable Python-Laufzeit).

## Bauen

```powershell
cd build
powershell -ExecutionPolicy Bypass -File build.ps1
```

Das Skript:
1. lädt eine relocatable CPython-Laufzeit (python-build-standalone) nach
   `build\runtime\python\` und installiert dort `requirements.txt`,
2. friert `launcher.py` mit **PyInstaller** zu `build\dist\PortfolioPro.exe` ein,
3. kompiliert `installer.iss` → **`build\Output\PortfolioPro-Setup-<version>.exe`**.

> Läuft der Build erneut und die Laufzeit ist schon vorhanden:
> `build.ps1 -SkipRuntime` überspringt den langen Download-/Install-Schritt.

Optional: eine `icon.ico` in `build\` ablegen – sie wird automatisch für Launcher
und Setup verwendet.

## Installations-Layout beim Freund

```
%LOCALAPPDATA%\Programs\PortfolioPro\        <- Code (wird bei Updates ersetzt)
    PortfolioPro.exe        (Launcher, Startmenü/Desktop-Verknüpfung)
    app\                    (App-Quellcode)
    runtime\python\         (gebündelte Python-Laufzeit inkl. Bibliotheken)

%LOCALAPPDATA%\PortfolioPro\                 <- NUTZERDATEN (bleiben immer erhalten)
    portfolio.db            (Positionen, Cash, API-Keys)
    updates\                (heruntergeladene Update-Pakete)
```

Die strikte Trennung von Code und Daten ist der Grund, warum Updates die Daten des
Freundes nie überschreiben.

## Versionen & Updates

- Die Version steht **nur** in `core/version.py` (`APP_VERSION`). `build.ps1` liest
  sie automatisch und setzt sie als Installer-Version.
- Für ein neues Release:
  1. `APP_VERSION` erhöhen (z. B. `1.1.0`).
  2. Auf GitHub ein Release mit Tag `v1.1.0` anlegen und ein **Code-Zip**
     (`portfolio_pro-1.1.0.zip`, Inhalt = Repo-Quellcode inkl. `app.py`) als Asset
     anhängen. (GitHub hängt sonst automatisch einen Zipball an, der ebenfalls
     funktioniert.)
  3. `core/updater.py` → `GITHUB_REPO` muss auf dein öffentliches Repo zeigen.
- Der Freund sieht das Update dann unter **Einstellungen → Updates** (bzw. als
  Hinweis in der Seitenleiste) und aktualisiert per Klick. Ein neuer `Setup.exe`-
  Versand ist nur nötig, wenn sich die **Python-Version der Laufzeit** ändert.
