@echo off
rem Drag a Schwab "Positions" CSV onto this file to import it.
rem Or run it from a terminal:  import.cmd "C:\path\to\All-Accounts-Positions-....csv"
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
if "%~1"=="" (
  echo.
  echo   Drag a Schwab Positions CSV file onto this icon, or pass its path as an argument.
  echo.
  pause
  exit /b 1
)
"%PYEXE%" "%~dp0portfolio.py" import "%~1" --db "%~dp0portfolio.db" --user admin1
echo.
pause
