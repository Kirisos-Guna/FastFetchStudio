@echo off
rem FastFetch Studio - build a standalone FastFetchStudio.exe
setlocal
cd /d "%~dp0"

python -m pip install --upgrade -r requirements-dev.txt || goto :err

python -m pytest tests/ -q || goto :err

python -m PyInstaller FastFetchStudio.spec || goto :err

echo.
echo Done: %~dp0dist\FastFetchStudio.exe
exit /b 0

:err
echo Build failed.
exit /b 1
