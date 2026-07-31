@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto cannot_enter_project
if not exist ".venv\Scripts\python.exe" goto missing_environment

set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

call ".venv\Scripts\python.exe" -m mangrove_terrain windows-guide egm2008 --confirm
set "GUIDE_EXIT=%ERRORLEVEL%"
if "%GUIDE_EXIT%"=="0" goto run_conversion
if "%GUIDE_EXIT%"=="2" goto cancelled
set "RUN_EXIT=%GUIDE_EXIT%"
goto failed

:run_conversion
call ".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml convert-egm2008
set "RUN_EXIT=%ERRORLEVEL%"
if "%RUN_EXIT%"=="0" goto success
goto failed

:success
echo.
echo EGM2008 conversion completed.
goto finish

:cancelled
set "RUN_EXIT=0"
echo.
echo Cancelled. No files were changed.
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
echo The conversion failed. Read the error above; this window will remain open.

:finish
echo.
echo Press any key to close this window.
pause >nul
exit /b %RUN_EXIT%
