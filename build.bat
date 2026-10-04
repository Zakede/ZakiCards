@echo off
rem Rebuilds dist\ZakiCards\ZakiCards.exe and the release zip.
cd /d "%~dp0"
copy /y zakicards.pyw zakicards_main.py >nul
python -m PyInstaller --noconfirm --clean --windowed --name ZakiCards --icon assets\icon.ico ^
  --add-data "ui.html;." --add-data "assets;assets" ^
  --exclude-module anki --exclude-module aqt --exclude-module PyQt6 --exclude-module PyQt5 ^
  --exclude-module torch --exclude-module matplotlib --exclude-module numpy --exclude-module pandas ^
  --distpath dist --workpath build zakicards_main.py
del zakicards_main.py ZakiCards.spec
rmdir /s /q build
if not exist release mkdir release
powershell -NoProfile -Command "Compress-Archive -Force dist\ZakiCards release\ZakiCards-win64.zip"
echo Done: dist\ZakiCards\ZakiCards.exe and release\ZakiCards-win64.zip
