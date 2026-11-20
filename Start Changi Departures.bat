@echo off
rem ---------------------------------------------------------------------------
rem  Changi Departures - start the app using Python (for the person maintaining it).
rem  Store desktops should use the packaged ChangiDepartures.exe instead (see README).
rem  First run creates a private Python environment and installs the libraries.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up. This takes a minute...
    py -3.12 -m venv .venv || python -m venv .venv || (echo Python 3.12 is not installed. & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Installing libraries failed. & pause & exit /b 1)
)
".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
