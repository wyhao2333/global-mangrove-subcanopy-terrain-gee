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
echo Stage 1: submit GEDI point-table assets. Default max_new_tasks is controlled in config.yaml.
echo Run run_04b_sample_alpha_all.bat after these assets become COMPLETED.
choice /C YN /N /M "Continue and submit GEDI asset tasks? [Y/N]: "
if errorlevel 2 (
  echo Cancelled. No task was submitted.
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml export-gedi-assets --years 2019-2025 --year-mode all
echo.
pause
