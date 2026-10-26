@echo off
chcp 65001 >nul
rem Собирает папку dist\cs2-roulette с cs2-roulette.exe (трей, без консоли; Python на целевом ПК не нужен).
rem --onedir (не --onefile): самораспаковывающиеся exe чаще ловят ложные срабатывания антивируса.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && set "PY=py -3" || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
  echo [cs2-roulette] Создаю виртуальное окружение...
  %PY% -m venv .venv || (echo Python не найден. Установите Python 3.11-3.13 и отметьте "Add python.exe to PATH". & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || (echo Не удалось установить зависимости. & pause & exit /b 1)
".venv\Scripts\python.exe" -c "import hid" || (echo Модуль hidapi не установился: без него exe не сможет управлять мышью. Проверьте версию Python (нужна 3.11-3.13). & pause & exit /b 1)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --noconsole --name cs2-roulette ^
  --icon "app\static\icon.ico" ^
  --add-data "app\templates;app\templates" --add-data "app\static;app\static" --add-data "QUICKSTART.md;." ^
  --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all anyio ^
  --collect-all pystray --collect-submodules PIL --hidden-import hid ^
  --hidden-import app.main --hidden-import app.modules.dpi --hidden-import app.tray ^
  tools\entry.py || (echo Сборка не удалась, смотрите сообщения выше. & pause & exit /b 1)
if not exist "dist\cs2-roulette\_internal\hid*.pyd" echo ВНИМАНИЕ: в сборке нет модуля hid (hidapi): управление мышью в exe работать не будет.
echo.
echo Готово: dist\cs2-roulette\cs2-roulette.exe  (запускается в трей; папка data\ создастся рядом с exe)
echo Запуск с консолью для отладки: cs2-roulette.exe --console
pause
