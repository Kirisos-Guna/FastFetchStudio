@echo off
rem FastFetch Studio - build a standalone FastFetchStudio.exe
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pillow pyinstaller || goto :err

python -m PyInstaller --onefile --noconsole --name FastFetchStudio ^
  --hidden-import tkinter --hidden-import PIL --hidden-import PIL.ImageTk ^
  --collect-submodules PIL app.py || goto :err

echo.
echo Done: %~dp0dist\FastFetchStudio.exe
exit /b 0

:err
echo Build failed.
exit /b 1
