@echo off
rem Double-click to pull ~2 years of daily price history from Yahoo Finance.
rem Needs:  pip install yfinance  (or pip install -r requirements.txt)
rem Window stays open until you press a key. Pass extra args, e.g.  sync-history.cmd --period 5y
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0sync_history.py" --db "%~dp0portfolio.db" %*
echo.
pause
