@echo off
setlocal
cd /d "%~dp0"

echo [1/2] Ensuring PyInstaller...
python -m pip install -q "pyinstaller>=6.0" pillow

echo [2/2] Building TTS.exe ...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name TTS ^
  --icon app.ico ^
  --add-data "app.ico;." ^
  --collect-all customtkinter ^
  --collect-all edge_tts ^
  --hidden-import edge_tts ^
  --hidden-import aiohttp ^
  --hidden-import certifi ^
  --hidden-import darkdetect ^
  --hidden-import PIL ^
  --hidden-import pydub ^
  main.py

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)

echo.
echo Done: dist\TTS.exe
dir dist\TTS.exe
endlocal
