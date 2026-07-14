# Baut die komplette Setup.exe für Portfolio Pro in drei Schritten:
#   1) gebündelte Python-Laufzeit + Bibliotheken holen (fetch_runtime.ps1)
#   2) Launcher (build\launcher.py) mit PyInstaller zu PortfolioPro.exe einfrieren
#   3) Inno Setup (installer.iss) kompilieren -> build\Output\PortfolioPro-Setup-<ver>.exe
#
# Nutzung (im build\-Ordner):
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Voraussetzungen:
#   - Python auf dem PATH (zum Einfrieren des Launchers via PyInstaller)
#   - Inno Setup 6 installiert (ISCC.exe), siehe README.md

param(
    [switch]$SkipRuntime  # überspringt Schritt 1, falls die Laufzeit schon existiert
)

$ErrorActionPreference = "Stop"
$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir  = Split-Path -Parent $BuildDir

# App-Version aus core\version.py lesen (Single Source of Truth)
$verLine = Get-Content (Join-Path $RepoDir "core\version.py") | Where-Object { $_ -match 'APP_VERSION' }
if ($verLine -match '"([0-9]+\.[0-9]+\.[0-9]+)"') { $Version = $Matches[1] } else { throw "APP_VERSION nicht gefunden." }
Write-Host "Baue Portfolio Pro v$Version"

# 1) Laufzeit
if (-not $SkipRuntime) {
    & (Join-Path $BuildDir "fetch_runtime.ps1")
} else {
    Write-Host "Schritt 1 übersprungen (-SkipRuntime)."
}

# 2) Launcher einfrieren
Write-Host "Friere Launcher mit PyInstaller ein ..."
python -m pip install --upgrade pyinstaller | Out-Null
$iconArg = @()
if (Test-Path (Join-Path $BuildDir "icon.ico")) { $iconArg = @("--icon", (Join-Path $BuildDir "icon.ico")) }
python -m PyInstaller --onefile --name PortfolioPro `
    --distpath (Join-Path $BuildDir "dist") `
    --workpath (Join-Path $BuildDir "_pybuild") `
    --specpath (Join-Path $BuildDir "_pybuild") `
    @iconArg (Join-Path $BuildDir "launcher.py")

# 3) Inno Setup kompilieren
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $guess = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $guess) { $iscc = $guess } else { throw "ISCC.exe (Inno Setup 6) nicht gefunden – bitte installieren." }
} else { $iscc = $iscc.Source }

Write-Host "Kompiliere Installer ..."
& $iscc "/DAppVersion=$Version" (Join-Path $BuildDir "installer.iss")

Write-Host ""
Write-Host "FERTIG: build\Output\PortfolioPro-Setup-$Version.exe"
