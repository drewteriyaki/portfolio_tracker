@echo off
rem Silent, non-interactive Yahoo history sync - meant for Windows Task
rem Scheduler (see setup-scheduled-tasks.ps1), not for double-clicking.
rem No `pause`, output goes to logs\sync.log instead of a console window.
rem Takes a couple of minutes (daily + intraday bars + fundamentals for
rem every held ticker) - run this once a day, not every few minutes.
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo [%date% %time%] sync start >> "%~dp0logs\sync.log"
"%PYEXE%" "%~dp0sync_history.py" --db "%~dp0portfolio.db" >> "%~dp0logs\sync.log" 2>&1
echo [%date% %time%] sync exit %errorlevel% >> "%~dp0logs\sync.log"
