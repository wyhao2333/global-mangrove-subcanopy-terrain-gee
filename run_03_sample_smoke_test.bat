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
echo Comparing legacy shard sampling with native-tile sampling on one observed month.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml validate-native --year 2020 --month 3 --limit 5000
echo.
pause
