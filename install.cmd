@echo off
setlocal EnableDelayedExpansion

:: Get the directory where this script is located (works from anywhere)
set "SCRIPT_DIR=%~dp0"

:: ── Version being installed (read from commands.py, never duplicated here) ───
:: Line format there:  FH_REPORT_RELEASE = "x.y.z"   (%%~V strips the quotes)
set "NEW_VER=unknown"
for /f "tokens=3" %%V in ('findstr /B /C:"FH_REPORT_RELEASE" "%SCRIPT_DIR%plugins\fh_report\commands.py" 2^>nul') do set "NEW_VER=%%~V"

:: ── Colors (Windows 10/11 only; empty on older systems so no stray characters)
:: NEWC new version, OLDC previous version, OKC/ERRC/ACTC status words, RSTC restart notice
:: (all bright, readable on black)
set "NEWC="
set "OLDC="
set "OKC="
set "ERRC="
set "ACTC="
set "RSTC="
set "OFF="
ver | findstr /C:" 10." > nul && (
    for /f %%E in ('echo prompt $E ^| cmd') do set "ESC=%%E"
    set "NEWC=!ESC![1;92m"
    set "OLDC=!ESC![31m"
    set "OKC=!ESC![1;94m"
    set "ERRC=!ESC![1;91m"
    set "ACTC=!ESC![1;96m"
    set "RSTC=!ESC![1;93m"
    set "OFF=!ESC![0m"
)

echo.
echo ============================================================
echo  Fh_Report Plugin - Installer / Updater --^> !NEWC!Ver. !NEW_VER!!OFF!
echo ============================================================
echo.

:: ── Detect DCSServerBot installation ─────────────────────────────────────────
set "DCSSB_PATH="
set "VERSIONS_SHOWN="

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
    call :show_versions
    set /p CONFIRM="Is this correct? (Y/N): "
    if /i "!CONFIRM!"=="N" (set "DCSSB_PATH=") else set "VERSIONS_SHOWN=1"
)

if "!DCSSB_PATH!"=="" (
    set /p DCSSB_PATH="Enter the full path to your DCSServerBot installation: "
)

if not exist "!DCSSB_PATH!\config\main.yaml" (
    echo.
    echo !ERRC!ERROR!OFF!: DCSServerBot not found at: !DCSSB_PATH!
    echo        Could not find config\main.yaml
    pause
    exit /b 1
)

:: A path typed by hand: show installed/new versions now (a detected path that
:: was confirmed already showed them before the question).
if not defined VERSIONS_SHOWN (
    echo.
    call :show_versions
)

:: ── Copy plugin files ─────────────────────────────────────────────────────────
echo [1/3] Copying plugin files...
if not exist "!DCSSB_PATH!\plugins\fh_report" mkdir "!DCSSB_PATH!\plugins\fh_report"

copy /Y "%SCRIPT_DIR%plugins\fh_report\commands.py"   "!DCSSB_PATH!\plugins\fh_report\commands.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\__init__.py"   "!DCSSB_PATH!\plugins\fh_report\__init__.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\listener.py"   "!DCSSB_PATH!\plugins\fh_report\listener.py"   > nul
copy /Y "%SCRIPT_DIR%plugins\fh_report\version.py"    "!DCSSB_PATH!\plugins\fh_report\version.py"    > nul
echo       !OKC!OK!OFF! - Plugin files copied.

