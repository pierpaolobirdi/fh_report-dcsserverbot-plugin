@echo off
setlocal EnableDelayedExpansion

:: Get the directory where this script is located (works from anywhere)
set "SCRIPT_DIR=%~dp0"

echo.
echo ================================================
echo  FH_Report Plugin Installer for DCSServerBot
echo ================================================
echo.

:: ── Detect DCSServerBot installation ─────────────────────────────────────────
set "DCSSB_PATH="

for %%P in (
    "C:\DCSServerBot"
    "D:\DCSServerBot"
    "E:\DCSServerBot"
    "L:\DCSServerBot"
    "%USERPROFILE%\DCSServerBot"
    "%USERPROFILE%\Documents\DCSServerBot"
) do (
    if exist "%%~P\config\main.yaml" (
        if "!DCSSB_PATH!"=="" set "DCSSB_PATH=%%~P"
    )
)

if not "!DCSSB_PATH!"=="" (
    echo Detected DCSServerBot at: !DCSSB_PATH!
    set /p CONFIRM="Is this correct? (Y/N): "
    if /i "!CONFIRM!"=="N" set "DCSSB_PATH="
)

if "!DCSSB_PATH!"=="" (
    set /p DCSSB_PATH="Enter the full path to your DCSServerBot installation: "
)

if not exist "!DCSSB_PATH!\config\main.yaml" (
    echo.
    echo ERROR: DCSServerBot not found at: !DCSSB_PATH!
    echo        Could not find config\main.yaml
    pause
    exit /b 1
)

echo.
echo Installing FH_Report to: !DCSSB_PATH!
echo.

:: ── Copy plugin files ─────────────────────────────────────────────────────────
echo [1/3] Copying plugin files...
if not exist "!DCSSB_PATH!\plugins\fh_report" mkdir "!DCSSB_PATH!\plugins\fh_report"

copy /Y "%SCRIPT_DIR%plugins\fh_report\commands.py"   "!DCSSB_PATH!\plugins\fh_report\commands.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\__init__.py"   "!DCSSB_PATH!\plugins\fh_report\__init__.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\listener.py"   "!DCSSB_PATH!\plugins\fh_report\listener.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\version.py"    "!DCSSB_PATH!\plugins\fh_report\version.py"    > nul
echo       OK - Plugin files copied.

:: ── Copy config file (only if it doesn't exist) ───────────────────────────────
echo [2/3] Copying configuration file...
if not exist "!DCSSB_PATH!\config\plugins\fh_report.yaml" (
    if not exist "!DCSSB_PATH!\config\plugins" mkdir "!DCSSB_PATH!\config\plugins"
    copy /Y "%SCRIPT_DIR%config\plugins\fh_report.yaml" "!DCSSB_PATH!\config\plugins\fh_report.yaml" > nul
    echo       OK - fh_report.yaml created. Edit it to configure your servers and channels.
) else (
    echo       SKIPPED - fh_report.yaml already exists, not overwritten.
    echo       Your existing configuration has been preserved.
)

:: ── Check main.yaml for fh_report entry ──────────────────────────────────────
echo [3/3] Checking main.yaml...
findstr /C:"- fh_report" "!DCSSB_PATH!\config\main.yaml" > nul 2>&1
if !ERRORLEVEL! == 0 (
    echo       OK - fh_report already listed in main.yaml.
) else (
    echo       ACTION REQUIRED - Add the following to your config\main.yaml:
    echo.
    echo           opt_plugins:
    echo             - fh_report
    echo.
)

:: ── Done ─────────────────────────────────────────────────────────────────────
echo.
echo ================================================
echo  Installation complete!
echo ================================================
echo.
echo Next steps:
echo   1. Make sure 'fh_report' is listed under opt_plugins in config\main.yaml
echo   2. Edit config\plugins\fh_report.yaml to configure your servers and channels
echo   3. Restart DCSServerBot
echo.
pause
