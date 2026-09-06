@echo off
setlocal EnableExtensions
cd /d "%~dp0"
python scripts\run_qwen_mixed_inference.py
set "Q38_EXIT=%ERRORLEVEL%"
echo.
echo Qwen mixed inference finished with exit code %Q38_EXIT%.
pause >nul
exit /b %Q38_EXIT%
