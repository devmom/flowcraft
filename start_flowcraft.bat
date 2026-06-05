@echo off
chcp 65001 >nul
cd /d "%~dp0core"

echo   FlowCraft v0.1.0
echo.

:: Kill old
for /f "tokens=5" %%a in ('netstat -ano ^| find ":8765" ^| find "LISTENING"') do taskkill /f /pid %%a >nul 2>nul

:: Start
start "" ".venv\Scripts\python.exe" -m flowcraft_core.simple_server

:: Wait + open browser
timeout /t 4 /nobreak >nul
start http://127.0.0.1:8765

echo   Done. Browser should open shortly.
pause
