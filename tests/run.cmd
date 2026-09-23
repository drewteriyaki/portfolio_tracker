@echo off
rem Run the test suite. Double-click, or:  tests\run.cmd
setlocal
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
pushd "%~dp0.."
"%PYEXE%" -m unittest discover -s tests -v
popd
echo.
pause
