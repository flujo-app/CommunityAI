@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "QWEN_PYTHON="
set "QWEN_PYTHON_ARGS="
if exist ".venv-cuda\Scripts\python.exe" set "QWEN_PYTHON=.venv-cuda\Scripts\python.exe"
if not defined QWEN_PYTHON if exist ".gate13-runs\qwen-product-venv\Scripts\python.exe" set "QWEN_PYTHON=.gate13-runs\qwen-product-venv\Scripts\python.exe"
if defined COMMUNITYAI_TEST_PYTHON set "QWEN_PYTHON=%COMMUNITYAI_TEST_PYTHON%"
if not defined QWEN_PYTHON where py.exe >nul 2>nul && set "QWEN_PYTHON=py.exe" && set "QWEN_PYTHON_ARGS=-3"
if not defined QWEN_PYTHON where python.exe >nul 2>nul && set "QWEN_PYTHON=python.exe"
if not defined QWEN_PYTHON (
  echo Python 3 was not found.
  set "QWEN_EXIT=2"
) else (
  call "%QWEN_PYTHON%" %QWEN_PYTHON_ARGS% scripts\run_qwen_qualification.py %*
  set "QWEN_EXIT=!ERRORLEVEL!"
)
echo.
echo Qwen qualification finished with exit code !QWEN_EXIT!.
echo This window may now be closed.
pause >nul
exit /b !QWEN_EXIT!
