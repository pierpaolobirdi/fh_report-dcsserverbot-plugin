@echo off
setlocal
rem Builds tools\dist\Fh_Report_Config.exe by hand: the configuration editor with Python, tkinter and
rem ruamel.yaml inside, so it runs on a computer without any of them. GitHub builds the same file by itself
rem (branch "builds"); use this only to try a change locally. Run it on Windows, from anywhere.
rem Needs a Python with tkinter and internet for the first run (it installs PyInstaller and ruamel.yaml).

set "HERE=%~dp0"
set "ROOT=%HERE%.."
set "PY=python"
if exist "%USERPROFILE%\.dcssb\Scripts\python.exe" set "PY=%USERPROFILE%\.dcssb\Scripts\python.exe"

"%PY%" -c "import tkinter" >nul 2>&1 || (echo This Python has no tkinter: %PY% & pause & exit /b 1)
"%PY%" -m pip install --quiet --upgrade pyinstaller ruamel.yaml || (echo pip failed & pause & exit /b 1)

pushd "%ROOT%"
"%PY%" tools\ci\release_info.py || (echo release_info failed & popd & pause & exit /b 1)
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name Fh_Report_Config ^
    --version-file "%ROOT%\build\version_info.txt" ^
    --paths "%HERE%." ^
    --add-data "%HERE%fh_report_options.yaml;." ^
    --add-data "%ROOT%\plugins\fh_report\version.py;plugins\fh_report" ^
    --add-data "%ROOT%\plugins\fh_report\schemas\fh_report_schema.yaml;plugins\fh_report\schemas" ^
    --distpath "%HERE%dist" --workpath "%ROOT%\build\work" --specpath "%ROOT%\build" ^
    "%HERE%fh_report_config.pyw"
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (echo The build failed. & pause & exit /b %RC%)

echo.
echo Done: %HERE%dist\Fh_Report_Config.exe
pause
