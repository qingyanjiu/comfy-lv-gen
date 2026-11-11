"""SelfLift 出片台 —— 独立服务入口（不依赖 ComfyUI 的 HTTP 服务）。

    <ComfyUI venv>/python.exe tools/serve_app.py            # 默认 127.0.0.1:8900
    <ComfyUI venv>/python.exe tools/serve_app.py --port 8900 --host 0.0.0.0

打开 http://127.0.0.1:8900/ 即可用（自动跳到 /h3-long-video/app）。

为什么要独立：
- ComfyUI 的 aiohttp 是模型服务的门面，重启/崩溃会让挂在它下面的页面一起 404；
- 网页每 3 秒轮询 status，如果 handler 里跑同步子进程，会冻住 ComfyUI 的 event loop
  （那边已经改成 asyncio.to_thread，但进程级隔离更彻底）；
- 独立后可以绑 0.0.0.0 给别的机器/手机用。

实现上**零重复**：路由与 handler 全部来自 selflift_panel.register_routes，
这里只负责把同一套 RouteTableDef 挂到自己的 Application 上。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

import selflift_panel as SP  # noqa: E402

BANNER = """
──────────────────────────────────────────────────────────
  SelfLift 出片台（独立服务）
  网页    http://{host}:{port}/
  API     http://{host}:{port}/h3-long-video/selflift/* 
  ComfyUI {comfy}   ← 只当算力后端，可以在别的机器
──────────────────────────────────────────────────────────
""".strip()


def build_app() -> web.Application:
    app = web.Application(client_max_size=256 * 1024 ** 3)
    routes = web.RouteTableDef()
    SP.register_routes(routes, web)          # 与 ComfyUI 面板共用同一套 handler

    @routes.get("/")
    async def index(request):
        raise web.HTTPFound("/h3-long-video/app")

    @routes.get("/health")
    async def health(request):
        driver = SP.DRIVER
        return web.json_response({
            "ok": True,
            "comfy": os.environ.get("H3_COMFY_API", ""),
            "driver": str(driver),
            "driver_exists": driver.is_file(),
            "output_root": str(SP.OUTPUT_ROOT),
            "output_exists": SP.OUTPUT_ROOT.is_dir(),
        })

    app.add_routes(routes)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="SelfLift 出片台独立服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；想给别的机器用就 0.0.0.0")
    ap.add_argument("--port", type=int, default=8900, help="端口，默认 8900")
    ap.add_argument("--comfy", default=os.environ.get("H3_COMFY_API",
                                                      "http://127.0.0.1:8188"),
                    help="ComfyUI 地址（run_selflift 子进程通过 H3_COMFY_API 读取）")
    ap.add_argument("--open", action="store_true",
                    help="启动约 1.5 秒后自动打开浏览器（bat 双击用）")
    a = ap.parse_args()

    # 允许只写 host:port（漏协议会让 requests/pipeline 直接失败）
    if "://" not in a.comfy:
        a.comfy = "http://" + a.comfy.strip().rstrip("/")
    a.comfy = a.comfy.rstrip("/")

    os.environ["H3_COMFY_API"] = a.comfy      # 子进程继承，run_selflift/pipeline 读它

    if not SP.DRIVER.is_file():
        print(f"[警告] 找不到驱动：{SP.DRIVER}\n"
              f"       可设环境变量 H3_SELFLIFT_DRIVER 指向 run_selflift.py", flush=True)
    if not SP.OUTPUT_ROOT.is_dir():
        print(f"[警告] 输出根目录不存在：{SP.OUTPUT_ROOT}", flush=True)

    print(BANNER.format(host=a.host, port=a.port, comfy=a.comfy), flush=True)
    if a.open and not os.environ.get("H3_NO_OPEN"):
        import threading
        import webbrowser

        host = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host

        def _open():
            try:
                webbrowser.open(f"http://{host}:{a.port}/")
            except Exception as exc:  # noqa: BLE001  打不开浏览器不该影响服务
                print(f"[提示] 自动开浏览器失败（{exc}），请手动打开 "
                      f"http://{host}:{a.port}/", flush=True)

        threading.Timer(1.5, _open).start()
    web.run_app(build_app(), host=a.host, port=a.port,
                print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
