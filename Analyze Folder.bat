@echo off
:: Bulk-analyze a folder of COMTRADE events.
:: Drag an events folder onto this file, or double-click and type the path.
:: Writes a dashboard, CSV and JSON into <folder>\analysis.

setlocal
cd /d "%~dp0"

set "TARGET=%~1"
if "%TARGET%"=="" (
    set /p TARGET=Path to the folder of COMTRADE events: 
)
if "%TARGET%"=="" (
    echo No folder given.
    pause
    exit /b 1
)

set "REGISTRY="
if exist "%~dp0devices.csv" set "REGISTRY=--devices "%~dp0devices.csv""

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python was not found on this PC.  See GIT_GUIDE.md section 1.
    pause
    exit /b 1
)

python -m comtrade_analyzer.batch "%TARGET%" %REGISTRY%
echo.
echo Done.  The dashboard is at:  %TARGET%\analysis\fleet_dashboard.html
pause
