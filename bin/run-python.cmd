@echo off
rem Launch memgraph.cli with one Python start: `py -3` runs it directly (memgraph.cli exits
rem quietly on Python older than 3.10). Only when `py` is missing or cannot start a Python
rem (launcher exit codes 100 and up; the CLI itself never returns those) try `python`.
setlocal
set "PYTHONPATH=%~dp0.."
where py >nul 2>&1
if errorlevel 1 goto trypython
py -3 -m memgraph.cli %*
if errorlevel 100 goto trypython
exit /b %errorlevel%
:trypython
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 exit /b 0
python -m memgraph.cli %*
exit /b %errorlevel%
