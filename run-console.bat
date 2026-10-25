@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Режим с консолью: лог на экране (для отладки). Обычный запуск - run.bat (трей).
if not exist ".venv\Scripts\python.exe" call run.bat
".venv\Scripts\python.exe" -m app --console %*
if errorlevel 1 (
  echo.
  echo [cs2-roulette] Утилита завершилась с ошибкой, код %errorlevel%. Смотрите сообщение выше и data\app.log.
  pause
)
