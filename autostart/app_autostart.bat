@echo off
rem Wird von der Windows-Aufgabe "PortfolioPro App" beim Systemstart
rem ausgefuehrt (unsichtbar, ohne Anmeldung). Die App ist danach im Browser
rem unter http://localhost:8501 erreichbar.
cd /d "%~dp0.."
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "LOGDIR=%LOCALAPPDATA%\PortfolioPro"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set PYTHONIOENCODING=utf-8
py -m streamlit run app.py --server.headless true --server.port 8501 >> "%LOGDIR%\streamlit.log" 2>&1
