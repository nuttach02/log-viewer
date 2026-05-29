@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  Log Viewer - Auto Setup ^& Start
echo ============================================
echo.

:: Check if existing .venv works (python + uvicorn both present)
"%~dp0.venv\Scripts\python.exe" --version >nul 2>&1
if %errorlevel% == 0 (
    if exist "%~dp0.venv\Scripts\uvicorn.exe" (
        echo [OK] Found working virtual environment.
        goto :start_server
    )
    echo [INFO] Virtual environment incomplete - reinstalling packages...
    goto :install_packages
)

echo [INFO] Virtual environment not found or broken. Setting up...
echo.

:: Try to find Python in PATH
set PYTHON_CMD=
for %%p in (python python3 py) do (
    if "!PYTHON_CMD!" == "" (
        %%p --version >nul 2>&1
        if !errorlevel! == 0 (
            set PYTHON_CMD=%%p
        )
    )
)

if "!PYTHON_CMD!" == "" (
    echo [ERROR] Python not found! Please install Python 3.9+ from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Show Python version found
echo [INFO] Using:
!PYTHON_CMD! --version

:: Create new virtual environment
echo [INFO] Creating virtual environment...
if exist "%~dp0.venv" rmdir /s /q "%~dp0.venv"
!PYTHON_CMD! -m venv "%~dp0.venv"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

:install_packages
:: Install requirements — use local packages\ folder if present (offline mode)
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
if exist "%~dp0packages\" (
    echo [INFO] Installing packages from local packages\ folder (offline mode)...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" --no-index --find-links "%~dp0packages"
) else (
    echo [INFO] Installing packages from internet (this may take a few minutes)...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
)
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install packages. See errors above.
    echo         If no internet: run download_packages.bat on a PC with internet first.
    pause
    exit /b 1
)

echo [OK] Setup complete!
echo.

:start_server
:: Add firewall rule if not already present (requires admin; silently skipped if not)
netsh advfirewall firewall show rule name="Log Viewer Port 8000" >nul 2>&1
if %errorlevel% neq 0 (
    netsh advfirewall firewall add rule name="Log Viewer Port 8000" protocol=TCP dir=in localport=8000 action=allow >nul 2>&1
)

echo ============================================
echo  Log Viewer starting on all interfaces...
echo  Access it from this PC or other PCs at:
echo.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set ip=%%a
    set ip=!ip: =!
    echo    http://!ip!:8000
)
echo ============================================
echo.

"%~dp0.venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port 8000
pause
