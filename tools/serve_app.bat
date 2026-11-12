@echo off
setlocal
REM ============================================================
REM  SelfLift studio - standalone launcher
REM  Keep this file PURE ASCII + CRLF: cmd may use a GBK codepage,
REM  so non-ASCII text here would be parsed as garbage commands.
REM ============================================================

set "HERE=%~dp0"
set "SERVE=%HERE%serve_app.py"
set "PY="

REM 1) repo clone:    ...\h3-long-video-repo\tools\  -> ...\ComfyUI-Installs\ComfyUI\ComfyUI\.venv
if exist "%HERE%..\..\ComfyUI\ComfyUI\.venv\Scripts\python.exe" set "PY=%HERE%..\..\ComfyUI\ComfyUI\.venv\Scripts\python.exe"

REM 2) custom_nodes:  ...\custom_nodes\ComfyUI-H3-LongVideo\tools\ -> ...\ComfyUI\.venv
if not defined PY if exist "%HERE%..\..\..\.venv\Scripts\python.exe" set "PY=%HERE%..\..\..\.venv\Scripts\python.exe"

REM 3) manual override
if defined H3_PY set "PY=%H3_PY%"

if not defined PY goto nopy
if not exist "%SERVE%" goto noserve

echo.
echo   SelfLift studio  (standalone service)
echo   python : %PY%
echo   url    : http://127.0.0.1:8900/   (override: --port 8901)
echo   ComfyUI is used as the renderer only (default 127.0.0.1:8188).
echo   Close this window or press Ctrl+C to stop.
echo.
"%PY%" "%SERVE%" --port 8900 --host 0.0.0.0 --open %*
echo.
echo Service stopped.
pause >nul
exit /b 0

:nopy
echo.
echo [ERROR] ComfyUI python venv not found. Tried:
echo   1^) %HERE%..\..\ComfyUI\ComfyUI\.venv\Scripts\python.exe
echo   2^) %HERE%..\..\..\.venv\Scripts\python.exe
echo.
echo Fix: set H3_PY=C:/path/to/python.exe   then run this file again.
echo Press any key to exit...
pause >nul
exit /b 1

:noserve
echo.
echo [ERROR] Missing file: %SERVE%
echo Press any key to exit...
pause >nul
exit /b 1
