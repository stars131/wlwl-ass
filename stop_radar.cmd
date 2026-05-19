@echo off
REM Ecosystem Radar — stop the standalone scheduler.
REM
REM Thin shim around scripts/start_radar.py --stop.

setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "LAUNCHER=%ROOT%scripts\start_radar.py"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%LAUNCHER%" --stop
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [stop_radar] no python on PATH; remove temp\radar_runner.pid and kill via taskkill manually.
        exit /b 127
    )
    python "%LAUNCHER%" --stop
)
endlocal
