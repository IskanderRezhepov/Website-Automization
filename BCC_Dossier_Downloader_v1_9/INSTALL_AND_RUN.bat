@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo BCC Electronic Credit Dossier Downloader - setup and launch
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ and tick "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)

echo Installing/updating Python packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo The app first tries installed Microsoft Edge / Google Chrome.
echo Playwright Chromium is optional and is not installed by this script.
echo.
".venv\Scripts\python.exe" main.py
exit /b 0

:error
echo.
echo Setup failed. Copy the error above and send it to the developer.
pause
exit /b 1
