@echo off
title Linux.do Unread Helper
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%linuxdo_unread_helper.py"
) else (
  python "%ROOT%linuxdo_unread_helper.py"
)
pause
