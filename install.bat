@echo off
title Replicant Quant MCP Server - Automated 1-Click Installer
echo ===================================================================
echo    Replicant Quantitative Market Intelligence & MCP Server
echo                   1-Click Automated Installer
echo ===================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer.ps1"

echo.
echo ===================================================================
echo Installation Process Complete!
echo Press any key to exit.
echo ===================================================================
pause > nul
