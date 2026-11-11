@echo off
REM SelfLift 出片台 —— 双击启动独立服务（不依赖 ComfyUI 的 HTTP 服务）
REM 需要 ComfyUI 在 8188 跑着才会有图可生成；网页本身独立于它。
setlocal
set "PY=%~dp0..\..\ComfyUI\ComfyUI\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [错误] 找不到 ComfyUI 的 python：%PY%
  echo        可以手动指定：set PY=你的python路径
  pause
  exit /b 1
)
echo 启动中……浏览器打开 http://127.0.0.1:8900/
start "" http://127.0.0.1:8900/
"%PY%" "%~dp0serve_app.py" --port 8900 %*
pause
