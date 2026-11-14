@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Как run.bat, но приложение пишет пакеты CS2 в data\gsi-record.jsonl (для проверки разработчиком).
rem Сыграйте один-два матча, закройте приложение и пришлите файл. Обычный запуск - run.bat.
set "CS2R_GSI_RECORD=1"
call run.bat
echo [hardmode] Запись пакетов CS2 включена: data\gsi-record.jsonl
timeout /t 3 >nul
