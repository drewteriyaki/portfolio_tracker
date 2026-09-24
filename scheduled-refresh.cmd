@echo off
rem Silent, non-interactive Finnhub price refresh - meant for Windows Task
rem Scheduler (see setup-scheduled-tasks.ps1), not for double-clicking.
rem No `pause`, output goes to logs\refresh.log instead of a console window.
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo [%date% %time%] refresh start >> "%~dp0logs\refresh.log"
"%PYEXE%" "%~dp0update_prices.py" --db "%~dp0portfolio.db" --user admin1 >> "%~dp0logs\refresh.log" 2>&1
echo [%date% %time%] refresh exit %errorlevel% >> "%~dp0logs\refresh.log"
