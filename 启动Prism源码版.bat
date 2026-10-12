@echo off
setlocal
rem 启动「整改副本」Prism-work 的源码（五项整改都在这个副本里）。
rem 环境复用 Prism-main 的 .venv；-m prism 以当前目录为 sys.path[0]，
rem 所以导入的是 Prism-work 的 prism 包，不会碰到原始项目。
cd /d "%~dp0"
set "PYEXE=%~dp0..\Prism-main\.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    echo Prism Python environment was not found at %PYEXE%.
    pause
    exit /b 1
)
echo Starting Prism from the REWORKED copy (Prism-work)...
"%PYEXE%" -m prism %*
set "PRISM_EXIT_CODE=%ERRORLEVEL%"
if not "%PRISM_EXIT_CODE%"=="0" (
    echo.
    echo Prism stopped with error code %PRISM_EXIT_CODE%.
    pause
)
exit /b %PRISM_EXIT_CODE%
