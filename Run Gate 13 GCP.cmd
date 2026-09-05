@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "GATE13_PYTHON="
if exist ".venv-cuda\Scripts\python.exe" set "GATE13_PYTHON=.venv-cuda\Scripts\python.exe"
if not defined GATE13_PYTHON where py.exe >nul 2>nul && set "GATE13_PYTHON=py.exe -3"
if not defined GATE13_PYTHON where python.exe >nul 2>nul && set "GATE13_PYTHON=python.exe"
if not defined GATE13_PYTHON (
  echo Python 3 was not found.
  set "GATE13_EXIT=2"
) else (
  call %GATE13_PYTHON% scripts\run_gate13_gcp.py
  set "GATE13_EXIT=!ERRORLEVEL!"
)
echo.
echo Gate 13 finished with exit code !GATE13_EXIT!.
echo This window may now be closed.
pause >nul
exit /b !GATE13_EXIT!
