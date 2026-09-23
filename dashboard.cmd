@echo off
rem Double-click to open the Streamlit dashboard in your browser.
rem Closes when you close the terminal window or press Ctrl+C.
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" -m streamlit run "%~dp0dashboard.py"
echo.
pause
