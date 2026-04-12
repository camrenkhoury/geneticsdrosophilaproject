@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
call "%SCRIPT_DIR%..\host_app\launchers\windows\launch_gui.bat"
exit /b %errorlevel%
