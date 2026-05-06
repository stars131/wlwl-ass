@echo off
setlocal

REM DEPRECATED: 此脚本写死了 %LocalAppData%\Programs\Python\Python312\python.exe，
REM 在其他 Python 版本/路径下会失败。推荐改用：
REM   python launch.pyw
REM 本文件仅保留向后兼容。

set "ROOT=%~dp0"
set "PYTHON312=%LocalAppData%\Programs\Python\Python312\python.exe"

if not exist "%PYTHON312%" (
    echo Python 3.12 not found at:
    echo   %PYTHON312%
    echo Install Python 3.12 first, then rerun this script.
    exit /b 1
)

if defined PYTHONPATH (
    set "PYTHONPATH=%ROOT%.deps;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%ROOT%.deps"
)

"%PYTHON312%" "%ROOT%launch.pyw" %*

