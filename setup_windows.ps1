$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "Project folder: $ProjectDir"

$PythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    cmd /c "python --version >nul 2>nul"
    if ($LASTEXITCODE -eq 0) {
        $PythonCmd = "python"
    }
}

if ($null -eq $PythonCmd -and (Get-Command py -ErrorAction SilentlyContinue)) {
    cmd /c "py -3.11 --version >nul 2>nul"
    if ($LASTEXITCODE -eq 0) {
        $PythonCmd = "py -3.11"
    }
}

if ($null -eq $PythonCmd) {
    throw "Python was not found. Please install Python 3.11 first, then run setup_windows.bat again."
}

if (!(Test-Path ".venv")) {
    Write-Host "Creating .venv ..."
    Invoke-Expression "$PythonCmd -m venv .venv"
}

$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (!(Test-Path $VenvPython)) {
    throw "Cannot find .venv Python: $VenvPython"
}

Write-Host "Upgrading pip ..."
& $VenvPython -m pip install --upgrade pip

Write-Host "Installing dependencies with wheel-only mode ..."
& $VenvPython -m pip install --only-binary=:all: -r requirements-windows.lock.txt

Write-Host "Installing this project package ..."
& $VenvPython -m pip install -e . --no-deps

if (!(Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml from config.example.yaml"
}

Write-Host ""
Write-Host "Setup finished."
Write-Host "Next step: double click run_01_check_gee.bat"
