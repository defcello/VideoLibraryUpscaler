@echo off
cd /d "%~dp0"
start "AI Remaster Pipeline Server" python -m app.server
timeout /t 3 /nobreak >nul
start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" "http://127.0.0.1:8756"
