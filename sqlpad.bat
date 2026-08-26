@echo off
REM SQLPad launcher. Finds your Anaconda Python and runs the package.
REM Usage: sqlpad [watch|repl|import|run|tables] [args...]

setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\Anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m sqlpad %*
endlocal
