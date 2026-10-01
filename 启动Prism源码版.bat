@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Prism Python environment was not found in .venv.
    echo Please restore the project environment before starting Prism.
    pause
    exit /b 1
)
echo Starting Prism from the latest source code...
"%~dp0.venv\Scripts\python.exe" -m prism %*
set "PRISM_EXIT_CODE=%ERRORLEVEL%"
if not "%PRISM_EXIT_CODE%"=="0" (
    echo.
    echo Prism stopped with error code %PRISM_EXIT_CODE%.
    echo Keep this window open to inspect the error above.
    pause
)
exit /b %PRISM_EXIT_CODE%
