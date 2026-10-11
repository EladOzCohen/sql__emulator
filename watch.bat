@echo off
REM Double-click to start the live watcher. Asks which dialect to practice
REM (Standard / MySQL / BigQuery), then watches that dialect's scratch file.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m sqlpad watch
endlocal
