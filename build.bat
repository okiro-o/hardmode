@echo off
chcp 65001 >nul
rem Собирает папку dist\cs2-roulette с exe (Python на целевом ПК не нужен).
rem --onedir (не --onefile): самораспаковывающиеся exe чаще ловят ложные срабатывания антивируса.
setlocal
cd /d "%~dp0"
where py >nul 2>nul && set "PY=py -3" || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
  echo [cs2-roulette] Создаю виртуальное окружение...
  %PY% -m venv .venv || (echo Python не найден. Установите Python 3.11+ и отметьте "Add python.exe to PATH". & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || (echo Не удалось установить зависимости. & pause & exit /b 1)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --name cs2-roulette ^
  --add-data "app\templates;app\templates" --add-data "app\static;app\static" ^
  --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all anyio ^
  --hidden-import app.main --hidden-import app.modules.dpi ^
  tools\entry.py || (echo Сборка не удалась, смотрите сообщения выше. & pause & exit /b 1)
echo.
echo Готово: dist\cs2-roulette\cs2-roulette.exe  (папка data\ создастся рядом с exe)
pause
