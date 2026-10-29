@echo off
setlocal

if not defined COMFYUI_CUSTOM_NODES set "COMFYUI_CUSTOM_NODES=D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\custom_nodes"
set "TARGET=%COMFYUI_CUSTOM_NODES%\ComfyUI-H3-LongVideo"

if not exist "%COMFYUI_CUSTOM_NODES%" (
  echo ComfyUI custom_nodes folder not found:
  echo %COMFYUI_CUSTOM_NODES%
  pause
  exit /b 1
)

if exist "%TARGET%" (
  echo Custom node path already exists:
  echo %TARGET%
  echo Restart ComfyUI to load the current files.
  pause
  exit /b 0
)

mklink /J "%TARGET%" "%~dp0."
if errorlevel 1 (
  echo Failed to create the junction.
  pause
  exit /b 1
)

echo Installed. Restart ComfyUI, then open:
echo workflows\H3-Long-Video-Director.json
pause
