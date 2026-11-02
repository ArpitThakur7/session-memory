@echo off
rem Launch memgraph.cli with the first usable Python >= 3.10 (skips the Windows Store stub).
setlocal
set "PYTHONPATH=%~dp0.."
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto trypython
py -3 -m memgraph.cli %*
exit /b %errorlevel%
:trypython
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 exit /b 0
python -m memgraph.cli %*
exit /b %errorlevel%
