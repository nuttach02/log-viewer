@echo off
echo ============================================
echo  Downloading packages for offline install
echo ============================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run start_server.bat first to create it.
    pause
    exit /b 1
)

if exist "%~dp0packages" rmdir /s /q "%~dp0packages"
mkdir "%~dp0packages"

echo [INFO] Downloading wheels to packages\ folder...
"%~dp0.venv\Scripts\python.exe" -m pip download -r "%~dp0requirements.txt" -d "%~dp0packages"
if %errorlevel% neq 0 (
    echo [ERROR] Download failed.
    pause
    exit /b 1
)

echo.
echo [OK] Done! Copy the packages\ folder to the offline PC alongside the project.
echo      start_server.bat will automatically use it if internet is unavailable.
pause
