@echo off
setlocal
cd /d "%~dp0"
set "PYEXE=%~dp0.venv\Scripts\python.exe"
if exist "%PYEXE%" goto check_dependencies

echo Creating the Prism Python 3.12 environment...
py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed

:check_dependencies
"%PYEXE%" -c "import importlib.util,sys; modules=('PyQt6','PyQt6.QtWebEngineWidgets','av','exif','lxml','numpy','onnxruntime','PIL','docx','rpack'); sys.exit(sys.version_info[:2] != (3,12) or any(importlib.util.find_spec(name) is None for name in modules))"
if not errorlevel 1 goto launch

echo Installing Prism dependencies. The first launch requires internet access.
"%PYEXE%" -m pip install -e .
if errorlevel 1 goto setup_failed

:launch
echo Starting Prism from "%CD%"...
"%PYEXE%" -m prism %*
set "PRISM_EXIT_CODE=%ERRORLEVEL%"
if not "%PRISM_EXIT_CODE%"=="0" (
    echo.
    echo Prism stopped with error code %PRISM_EXIT_CODE%.
    pause
)
exit /b %PRISM_EXIT_CODE%

:setup_failed
echo.
echo Setup failed. Install Python 3.12 and check your internet connection.
echo The error above explains which step failed.
pause
exit /b 1
