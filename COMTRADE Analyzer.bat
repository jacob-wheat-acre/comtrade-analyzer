@echo off
:: COMTRADE Analyzer Launcher
:: Double-click this file to open the COMTRADE Analyzer GUI.
:: Requires Python 3.10+.  On a managed PC, request it through your IT
:: software portal -- see GIT_GUIDE.md section 1.

cd /d "%~dp0"

:: Try pythonw first (no console window), fall back to python
where pythonw >nul 2>&1
if %errorlevel% == 0 (
    start "" pythonw "%~dp0app.py"
) else (
    where python >nul 2>&1
    if %errorlevel% == 0 (
        start "" python "%~dp0app.py"
    ) else (
        echo Python was not found on this PC.
        echo.
        echo Try running:  py --version
        echo If that works, Python is installed but not on the system PATH --
        echo ask IT to add it.
        echo.
        echo If Python is not installed, request it through your IT software
        echo portal.  See GIT_GUIDE.md section 1.
        pause
    )
)
