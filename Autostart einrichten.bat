@echo off
rem Startet die Einrichtung mit Administratorrechten (noetig fuer den
rem Ausloeser "Beim Systemstart").
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0autostart\autostart_einrichten.ps1\"'"
