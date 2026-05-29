@echo off
echo ============================================
echo  Building Log Viewer executable...
echo ============================================
echo.

call "%~dp0.venv\Scripts\activate.bat"

pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing PyInstaller...
    pip install pyinstaller --quiet
)

echo [INFO] Building... (this takes ~2-5 minutes)
pyinstaller log_viewer.spec --noconfirm

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo [INFO] Copying config files to dist folder...
copy /y "%~dp0servers.json.example" "%~dp0dist\log_viewer\servers.json.example" >nul
if exist "%~dp0servers.json" copy /y "%~dp0servers.json" "%~dp0dist\log_viewer\servers.json" >nul

echo.
echo ============================================
echo  BUILD COMPLETE!
echo.
echo  Output: dist\log_viewer\
echo.
echo  To deploy to another PC:
echo    1. Copy the entire "dist\log_viewer\" folder
echo    2. Edit servers.json (rename from servers.json.example)
echo    3. Double-click log_viewer.exe
echo ============================================
pause
