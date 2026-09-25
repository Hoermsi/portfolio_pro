@echo off
rem Wird von der Windows-Aufgabe "PortfolioPro Bot-Runner" beim Systemstart
rem ausgefuehrt (unsichtbar, ohne Anmeldung). Nicht fuer den Doppelklick
rem gedacht - dafuer gibt es "Start Trading Bot.bat".
cd /d "%~dp0.."
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "LOGDIR=%LOCALAPPDATA%\PortfolioPro"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\bot_runner.log"
set PYTHONIOENCODING=utf-8

rem Direkt nach dem Hochfahren ist das Netz oft noch nicht da - bot_runner.py
rem bricht dann beim Aufbau der Boersenanbindung ab und startet nicht neu.
rem Darum bis zu 10 Minuten auf Hyperliquid warten. "timeout" funktioniert
rem ohne Konsole nicht, deshalb ping als Wartezeit.
set /a TRIES=0
:waitnet
curl.exe -s -o nul --max-time 5 https://api.hyperliquid.xyz/info && goto netok
set /a TRIES+=1
if %TRIES% GEQ 60 goto netfail
ping -n 11 127.0.0.1 >nul
goto waitnet

:netfail
echo [%date% %time%] AUTOSTART: Kein Netz nach 10 Minuten - Runner wird trotzdem gestartet.>> "%LOG%"
:netok
echo [%date% %time%] AUTOSTART: Runner wird nach Systemstart gestartet.>> "%LOG%"
py bot_runner.py >> "%LOG%" 2>&1
