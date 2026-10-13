@echo off
setlocal
rem Resolve project-relative paths even when this script is launched from another folder.
cd /d "%~dp0"
rem Prefer the Windows Python launcher, with python.exe as a fallback.
where py >nul 2>nul
if errorlevel 1 (
  python app.py
) else (
  py -3 app.py
)
if errorlevel 1 (
  echo.
  echo Could not start the portal. Install Python 3.10 or newer and try again.
  pause
)
