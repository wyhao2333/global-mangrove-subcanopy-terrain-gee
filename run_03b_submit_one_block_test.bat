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
echo This submits ONE real shard task for all GEDI years to Google Drive.
echo It is used to test whether the current GMW shard size works in GEE.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml sample --mode drive --max-shards 1 --years 2019-2025 --year-mode all
echo.
pause
