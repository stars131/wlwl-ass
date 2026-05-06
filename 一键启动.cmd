@echo off
setlocal

set "ROOT=%~dp0"
set "BOOTSTRAP=%ROOT%launcher\bootstrap_start.ps1"

if not exist "%BOOTSTRAP%" (
    echo Cannot find bootstrap script:
    echo   %BOOTSTRAP%
    pause
    exit /b 1
)

powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo wlwl-ass exited normally.
) else (
    echo wlwl-ass startup failed with exit code %RC%.
)
echo Check temp\logs\bootstrap-*.log for details.
pause
exit /b %RC%
