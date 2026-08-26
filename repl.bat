@echo off
REM Double-click to start the interactive SQL REPL.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m sqlpad repl
endlocal
