@echo off
setlocal
cd /d "%~dp0"

rem --- venv fuera de OneDrive ---
set "WORK=%LOCALAPPDATA%\whisper-transcription"
set "VENV=%WORK%\.venv"

rem --- Localizar Python 3.14/3.13/3.12 (3.15 aun no tiene todas las librerias) ---
set "PY="
for %%V in (3.14 3.13 3.12) do if not defined PY py -V:%%V --version >nul 2>nul && set "PY=py -V:%%V"
if not defined PY (
  echo [ERROR] No se encuentra Python 3.14. Ejecuta en PowerShell:  py install 3.14
  pause
  exit /b 1
)

rem --- Entorno virtual y dependencias (la primera vez tarda unos minutos) ---
if not exist "%VENV%\Scripts\python.exe" (
  echo Creando entorno virtual en %VENV%
  %PY% -m venv "%VENV%"
  if errorlevel 1 ( pause & exit /b 1 )
)
set "VPY=%VENV%\Scripts\python.exe"
echo Comprobando dependencias...
"%VPY%" -m pip install --upgrade pip -q --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] Fallo en pip install. & pause & exit /b 1 )

"%VPY%" -c "import tkinter" 2>nul
if errorlevel 1 (
  echo [ERROR] Esta instalacion de Python no incluye tkinter, necesario para la interfaz.
  pause
  exit /b 1
)

rem --- Abrir la interfaz sin consola ---
echo Abriendo la interfaz...
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0
