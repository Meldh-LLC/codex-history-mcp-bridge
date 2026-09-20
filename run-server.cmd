@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo Missing virtual environment. Run setup.ps1 first. 1>&2
  exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%server.py"
