@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%\..\..\.."

set "PYTHONW_EXE="
set "PYTHON_EXE="

for /f "usebackq delims=" %%I in (`where pythonw 2^>nul`) do (
    echo %%~fI | find /i "WindowsApps" >nul
    if errorlevel 1 if not defined PYTHONW_EXE set "PYTHONW_EXE=%%~fI"
)

for /f "usebackq delims=" %%I in (`where python 2^>nul`) do (
    echo %%~fI | find /i "WindowsApps" >nul
    if errorlevel 1 if not defined PYTHON_EXE set "PYTHON_EXE=%%~fI"
)

if defined PYTHONW_EXE (
    start "" "%PYTHONW_EXE%" "CodeDirectory\gui.py"
    exit /b 0
)

if defined PYTHON_EXE (
    "%PYTHON_EXE%" "CodeDirectory\gui.py"
    if errorlevel 1 (
        echo.
        echo GUI launch failed.
        pause
    )
    exit /b %errorlevel%
)

echo Could not find a usable Python installation.
echo Install Python 3 for Windows, or fix PATH, then try again.
pause
exit /b 1
