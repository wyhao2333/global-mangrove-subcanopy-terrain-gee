@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击运行 setup_windows.bat 安装运行环境。
  pause
  exit /b 1
)
set PYTHONPATH=%~dp0src
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m mangrove_terrain windows-guide egm2008 --confirm
if errorlevel 2 goto done
".venv\Scripts\python.exe" -m mangrove_terrain --config config.yaml convert-egm2008
:done
echo.
pause
