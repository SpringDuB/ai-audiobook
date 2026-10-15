@echo off
rem Double-click me: restart the backend (serve + web UI) and the worker.
rem Extra args are forwarded, e.g.  tools\restart.cmd -WithTts
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1" %*
set "code=%errorlevel%"
if not "%code%"=="0" (
  echo.
  echo Something did not come up - see the output above and the logs under data\logs.
)
rem Double-clicked (no args): keep the window open so the result is readable
if "%~1"=="" pause
endlocal & exit /b %code%
