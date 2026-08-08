@echo off
title Finance MCP Server - Automated 1-Click Installer
echo ===================================================================
echo    Finance MCP - Market Data, Macro Calendar ^& SEC Filings
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
