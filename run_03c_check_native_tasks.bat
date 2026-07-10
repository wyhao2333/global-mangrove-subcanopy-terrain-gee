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
echo Checking native GEDI tile export tasks...
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml check-native-tasks
echo.
pause
