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
echo This submits Google Drive export tasks. Default max_new_tasks is controlled in config.yaml.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml sample --mode drive --years 2019-2025 --year-mode all
echo.
pause
