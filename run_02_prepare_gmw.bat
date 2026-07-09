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
echo This step may take a long time for the full GMW shapefile.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml prepare-gmw --all
echo.
pause
