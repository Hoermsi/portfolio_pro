# Lädt eine relocatable CPython-Laufzeit (python-build-standalone) herunter und
# installiert die App-Abhängigkeiten hinein. Ergebnis: build\runtime\python\ enthält
# eine vollständige, an jeden Ort verschiebbare Python-Installation inkl. Bibliotheken.
#
# Nutzung (im build\-Ordner):  powershell -ExecutionPolicy Bypass -File fetch_runtime.ps1
#
# Voraussetzung: Internetzugang. tar ist in Windows 10/11 enthalten.

param(
    [string]$PythonMinor = "3.12"
)

$ErrorActionPreference = "Stop"
$BuildDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir    = Split-Path -Parent $BuildDir
$RuntimeDir = Join-Path $BuildDir "runtime"
$Downloads  = Join-Path $BuildDir "_downloads"

New-Item -ItemType Directory -Force -Path $RuntimeDir, $Downloads | Out-Null

Write-Host "Suche neueste python-build-standalone-Version für Python $PythonMinor ..."
$rel = Invoke-RestMethod "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" `
    -Headers @{ "Accept" = "application/vnd.github+json" }

$pattern = "cpython-$([regex]::Escape($PythonMinor))\.\d+\+.*x86_64-pc-windows-msvc-install_only\.tar\.gz$"
$asset = $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
if (-not $asset) {
    throw "Kein passendes Windows-Asset für Python $PythonMinor im neuesten Release gefunden."
}

$archive = Join-Path $Downloads $asset.name
Write-Host "Lade $($asset.name) ..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive

# Vorhandene Laufzeit ersetzen
$PythonDir = Join-Path $RuntimeDir "python"
if (Test-Path $PythonDir) { Remove-Item -Recurse -Force $PythonDir }

Write-Host "Entpacke Laufzeit ..."
# Das install_only-Archiv enthält einen Top-Level-Ordner "python\"
tar -xzf $archive -C $RuntimeDir

$PyExe = Join-Path $PythonDir "python.exe"
if (-not (Test-Path $PyExe)) { throw "python.exe nicht gefunden unter $PyExe" }

Write-Host "Installiere App-Abhängigkeiten in die Laufzeit ..."
& $PyExe -m pip install --upgrade pip
& $PyExe -m pip install -r (Join-Path $RepoDir "requirements.txt")

# Test-Only-Abhängigkeit im Auslieferungs-Runtime nicht nötig
& $PyExe -m pip uninstall -y pytest 2>$null

Write-Host ""
Write-Host "Fertig. Laufzeit liegt in: $PythonDir"
& $PyExe --version
