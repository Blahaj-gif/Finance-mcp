@echo off
setlocal
title Finance MCP - Installer

REM This wrapper exists because Windows will not run a .ps1 on double-click:
REM the default execution policy blocks it, and there is no way to attach
REM "Bypass" to a file association. So install.bat is the thing a person can
REM actually click, and installer.ps1 is the installer. Merging them would mean
REM writing the installer in batch, which is a downgrade, not a simplification.
REM
REM Run either one -- installer.ps1 works directly from a PowerShell prompt.

echo ===================================================================
echo    Finance MCP - Market Data, Macro Calendar ^& SEC Filings
echo ===================================================================
echo.

set "PS1=%~dp0installer.ps1"

if not exist "%PS1%" (
    echo   ERROR: installer.ps1 was not found next to this file.
    echo.
    echo   Both files must stay together. If you downloaded install.bat on its
    echo   own, re-download the whole repository or release archive.
    echo.
    goto :failed
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"

REM Propagate the real result. This used to print "Installation Process
REM Complete!" unconditionally, so an installer that threw still ended on a
REM success banner -- the one line a person actually reads.
if errorlevel 1 goto :failed

echo.
echo ===================================================================
echo  Installation complete.
echo ===================================================================
echo.
echo  Next: open the "Finance MCP Dashboard" shortcut on your Desktop and
echo  work through the one-time briefing it shows you.
echo.
pause
endlocal
exit /b 0

:failed
echo.
echo ===================================================================
echo  INSTALLATION FAILED - nothing above should be treated as done.
echo ===================================================================
echo.
echo  Scroll up for the error. Common causes:
echo    * No internet connection while installing uv or dependencies
echo    * PowerShell blocked by group policy
echo.
pause
endlocal
exit /b 1
