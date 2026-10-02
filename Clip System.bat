@echo off
rem Double-click launcher for the Clip System desktop app.
cd /d "%~dp0"

rem First-run: install GUI deps (flask + pywebview) if missing.
py -3 -c "import flask, webview" 1>nul 2>nul
if errorlevel 1 (
  echo Installing GUI dependencies ^(first run only^)...
  py -3 -m pip install -r "gui\requirements-gui.txt"
)

py -3 gui\app.py
if errorlevel 1 pause
