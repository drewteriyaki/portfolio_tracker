@echo off
rem Double-click to print the portfolio summary. Window stays open until you press a key.
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0portfolio.py" report --db "%~dp0portfolio.db" %*
echo.
pause
