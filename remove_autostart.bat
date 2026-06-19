@echo off
echo Removing AutoTrader Engine auto-start task...
echo.
schtasks /Delete /TN "AutoTrader Engine" /F
if %ERRORLEVEL% EQU 0 (
    echo.
    echo AutoTrader Engine auto-start removed successfully.
) else (
    echo.
    echo Task was not found or could not be removed.
)
echo.
pause
