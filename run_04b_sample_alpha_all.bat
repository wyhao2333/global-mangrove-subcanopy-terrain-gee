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
echo Stage 2: sample AlphaEarth from completed GEDI point assets.
echo Failed or incomplete GEDI assets will be skipped until they are ready.
choice /C YN /N /M "Continue and submit AlphaEarth chunk tasks? [Y/N]: "
if errorlevel 2 (
  echo Cancelled. No task was submitted.
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml sample-alpha-assets --years 2019-2025 --year-mode all
echo.
pause
