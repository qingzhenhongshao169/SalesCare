@echo off
cd /d "%~dp0"
".mvp-venv\Scripts\python.exe" scripts\run_mvp.py --host 0.0.0.0 --port 8767
