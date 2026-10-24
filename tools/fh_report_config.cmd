@echo off
setlocal EnableDelayedExpansion
rem Fh_Report configuration editor.
rem Usage: fh_report_config.cmd [DCSServerBot folder]   (without it: the last folder used, or a question)
rem 1) Fh_Report_Config.exe next to this file (or in dist\) runs on its own, no Python needed.
rem 2) Otherwise the first Python of this computer that has tkinter and ruamel.yaml is used.

set "HERE=%~dp0"
for %%E in ("%HERE%Fh_Report_Config.exe" "%HERE%dist\Fh_Report_Config.exe") do (
    if exist %%E (
        start "" %%E %*
        exit /b 0
    )
)

set "PY="
call :try "%USERPROFILE%\.dcssb\Scripts\python.exe"
if not defined PY call :try python
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PY call :try "%%~D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PY call :try "%%~D\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do if not defined PY call :try "%%~D\python.exe"

if not defined PY (
    echo The configuration editor needs Python with tkinter and could not find one.
    echo Install Python 3.11 or later from python.org ^(keep "tcl/tk and IDLE" ticked^) and open this again,
    echo or use Fh_Report_Config.exe if your release comes with it.
    powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show('The configuration editor needs Python with tkinter, and none was found on this computer. Install Python 3.11 or later from python.org (keep the tcl/tk option ticked), then open the editor again.', 'Fh_Report configuration')" >nul 2>&1
    pause
    exit /b 1
)

set "PYW=!PY:python.exe=pythonw.exe!"
if /i "!PY!"=="python" set "PYW=pythonw"
start "" "!PYW!" "%HERE%fh_report_config.pyw" %*
exit /b 0

:try
rem Is %~1 a Python with tkinter and ruamel.yaml? (ruamel.yaml is installed for the user if only that is missing.)
if /i not "%~1"=="python" if not exist "%~1" goto :eof
"%~1" -c "import tkinter" >nul 2>&1 || goto :eof
"%~1" -c "import ruamel.yaml" >nul 2>&1 || "%~1" -m pip install --user --quiet ruamel.yaml >nul 2>&1
"%~1" -c "import ruamel.yaml" >nul 2>&1 && set "PY=%~1"
goto :eof
