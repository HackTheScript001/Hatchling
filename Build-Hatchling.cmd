@echo off
rem Double-click to build Hatchling and its installer. Installs Python and Inno Setup if needed.
title Build Hatchling
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build-windows.ps1"
echo.
pause
