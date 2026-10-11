@echo off
REM Double-click to start the interactive SQL REPL. Asks which dialect to
REM practice (Standard / MySQL / BigQuery) first.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m sqlpad repl
endlocal
