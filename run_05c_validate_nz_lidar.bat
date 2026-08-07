@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto cannot_enter_project
if not exist ".venv\Scripts\python.exe" goto missing_environment

set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

call ".venv\Scripts\python.exe" -m mangrove_terrain windows-guide nz_lidar_validation --confirm
set "GUIDE_EXIT=%ERRORLEVEL%"
if "%GUIDE_EXIT%"=="0" goto run_validation
if "%GUIDE_EXIT%"=="2" goto cancelled
set "RUN_EXIT=%GUIDE_EXIT%"
goto failed

:run_validation
call ".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml validate-nz-lidar
set "RUN_EXIT=%ERRORLEVEL%"
if "%RUN_EXIT%"=="0" goto success
goto failed

:success
echo.
echo Validation completed. Read the Chinese output and README for result paths.
goto finish

:cancelled
set "RUN_EXIT=0"
echo.
echo Cancelled. No source data were changed.
goto finish

:missing_environment
set "RUN_EXIT=1"
echo.
echo Project environment was not found. Run setup_windows.bat first.
goto finish

:cannot_enter_project
set "RUN_EXIT=1"
echo.
echo Unable to enter the project folder.
goto finish

:failed
echo.
echo Validation failed. Read the error above; this window will remain open.

:finish
echo.
echo Press any key to close this window.
pause >nul
exit /b %RUN_EXIT%
