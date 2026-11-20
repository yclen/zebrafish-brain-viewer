@echo off
rem Serves this folder on http://localhost:8765 and opens the viewer in the default browser.
rem Close this window to stop the server.
cd /d "%~dp0"
start "" http://localhost:8765/
python -m http.server 8765
