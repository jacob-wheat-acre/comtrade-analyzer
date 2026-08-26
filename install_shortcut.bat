@echo off
:: Creates a "COMTRADE Analyzer" shortcut on the current user's Desktop.
:: Run this once after copying the comtrade-analyzer folder to the machine.
:: The shortcut is built by install_shortcut.py, which finds the real Desktop
:: even when OneDrive has redirected it, and sets the application icon.

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% == 0 (
    python "%~dp0install_shortcut.py"
    goto done
)
where py >nul 2>&1
if %errorlevel% == 0 (
    py "%~dp0install_shortcut.py"
    goto done
)

echo Python was not found on this PC.
echo.
echo Try running:  py --version
echo If that works, Python is installed but not on the system PATH --
echo ask IT to add it.
echo.
echo If Python is not installed, request it through your IT software portal.
echo See GIT_GUIDE.md section 1.

:done
pause
