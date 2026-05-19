@echo off
REM wlwl-ass terminal launcher shim.
REM
REM Lets the user type "wlwl" in any terminal (cmd, PowerShell, Windows
REM Terminal) and drop into the interactive REPL, the same way "claude" and
REM "codex" work -- without needing to activate the .venv first.
REM
REM Add this directory to PATH so "wlwl" is reachable from anywhere:
REM   setx PATH "%PATH%;%~dp0"   (current user; new shells only)
REM
REM Or just run start_from_zero.cmd once to bootstrap .venv, then call
REM .venv\Scripts\wlwl.exe directly (created by "pip install -e ." via the
REM [project.scripts] entry in pyproject.toml).

setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -m launcher.cli_repl %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    pushd "%ROOT%" >nul
    python -m launcher.cli_repl %*
    set "RC=%ERRORLEVEL%"
    popd >nul
    exit /b %RC%
)

echo [wlwl] Could not find a Python interpreter.
echo        Run start_from_zero.cmd once to create the .venv, then retry.
exit /b 127
