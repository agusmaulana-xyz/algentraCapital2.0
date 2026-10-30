@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto :directory_error

set "DISABLE_SQLALCHEMY_CEXT_RUNTIME=1"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "PYTHON_ARGS="
if not exist "%PYTHON_EXE%" (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
  ) else (
    where python >nul 2>&1
    if errorlevel 1 goto :python_error
    set "PYTHON_EXE=python"
  )
)

if not exist ".env" (
  if not defined APP_SECRET_KEY goto :config_error
  if not defined ADMIN_PASSWORD goto :config_error
)

"%PYTHON_EXE%" %PYTHON_ARGS% --version
if errorlevel 1 goto :python_error
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import MetaTrader5, fastapi, neonize, segno, uvicorn" >nul 2>&1
if errorlevel 1 goto :dependency_error

set "ALGENTRA_PORT=8000"
netstat -ano -p tcp | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 (
  echo ERROR: Port 8000 is already in use. Stop the other process using it first.
  goto :port_error
)

echo Starting Algentra Capital and its worker processes...
echo WhatsApp and copy trading workers open in separate terminals. Press Ctrl+C here to stop all.
"%PYTHON_EXE%" %PYTHON_ARGS% run-all.py --reload
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
  echo.
  echo Backend stopped with exit code %APP_EXIT_CODE%. Read the error above.
  pause
)
endlocal & exit /b %APP_EXIT_CODE%

:config_error
echo ERROR: .env is missing, and APP_SECRET_KEY / ADMIN_PASSWORD are not set.
echo Copy .env.example to .env, then set a random APP_SECRET_KEY (32+ characters)
echo and a strong ADMIN_PASSWORD (12+ characters). See README.md for setup.
goto :failed

:dependency_error
echo ERROR: A backend, WhatsApp, or MT5 copy worker dependency is missing.
echo Install the backend dependencies and the copyTrading package:
echo   py -3 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r backend\requirements.txt -e copyTrading
goto :failed

:python_error
echo ERROR: Python could not be started. Install Python 3.11+ or create .venv.
goto :failed

:directory_error
echo ERROR: Could not switch to the project folder: %~dp0
goto :failed

:port_error
echo The backend must use http://127.0.0.1:8000 for this local setup.

:failed
echo.
pause
endlocal
exit /b 1
