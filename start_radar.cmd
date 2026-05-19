@echo off
REM Ecosystem Radar — one-click launcher (Windows .cmd shim).
REM
REM Delegates to scripts/start_radar.py which spawns the actual scheduler
REM as a detached Windows background process. Cross-shell safe — works
REM from a normal cmd window, a Git Bash session, PowerShell, etc.
REM
REM Idempotent: a second invocation while the runner is alive is a no-op.
REM Run stop_radar.cmd to terminate.

setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "LAUNCHER=%ROOT%scripts\start_radar.py"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%LAUNCHER%" %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [start_radar] no python found on PATH and no .venv at %VENV_PY%.
        echo                Run start_from_zero.cmd first or activate a python env.
        exit /b 127
    )
    python "%LAUNCHER%" %*
)
endlocal
