@echo off
rem Double-click to fetch live prices from Finnhub and refresh unrealized G/L.
rem Needs your key in .env (FINNHUB_API_KEY=). Window stays open until you press a key.
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0update_prices.py" --db "%~dp0portfolio.db" %*
echo.
pause
