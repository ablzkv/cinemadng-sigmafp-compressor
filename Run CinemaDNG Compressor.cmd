@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "APP=%~dp0sigma_fp_app.py"
set "PYTHONW=%~dp0runtime\pythonw.exe"
set "PYTHON=%~dp0runtime\python.exe"
if not exist "%APP%" (
  echo ERROR: sigma_fp_app.py was not found beside this launcher.
  pause
  exit /b 1
)
if exist "%PYTHONW%" (
  "%PYTHONW%" "%APP%"
  set "APP_EXIT=!ERRORLEVEL!"
  if not "!APP_EXIT!"=="0" (
    echo The app exited with code !APP_EXIT!.
    echo For diagnostics, run runtime\python.exe sigma_fp_app.py from this folder.
    pause
  )
  exit /b !APP_EXIT!
)
if not exist "%PYTHON%" set "PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: The required Python runtime was not found.
  echo Expected runtime\pythonw.exe or runtime\python.exe beside this launcher.
  pause
  exit /b 1
)
echo NOTE: pythonw.exe not found; running with python.exe (a console window will stay open).
"%PYTHON%" "%APP%"
set "APP_EXIT=!ERRORLEVEL!"
if not "!APP_EXIT!"=="0" pause
exit /b !APP_EXIT!