:: ── Handle config file ────────────────────────────────────────────────────────
echo [2/3] Checking configuration file...
if not exist "!DCSSB_PATH!\config\plugins\fh_report.yaml" (
    :: First install — copy fresh config
    if not exist "!DCSSB_PATH!\config\plugins" mkdir "!DCSSB_PATH!\config\plugins"
    copy /Y "%SCRIPT_DIR%config\plugins\fh_report.yaml" "!DCSSB_PATH!\config\plugins\fh_report.yaml" > nul
    echo       !OKC!OK!OFF! - fh_report.yaml created. Edit it to configure your servers and channels.
) else (
    :: Existing config found — run migration to add any new variables
    echo       Existing fh_report.yaml found. Running migration...
    set "PYTHON_EXE="

    :: Try DCSServerBot virtual environment first
    if exist "%USERPROFILE%\.dcssb\Scripts\python.exe" (
        set "PYTHON_EXE=%USERPROFILE%\.dcssb\Scripts\python.exe"
    )

    :: Fallback to system Python
    if "!PYTHON_EXE!"=="" (
        where python >nul 2>&1
        if !ERRORLEVEL! == 0 set "PYTHON_EXE=python"
    )

    if "!PYTHON_EXE!"=="" (
        echo       !ERRC!WARNING!OFF! - Python not found. Could not run migration.
        echo       Your existing config has been preserved unchanged.
        echo       Please manually check for new variables in the sample config.
    ) else (
        "!PYTHON_EXE!" "%SCRIPT_DIR%migrate_config.py" "!DCSSB_PATH!\config\plugins\fh_report.yaml"
        if !ERRORLEVEL! == 0 (
            echo       !OKC!OK!OFF! - Configuration migrated successfully.
        ) else (
            echo       !ERRC!WARNING!OFF! - Migration script encountered an error.
            echo       Your existing config has been preserved unchanged.
        )
    )
)

:: ── Check main.yaml for fh_report entry ──────────────────────────────────────
echo [3/3] Checking main.yaml...
findstr /C:"- fh_report" "!DCSSB_PATH!\config\main.yaml" > nul 2>&1
if !ERRORLEVEL! == 0 (
    echo       !OKC!OK!OFF! - fh_report already listed in main.yaml.
) else (
    echo       !ACTC!ACTION REQUIRED!OFF! - Add the following to your config\main.yaml:
    echo.
    echo           opt_plugins:
    echo             - fh_report
    echo.
)

:: ── Done ─────────────────────────────────────────────────────────────────────
echo.
echo ============================================================
:: A lone "!" can't be echoed with delayed expansion on, so this line uses %var%
:: expansion (the colors and NEW_VER are already set) with it switched off.
setlocal DisableDelayedExpansion
echo  %NEWC%Installation complete!%OFF% Fh_Report %NEWC%Ver. %NEW_VER%%OFF%
endlocal
echo ============================================================
echo.
echo !RSTC!RESTART REQUIRED!OFF! - Restart DCSServerBot to load !NEWC!Ver. !NEW_VER!!OFF! ^(the running bot keeps the old code until then^)
echo.
echo Next steps:
echo   1. Make sure 'fh_report' is listed under opt_plugins in config\main.yaml
echo   2. Edit config\plugins\fh_report.yaml to configure your servers and channels
echo   3. Restart DCSServerBot
echo.
pause
exit /b 0

:: ── Show the version installed in !DCSSB_PATH! and the one about to be installed
:show_versions
set "OLD_VER="
set "OLD_FILE=!DCSSB_PATH!\plugins\fh_report\commands.py"
if exist "!OLD_FILE!" set "OLD_VER=unknown (no version in the installed file)"
if exist "!OLD_FILE!" for /f "tokens=3" %%V in ('findstr /B /C:"FH_REPORT_RELEASE" "!OLD_FILE!" 2^>nul') do set "OLD_VER=%%~V"

echo Installing Fh_Report !NEWC!Ver. !NEW_VER!!OFF! to: !DCSSB_PATH!
if "!OLD_VER!"=="" (
    echo Installed now: none ^(new install^)
) else if "!OLD_VER!"=="!NEW_VER!" (
    echo Installed now: !NEWC!Ver. !OLD_VER!!OFF! ^(same version - files will be refreshed^)
) else (
    echo Installed now: !OLDC!Ver. !OLD_VER!!OFF! --^> updating to !NEWC!Ver. !NEW_VER!!OFF!
)
echo.
exit /b 0
