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
echo This submits ONE direct native-tile task as a fallback comparison.
echo The recommended workflow is the staged asset workflow in run_03e/run_03f.
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml sample-native --mode drive --tiles 102W_012N --years 2019-2025 --year-mode all
echo.
pause
