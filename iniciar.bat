@echo off
title HOME SUSHI POS
cd /d "%~dp0"

echo.
echo  ==========================================
echo   HOME SUSHI POS - A iniciar...
echo  ==========================================
echo.

:: Grava PID desta janela CMD (para o botao SAIR fechar)
for /f %%i in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\").ParentProcessId"') do (
  echo %%i> "%~dp0.pos_launcher.pid"
)

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
pip install flask flask-cors pywin32 pillow pyopenssl -q --disable-pip-version-check
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
timeout /t 4 /nobreak >nul

:: Abre POS e Monitor em janelas app (mais faceis de fechar com SAIR)
echo  A abrir POS e Monitor no browser...
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "CHROME86=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE64=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "POS_URL=https://127.0.0.1:5000/"
set "MON_URL=https://127.0.0.1:5000/monitor?v=rd2"
:: Certificado local self-signed — so neste PC do evento
set "APP_FLAGS=--ignore-certificate-errors --new-window --start-maximized"

if exist "%CHROME%" (
    start "HOME SUSHI POS" /MAX "%CHROME%" %APP_FLAGS% --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%CHROME%" %APP_FLAGS% --app="%MON_URL%"
) else if exist "%CHROME86%" (
    start "HOME SUSHI POS" /MAX "%CHROME86%" %APP_FLAGS% --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%CHROME86%" %APP_FLAGS% --app="%MON_URL%"
) else if exist "%EDGE64%" (
    start "HOME SUSHI POS" /MAX "%EDGE64%" %APP_FLAGS% --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%EDGE64%" %APP_FLAGS% --app="%MON_URL%"
) else if exist "%EDGE%" (
    start "HOME SUSHI POS" /MAX "%EDGE%" %APP_FLAGS% --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%EDGE%" %APP_FLAGS% --app="%MON_URL%"
) else (
    start "HOME SUSHI POS" /MAX "%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%MON_URL%"
)

:: Garante maximizar as janelas app (Chrome/Edge por vezes ignoram /MAX com --app)
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command ^
  "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class W{[DllImport(\"user32.dll\")]public static extern bool ShowWindowAsync(IntPtr h,int c);}'; ^
  "Get-Process chrome,msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and ($_.MainWindowTitle -match 'Home Sushi|ROCK|Monitor') } | ForEach-Object { [void][W]::ShowWindowAsync($_.MainWindowHandle,3) }"

echo.
echo  POS:     https://127.0.0.1:5000
echo  Monitor: https://127.0.0.1:5000/monitor
echo.
echo  Noutro dispositivo na mesma rede Wi-Fi (PWA):
echo  abre https://IP-DESTE-PC:5000/        (caixa)
echo  abre https://IP-DESTE-PC:5000/monitor (monitor)
echo  Aceita o aviso do certificado uma vez,
echo  depois: Instalar app / Adicionar ao ecra inicial.
echo  (o IP aparece na janela do servidor)
echo.
echo  NAO abras os ficheiros .html diretamente.
echo  Usa sempre os links https:// acima.
echo.
echo  Para fechar tudo: botao SAIR no POS ou Monitor.
echo.
pause
