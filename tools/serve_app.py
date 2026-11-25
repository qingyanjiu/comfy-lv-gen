"""H3 工作台 —— 独立服务入口（不依赖 ComfyUI 的 HTTP 服务）。

    <ComfyUI venv>/python.exe tools/serve_app.py            # 默认 127.0.0.1:8900
    <ComfyUI venv>/python.exe tools/serve_app.py --port 8900 --host 0.0.0.0

一个端口两个面板：
    根        /                      入口页，二选一
    出片台    /h3-long-video/app      SelfLift（参考图条带 · 全片提示词 · 分段卡片）
    复刻台    /remake                视频复刻导演台（黑鹤二采 · 切段 · 链式首帧）
    单图出片  /single                input 选图 · MP/比例 · 提示词 · 带音轨成片列表

以前这两个面板是两个独立服务、两个端口（8900 / 8901），要开两个窗口。现在合到
一个进程里：它们的路由命名空间本来就不冲突——出片台全走 `/h3-long-video/*`，
复刻台全走 `/api/*`，一条都不重叠。合并方式是 `tools/remake_bridge.py` 那个
伪 handler 适配层，复刻台 600 行业务代码一行没改。

为什么独立于 ComfyUI：
- ComfyUI 的 aiohttp 是模型服务的门面，重启/崩溃会让挂在它下面的页面一起 404；
- 网页每 3 秒轮询 status，如果 handler 里跑同步子进程，会冻住 ComfyUI 的 event loop；
- 独立后可以绑 0.0.0.0 给别的机器/手机用。

实现上**零重复**：出片台路由来自 selflift_panel.register_routes，复刻台路由来自
remake_bridge.register_remake_routes，这里只负责把两套挂到同一个 Application 上。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from aiohttp import web  # noqa: E402

import selflift_panel as SP  # noqa: E402
import single_panel as SGP  # noqa: E402

BANNER = """
──────────────────────────────────────────────────────────
  H3 工作台（独立服务，一个端口两个面板）
  入口    http://{host}:{port}/             ← 二选一
  出片台  http://{host}:{port}/h3-long-video/app     (SelfLift)
  复刻台  http://{host}:{port}/remake                (黑鹤二采)
  单图出片 http://{host}:{port}/single               (input 选图直出)
  ComfyUI {comfy}   ← 只当算力后端，可以在别的机器
──────────────────────────────────────────────────────────
""".strip()

# 实际监听端口（main 里填）。remake_panel 的 /api/version 要用它报真实端口，
# 而 build_app() 可能在解析参数之前就被调用，所以用 list 做可变容器。
_PORT = [8900]


def build_app() -> web.Application:
    app = web.Application(client_max_size=256 * 1024 ** 3)
    routes = web.RouteTableDef()
    SP.register_routes(routes, web)          # 与 ComfyUI 面板共用同一套 handler
    SGP.register_routes(routes, web)         # 单图出片台：/single/*

    # ---- 复刻导演台：可选加载，缺文件不影响出片台 ----
    remake_ok, remake_err = False, ""
    try:
        import remake_panel as RP
        import remake_bridge as RB
        RP.PORT = _PORT[0]              # /api/version 报真实监听端口
        RB.register_remake_routes(routes, web, RP)
        # 服务重启后把上次遗留的 running 归位（那些子进程已随旧服务消失）。
        # 原来这一步在 remake_panel.__main__ 里，合并后由这里接管。
        try:
            RP.reconcile_stale_jobs()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        remake_ok = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        remake_err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        print(f"[警告] 复刻导演台挂载失败（出片台不受影响）：{remake_err}",
              flush=True)

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
            "remake": remake_ok,
            "remake_error": remake_err,
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
    _PORT[0] = a.port                         # build_app 里要把这个端口告诉 remake_panel

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
