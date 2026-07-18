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
set "TARGET_PROJECT="
set "SOURCE_FOLDER="
echo Stage 2: sample AlphaEarth from completed GEDI point assets.
echo You may use this account's assets or a shared source asset folder.
set /p TARGET_PROJECT=GEE project for this run and Drive export (blank=config.yaml):
set /p SOURCE_FOLDER=GEDI source asset folder (blank=current project):
set "PROJECT_ARG="
set "SOURCE_ARG="
if not "%TARGET_PROJECT%"=="" set "PROJECT_ARG=--project %TARGET_PROJECT%"
if not "%SOURCE_FOLDER%"=="" set "SOURCE_ARG=--source-asset-folder "%SOURCE_FOLDER%""
choice /C YN /N /M "Submit AlphaEarth spatial chunk tasks? [Y/N]: "
if errorlevel 2 (
  echo Cancelled. No task was submitted.
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml %PROJECT_ARG% sample-alpha-assets --years 2019-2025 --year-mode all %SOURCE_ARG%
echo.
pause
