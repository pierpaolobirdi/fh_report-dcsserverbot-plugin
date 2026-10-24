@echo off
setlocal
rem Fh_Report configuration editor. Uses the Python of DCSServerBot (it already has PyYAML and ruamel.yaml).
rem Usage: fh_report_config.cmd [path to the DCSServerBot folder]   (without it, the last folder used, or a question)

set "PY="
set "PYW="
if exist "%USERPROFILE%\.dcssb\Scripts\python.exe" (
    set "PY=%USERPROFILE%\.dcssb\Scripts\python.exe"
    set "PYW=%USERPROFILE%\.dcssb\Scripts\pythonw.exe"
)
if "%PY%"=="" (
    where python >nul 2>&1 && set "PY=python"
    where pythonw >nul 2>&1 && set "PYW=pythonw"
)
if "%PYW%"=="" set "PYW=%PY%"
if "%PY%"=="" (
    echo Python was not found. Install Python 3.11 or later, or run this from a machine with DCSServerBot.
    pause
    exit /b 1
)

set "MISSING="
"%PY%" -c "import ruamel.yaml" >nul 2>&1 || set "MISSING=%MISSING% ruamel.yaml"
"%PY%" -c "import tkinter" >nul 2>&1 || set "MISSING=%MISSING% tkinter"
if defined MISSING (
    echo The Python found is missing:%MISSING%
    echo   Python: %PY%
    echo ruamel.yaml comes with DCSServerBot ^(%PY% -m pip install ruamel.yaml^).
    echo tkinter comes with the python.org installer ^(Modify the installation and tick "tcl/tk and IDLE"^).
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0fh_report_config.pyw" %*
