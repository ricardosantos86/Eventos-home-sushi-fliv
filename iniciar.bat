@echo off
title HOME SUSHI POS
cd /d "%~dp0"

echo.
echo  ==========================================
echo   HOME SUSHI POS - A iniciar...
echo  ==========================================
echo.

:: Verifica Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERRO: Python nao encontrado.
    echo.
    echo  Instala em: https://python.org/downloads
    echo  Marca a opcao "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
)

:: Instala dependencias
echo  A verificar dependencias...
pip install flask flask-cors pywin32 pillow -q --disable-pip-version-check
if errorlevel 1 (
    echo  ERRO ao instalar dependencias.
    pause
    exit /b 1
)
echo  Dependencias OK.
echo.

:: Inicia o servidor em janela minimizada
echo  A iniciar servidor...
start "HOME SUSHI Servidor" /min python server.py

:: Aguarda arranque
timeout /t 3 /nobreak >nul

:: Abre no Chrome (ou browser padrao se nao encontrar)
echo  A abrir POS no browser...
set "FILE=%~dp0home_sushi_pos.html"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "CHROME86=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" (
    start "" "%CHROME%" "%FILE%"
) else if exist "%CHROME86%" (
    start "" "%CHROME86%" "%FILE%"
) else (
    start "" "%FILE%"
)

echo.
echo  POS ativo em http://localhost:5000
echo  Nao feches esta janela.
echo.
pause
