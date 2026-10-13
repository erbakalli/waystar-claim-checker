@echo off
cd /d "%~dp0"
if exist "dist\Waystar Claim Checker.exe" (
  start "" "dist\Waystar Claim Checker.exe"
  exit /b 0
)
if not exist ".venv\Scripts\python.exe" (
  echo Follow the Python setup steps in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
