@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul && set "PY=py -3" || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
  echo [hardmode] Создаю виртуальное окружение...
  %PY% -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
)
rem После git pull зависимости могли измениться: ставим, если requirements.txt отличается от установленного.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1 || (
  echo [hardmode] Обновляю зависимости...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
echo [hardmode] Запускаю в трее. Иконка появится рядом с часами; это окно можно закрыть.
start "" ".venv\Scripts\pythonw.exe" "%~dp0start_tray.pyw"
timeout /t 2 >nul
goto :eof
:nopython
echo.
echo Python не найден. Установите Python 3.11-3.13 с https://www.python.org/downloads/windows/
echo (при установке отметьте "Add python.exe to PATH"), затем запустите run.bat снова.
pause
goto :eof
:fail
echo Не удалось установить зависимости. Проверьте интернет и запустите run.bat снова.
pause
