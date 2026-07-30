@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Run setup_windows.bat first.
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
set PYTHONUTF8=1
set "EXIT_CODE=0"
".venv\Scripts\python.exe" -m mangrove_terrain windows-guide sample_qc_evaluate --confirm
if errorlevel 2 goto done
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto done
)
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml evaluate-sample-qc-candidates
set "EXIT_CODE=%ERRORLEVEL%"
:done
echo.
pause
exit /b %EXIT_CODE%
