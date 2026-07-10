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
echo Stage 1 test: one complete 6-degree GEDI point asset for 2019-2025.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml export-gedi-assets --tiles 102W_012N --years 2019-2025 --year-mode all
echo Use run_03d_check_staged_tasks.bat to inspect the task.
echo.
pause
