@echo off
setlocal EnableExtensions
cd /d "%~dp0"
python scripts\run_qwen_full_inference_gcp.py
set "Q38_EXIT=%ERRORLEVEL%"
echo.
echo Qwen full inference finished with exit code %Q38_EXIT%.
pause >nul
exit /b %Q38_EXIT%
