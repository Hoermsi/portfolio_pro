# Legt zwei Windows-Aufgaben an, die App und Bot-Runner beim Systemstart
# starten - auch ohne Anmeldung (z.B. nach einem naechtlichen Update-Neustart).
# Anmeldeart S4U: laeuft als dein Benutzer, ohne dass ein Passwort
# gespeichert wird (reicht fuer lokale Platte + Internet).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host ""
Write-Host "Autostart fuer Portfolio Pro einrichten (Benutzer: $user)" -ForegroundColor Cyan

# Alte Aufgabe zeigte noch auf "D:\Portfolio Management\portfolio_pro", den
# es nicht mehr gibt - sie startete beim Hochfahren nur ein haengendes
# wscript und sonst nichts.
$old = Get-ScheduledTask -TaskName 'PortfolioProAutoStart' -ErrorAction SilentlyContinue
if ($old) {
    Stop-ScheduledTask -TaskName 'PortfolioProAutoStart' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'PortfolioProAutoStart' -Confirm:$false
    Write-Host "Alte, defekte Aufgabe 'PortfolioProAutoStart' entfernt." -ForegroundColor Yellow
}

# Keine Laufzeitbegrenzung (Standard waere 72 h -> Bot wuerde nach 3 Tagen
# beendet), auch im Akkubetrieb, keine Doppelstarts.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$tasks = @(
    @{ Name = 'PortfolioPro Bot-Runner'; Bat = 'autostart\bot_runner_autostart.bat';
       Desc = 'Startet den Trading-Bot-Runner beim Hochfahren, auch ohne Anmeldung.' },
    @{ Name = 'PortfolioPro App';        Bat = 'autostart\app_autostart.bat';
       Desc = 'Startet die Portfolio-Pro-App (http://localhost:8501) beim Hochfahren, auch ohne Anmeldung.' }
)

foreach ($t in $tasks) {
    $batPath = Join-Path $root $t.Bat
    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$batPath`"" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT1M'
    Register-ScheduledTask -TaskName $t.Name -Description $t.Desc -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Aufgabe angelegt: $($t.Name)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Fertig. Ab dem naechsten Neustart laufen App und Bot automatisch."
Write-Host "App im Browser: http://localhost:8501"
Write-Host "Logs: $env:LOCALAPPDATA\PortfolioPro\bot_runner.log und streamlit.log"
