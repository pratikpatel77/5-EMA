@echo off
rem Starts the 5 EMA monthly desk and opens it in your browser.
title 5 EMA Monthly Desk
cd /d "%~dp0"

python -c "import pandas, bs4, lxml" 2>nul
if errorlevel 1 (
  echo Installing what the desk needs...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install the requirements. Check that Python is on your PATH.
    pause
    exit /b 1
  )
)

echo Starting the 5 EMA monthly desk...
python -m ema5 %*
if errorlevel 1 pause
