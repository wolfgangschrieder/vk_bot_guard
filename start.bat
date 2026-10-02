@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
    echo Сначала скопируйте .env.example в .env и заполните VK_TOKEN и GROUP_ID.
    pause
    exit /b 1
)

echo Запуск VK Bot Guard. Закройте окно, чтобы остановить.
:loop
python bot.py
if errorlevel 1 echo Бот завершился с ошибкой.
echo Перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
