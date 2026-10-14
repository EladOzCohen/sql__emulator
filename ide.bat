@echo off
REM Double-click to launch the SQLPad IDE (native window).
REM Optional dialect: pass standard | mysql | bigquery (default standard;
REM you can also switch it inside the IDE).
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\anaconda3\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
set "DIALECT=%~1"
if "%DIALECT%"=="" (
  start "" "%PY%" -m sqlpad ide
) else (
  start "" "%PY%" -m sqlpad -d %DIALECT% ide
)
endlocal
