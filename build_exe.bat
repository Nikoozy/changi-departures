@echo off
rem ---------------------------------------------------------------------------
rem  Builds dist\ChangiDepartures\ChangiDepartures.exe  (no Python needed on store PCs).
rem  Run this once on a Windows PC that has Python 3.12. Then copy the whole
rem  dist\ChangiDepartures folder to each store desktop (e.g. C:\ChangiDepartures).
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv || python -m venv .venv || (echo Python 3.12 is not installed. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || (pause & exit /b 1)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean changi_departures.spec || (echo Build failed. & pause & exit /b 1)
if not exist "dist\ChangiDepartures\ChangiDepartures.exe" (
    echo.
    echo The build finished but dist\ChangiDepartures\ChangiDepartures.exe is missing.
    echo Antivirus often removes newly built programs: check Windows Security ^> Protection history.
    pause & exit /b 1
)
copy /y settings.example.json dist\ChangiDepartures\settings.json >nul
echo.
echo ================================================================
echo  Done. The app is:  %CD%\dist\ChangiDepartures\ChangiDepartures.exe
echo  (ChangiDepartures.spec in this folder is only the build recipe.)
echo  Copy the WHOLE folder  dist\ChangiDepartures  to each store desktop,
echo  and set the store's terminal in settings.json inside it.
echo ================================================================
explorer "dist\ChangiDepartures"
pause
