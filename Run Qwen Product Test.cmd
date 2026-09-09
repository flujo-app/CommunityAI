@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "QWEN_PYTHON=.gate13-runs\qwen-product-venv\Scripts\python.exe"
if defined COMMUNITYAI_TEST_PYTHON set "QWEN_PYTHON=%COMMUNITYAI_TEST_PYTHON%"
if not exist "%QWEN_PYTHON%" (
  echo Set COMMUNITYAI_TEST_PYTHON to the Python executable with desktop test dependencies installed.
  pause >nul
  exit /b 2
)
"%QWEN_PYTHON%" scripts\run_qwen_product_test.py %*
set "QWEN_EXIT=%ERRORLEVEL%"
echo.
echo Qwen product test finished with exit code %QWEN_EXIT%.
pause >nul
exit /b %QWEN_EXIT%
