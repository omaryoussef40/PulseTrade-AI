@echo off
cd /d "%~dp0"
echo Installing AutoTrader Engine auto-start task...
echo.
schtasks /Create /TN "AutoTrader Engine" /TR "\"%~dp0run_engine.bat\"" /SC ONLOGON /RL HIGHEST /F
if %ERRORLEVEL% EQU 0 (
    echo.
    echo AutoTrader Engine auto-start installed successfully.
    echo It will start after Windows login.
) else (
    echo.
    echo Failed to install auto-start task. Right-click this file and choose Run as administrator.
)
echo.
pause
