@echo off
rem Entfernt beide Autostart-Aufgaben wieder (mit Administratorrechten).
powershell -NoProfile -Command "Start-Process cmd -Verb RunAs -ArgumentList '/c schtasks /Delete /TN \"PortfolioPro Bot-Runner\" /F & schtasks /Delete /TN \"PortfolioPro App\" /F & pause'"
