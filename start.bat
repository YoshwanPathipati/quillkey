@echo off
title QuillKey
cd /d "%~dp0"

echo ==========================================
echo   QuillKey - starting up
echo ==========================================
echo.

echo [1/4] Starting LanguageTool (Docker)...
docker compose up -d
if errorlevel 1 (
    echo.
    echo   ERROR: Docker failed. Is Docker Desktop running?
    pause
    exit /b 1
)

echo [2/4] Checking Ollama...
curl -s -o nul http://localhost:11434/api/tags
if errorlevel 1 (
    echo   WARNING: Ollama is not responding. Start it with: ollama serve
    echo   AI style coaching and rewriting will be off until Ollama is running.
) else (
    echo   Ollama is up.
)

echo [3/4] Starting backend on http://127.0.0.1:8765 ...
tasklist /fi "WINDOWTITLE eq quillkey-backend*" 2>nul | find "cmd.exe" >nul
if errorlevel 1 (
    start "quillkey-backend" /min cmd /c "cd backend && python main.py"
)

echo   Waiting for backend...
:wait_backend
timeout /t 1 /nobreak >nul
curl -s -o nul http://127.0.0.1:8765/health
if errorlevel 1 goto wait_backend
echo   Backend is up.

echo [4/4] Starting QuillKey (tray icon)...
start "" pythonw desktop\app.py

echo.
echo ------------------------------------------------------------
echo  QuillKey is running in your system tray.
echo    - Click into any text field: errors get underlined and a
echo      dot shows the issue count. Click the dot to fix them.
echo    - Misspelled words auto-fix as you type, anywhere.
echo    - Ctrl+Alt+G: fix all errors in selected text
echo    - Ctrl+Alt+R: rewrite selected text with local AI
echo    - Right-click the tray icon for modes and toggles
echo ------------------------------------------------------------
timeout /t 15
