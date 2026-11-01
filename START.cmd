@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run INSTALL.cmd first. See README_RU.md.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" launch.py
