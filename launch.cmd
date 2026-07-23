@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Editoro
cd /d "%~dp0"

chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%VENV_DIR%\playwright-browsers"

echo.
echo ============================================================
echo   EDITORO - Local video editor
echo ============================================================
echo.

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [ERROR] FFmpeg was not found on PATH.
  echo Install FFmpeg, reopen this window, and run launch.cmd again.
  goto :failed
)

where ffprobe >nul 2>nul
if errorlevel 1 (
  echo [ERROR] FFprobe was not found on PATH.
  echo Install the complete FFmpeg package and run launch.cmd again.
  goto :failed
)

if not exist "%VENV_PY%" (
  echo [SETUP] Creating the private Editoro Python environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.11 -m venv "%VENV_DIR%"
  ) else (
    where python >nul 2>nul
    if errorlevel 1 (
      echo [ERROR] Python 3.11 or newer was not found.
      echo Install Python from python.org and enable "Add Python to PATH".
      goto :failed
    )
    python -m venv "%VENV_DIR%"
  )
  if errorlevel 1 (
    echo [ERROR] The virtual environment could not be created.
    goto :failed
  )
)

for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath 'requirements.txt').Hash"`) do set "REQ_HASH=%%H"
set "OLD_HASH="
if exist "%VENV_DIR%\.requirements.sha256" set /p OLD_HASH=<"%VENV_DIR%\.requirements.sha256"

if not "!REQ_HASH!"=="!OLD_HASH!" (
  echo [SETUP] Installing Editoro dependencies...
  "%VENV_PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip wheel
  if errorlevel 1 goto :dependency_failed
  "%VENV_PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
  if errorlevel 1 goto :dependency_failed
  >"%VENV_DIR%\.requirements.sha256" echo !REQ_HASH!
) else (
  echo [OK] Python dependencies are ready.
)

if not exist "%VENV_DIR%\.playwright-ready" (
  echo [SETUP] Installing the private rendering browser...
  "%VENV_PY%" -m playwright install chromium
  if errorlevel 1 (
    echo [ERROR] Chromium could not be installed for the export renderer.
    goto :failed
  )
  >"%VENV_DIR%\.playwright-ready" echo ready
) else (
  echo [OK] Export renderer is ready.
)

echo [START] Opening Editoro...
"%VENV_PY%" server.py %*
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
  echo.
  echo [ERROR] Editoro stopped with exit code %APP_EXIT%.
  goto :failed
)

echo.
echo Editoro stopped normally.
pause
exit /b 0

:dependency_failed
echo.
echo [ERROR] A dependency could not be installed.
echo Check your internet connection, then run launch.cmd again.

:failed
echo.
echo The window will stay open so you can read the message above.
pause
exit /b 1
