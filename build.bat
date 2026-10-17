@echo off
rem Builds dist\LamzuBattery.exe. Needs: pip install hidapi pystray Pillow pyinstaller
cd /d "%~dp0"
python make_icon.py || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name LamzuBattery --icon app.ico lamzu_tray.pyw || exit /b 1
echo.
echo Built dist\LamzuBattery.exe
