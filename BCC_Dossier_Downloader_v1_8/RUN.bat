@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run INSTALL_AND_RUN.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
