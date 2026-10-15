@echo off
setlocal

set "PROJECT=D:\All\Downloads\crypto"
set "LOGS=%PROJECT%\logs"

if not exist "%LOGS%" mkdir "%LOGS%"

set "STDOUT=%LOGS%\dashboard-out.log"
set "STDERR=%LOGS%\dashboard-err.log"

cd /d "%PROJECT%"

set "DASHBOARD_SCAN_SECONDS=60"
set "DASHBOARD_HOST=0.0.0.0"
set "DASHBOARD_PORT=8787"

echo [%date% %time%] Starting crypto dashboard...
echo [%date% %time%] Working directory: %CD%
echo [%date% %time%] Standard output: %STDOUT%
echo [%date% %time%] Error output: %STDERR%

uv run crypto-dashboard 1>>"%STDOUT%" 2>>"%STDERR%"

echo [%date% %time%] Dashboard exited with code %errorlevel%.
echo Review "%STDERR%" and "%STDOUT%" before restarting.
pause

endlocal
