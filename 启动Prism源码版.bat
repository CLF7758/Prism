@echo off
setlocal
cd /d "%~dp0"
set "PYEXE=%~dp0.venv\Scripts\python.exe"
set "PYEXEW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYEXE%" goto check_dependencies

echo Creating the Prism Python 3.12 environment...
py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 goto setup_failed

:check_dependencies
"%PYEXE%" -c "import importlib.util,sys; modules=('PyQt6','PyQt6.QtWebEngineWidgets','av','exif','lxml','numpy','onnxruntime','PIL','docx','rpack','qtpy','disk_objectstore'); sys.exit(sys.version_info[:2] != (3,12) or any(importlib.util.find_spec(name) is None for name in modules))"
if not errorlevel 1 goto launch

echo Installing Prism dependencies. The first launch requires internet access.
"%PYEXE%" -m pip install -e .
if errorlevel 1 goto setup_failed

:launch
echo Starting Prism from "%CD%"...
rem pythonw.exe is a GUI program: it does not inherit this console, so closing
rem this black window can no longer kill Prism.  A console python.exe received
rem the very same CTRL_CLOSE_EVENT and died together with the window -- that
rem is exactly what the user saw: the whole interface vanished and came back
rem on the next launch, without a crash report, a traceback or an exit log.
if exist "%PYEXEW%" (
    start "Prism" "%PYEXEW%" -m prism %*
) else (
    rem No pythonw here: give the application its own console instead of this
    rem one, so that closing this window still cannot kill it.
    start "Prism" "%PYEXE%" -m prism %*
)
echo.
echo Prism runs in its own process now.  This window can be closed safely.
echo The log is at %APPDATA%\Prism\Prism.log
pause
exit /b 0

:setup_failed
echo.
echo Setup failed. Install Python 3.12 and check your internet connection.
echo The error above explains what step failed.
pause
exit /b 1
