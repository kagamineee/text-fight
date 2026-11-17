@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo   YapArena - Launcher
echo ==========================================
echo.

REM ---- 1. Check Python ----
python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.11+ not found.
    echo         Install from https://www.python.org/downloads/ and enable "Add to PATH".
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo [1/4] Python %PYVER% OK

REM ---- 2. Virtual env ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual env .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual env.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Virtual env found
)

REM ---- 3. Dependencies ----
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, yaml, argon2" 2>nul
if errorlevel 1 (
    echo [3/4] Installing dependencies ...
    ".venv\Scripts\python.exe" -m pip install -r server\requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies. Check your network.
        pause
        exit /b 1
    )
) else (
    echo [3/4] Dependencies ready
)

REM ---- 4. Frontend build ----
if not exist "web\dist\index.html" (
    echo [4/4] Frontend build not found, building ...
    where npm >nul 2>nul
    if errorlevel 1 (
        echo [WARN] Node.js not installed, cannot build frontend.
    ) else (
        pushd web
        if not exist "node_modules" call npm install
        call npm run build
        popd
    )
) else (
    echo [4/4] Frontend build ready
)

REM ---- Addresses ----
echo.
echo ------------------------------------------
echo   Local:   http://localhost:8000
for /f "usebackq delims=" %%i in (`python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()"`) do (
    echo   LAN:     http://%%i:8000
)
where tailscale >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%t in ('tailscale ip -4 2^>nul') do (
        echo   Tailscale: http://%%t:8000
    )
)
echo ------------------------------------------
echo   Friends in the same Tailnet can join with the address above.
echo   Close this window to stop the server.
echo ------------------------------------------
echo.

start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:8000'"
".venv\Scripts\python.exe" -m uvicorn server.app:app --host 0.0.0.0 --port 8000

pause
