@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -ListenAddress 0.0.0.0 -Port 8766
