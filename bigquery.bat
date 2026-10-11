@echo off
REM Double-click to start the watcher straight in BigQuery (GoogleSQL) mode,
REM editing query_bigquery.sql. No menu.
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m sqlpad -d bigquery watch
endlocal
