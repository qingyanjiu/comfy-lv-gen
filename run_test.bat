@echo off
setlocal
cd /d "%~dp0"

if not defined COMFYUI_PYTHON set "COMFYUI_PYTHON=D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\.venv\Scripts\python.exe"

if not exist "%COMFYUI_PYTHON%" (
  echo Python not found: %COMFYUI_PYTHON%
  echo Set COMFYUI_PYTHON to your ComfyUI virtual environment python.exe.
  pause
  exit /b 1
)

"%COMFYUI_PYTHON%" "%~dp0run_h3_long_video.py" --config "%~dp0config.json" --limit 1 --no-stitch
pause

