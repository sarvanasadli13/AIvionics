@echo off
REM ===================================================================
REM  AIvionics - AI-assisted engineering workstation
REM
REM  Double-click to start. Uses pythonw so no console window appears
REM  alongside the application.
REM
REM  Runs against the demo database by default. To use your own corpus,
REM  run without --db:   python -m aivionics.ui
REM ===================================================================
title AIvionics

cd /d "%~dp0"

set "DEMO_DB=data\demo\aivionics-demo.db"

REM Prefer pythonw (no console). Fall back to python if it is not found.
set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"
if not defined PYW set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"

if not exist "%PYW%" (
    echo Could not find pythonw.exe.
    echo Install Python 3.11+ and make sure it is on your PATH.
    pause
    exit /b 1
)

if exist "%DEMO_DB%" (
    start "" "%PYW%" -m aivionics.ui --db "%DEMO_DB%"
) else (
    echo No demo database found at %DEMO_DB%.
    echo Starting against the default corpus instead.
    echo Build the demo with:  python scripts\make_demo_db.py
    echo.
    start "" "%PYW%" -m aivionics.ui
)

exit /b 0
