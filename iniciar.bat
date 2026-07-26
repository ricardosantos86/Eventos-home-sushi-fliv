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

:: Abre POS e Monitor em janelas app (mais faceis de fechar com SAIR)
echo  A abrir POS e Monitor no browser...
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "CHROME86=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE64=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "POS_URL=http://127.0.0.1:5000/"
set "MON_URL=http://127.0.0.1:5000/monitor"

if exist "%CHROME%" (
    start "HOME SUSHI POS" /MAX "%CHROME%" --new-window --start-maximized --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%CHROME%" --new-window --start-maximized --app="%MON_URL%"
) else if exist "%CHROME86%" (
    start "HOME SUSHI POS" /MAX "%CHROME86%" --new-window --start-maximized --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%CHROME86%" --new-window --start-maximized --app="%MON_URL%"
) else if exist "%EDGE64%" (
    start "HOME SUSHI POS" /MAX "%EDGE64%" --new-window --start-maximized --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%EDGE64%" --new-window --start-maximized --app="%MON_URL%"
) else if exist "%EDGE%" (
    start "HOME SUSHI POS" /MAX "%EDGE%" --new-window --start-maximized --app="%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%EDGE%" --new-window --start-maximized --app="%MON_URL%"
) else (
    start "HOME SUSHI POS" /MAX "%POS_URL%"
    start "HOME SUSHI Monitor" /MAX "%MON_URL%"
)

:: Garante maximizar as janelas app (Chrome/Edge por vezes ignoram /MAX com --app)
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command ^
  "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class W{[DllImport(\"user32.dll\")]public static extern bool ShowWindowAsync(IntPtr h,int c);}'; ^
  "Get-Process chrome,msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and ($_.MainWindowTitle -match 'Home Sushi|NEXO|Monitor') } | ForEach-Object { [void][W]::ShowWindowAsync($_.MainWindowHandle,3) }"

echo.
echo  POS:     http://127.0.0.1:5000
echo  Monitor: http://127.0.0.1:5000/monitor
echo.
echo  Noutro dispositivo na mesma rede Wi-Fi:
echo  abre http://IP-DESTE-PC:5000/monitor
echo  (o IP aparece na janela do servidor)
echo.
echo  NAO abras os ficheiros .html diretamente.
echo  Usa sempre os links http:// acima.
echo.
echo  Para fechar tudo: botao SAIR no POS ou Monitor.
echo.
pause
