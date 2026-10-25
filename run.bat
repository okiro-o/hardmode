@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul && set "PY=py -3" || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
  echo [cs2-roulette] Создаю виртуальное окружение...
  %PY% -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
echo [cs2-roulette] Запуск. Панель откроется в браузере. Закройте это окно, чтобы остановить.
".venv\Scripts\python.exe" -m app %*
if errorlevel 1 (
  echo.
  echo [cs2-roulette] Утилита завершилась с ошибкой, код %errorlevel%. Смотрите сообщение выше.
  pause
)
goto :eof
:nopython
echo.
echo Python не найден. Установите Python 3.11+ с https://www.python.org/downloads/windows/
echo (при установке отметьте "Add python.exe to PATH"), затем запустите run.bat снова.
pause
goto :eof
:fail
echo Не удалось установить зависимости. Проверьте интернет и запустите run.bat снова.
pause
