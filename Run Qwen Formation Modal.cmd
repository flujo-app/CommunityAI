@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "MODAL_PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined COMMUNITYAI_MODAL_PYTHON set "MODAL_PYTHON=%COMMUNITYAI_MODAL_PYTHON%"
set "PYTHONIOENCODING=utf-8"
if exist "%MODAL_PYTHON%" (
  "%MODAL_PYTHON%" scripts\run_qwen_formation_modal.py %*
  set "QWEN_EXIT=!ERRORLEVEL!"
) else (
  echo Modal Python was not found. Set COMMUNITYAI_MODAL_PYTHON to its Python executable.
  set "QWEN_EXIT=2"
)
echo.
echo Qwen Modal formation finished with exit code !QWEN_EXIT!.
echo This window may now be closed.
pause >nul
exit /b !QWEN_EXIT!
