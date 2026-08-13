@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Калькулятор растаможки

where python >nul 2>&1
if errorlevel 1 (
    echo Python не найден. Поставь с python.org и отметь "Add to PATH".
    pause
    exit /b 1
)

python -c "import playwright, requests" >nul 2>&1
if errorlevel 1 (
    echo Первый запуск — ставлю зависимости, это займёт пару минут...
    python -m pip install --quiet playwright requests || goto :fail
    python -m playwright install chromium || goto :fail
)

start "" http://localhost:8731
python serve.py
exit /b 0

:fail
echo.
echo Не удалось поставить зависимости. Запусти вручную:
echo   python -m pip install playwright requests
echo   python -m playwright install chromium
pause
exit /b 1
