@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup_windows.bat first.
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
set "GEE_PROJECT="
echo.
echo Add or update a project-specific GEE credential.
echo The program prints an authorization URL and does not open a browser.
set /p GEE_PROJECT=Enter GEE project ID, for example ee-wyhao026: 
if "%GEE_PROJECT%"=="" (
  echo No project ID entered. Cancelled.
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml auth-project --project "%GEE_PROJECT%"
echo.
pause
