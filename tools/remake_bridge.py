"""把「视频复刻导演台」（remake_panel.H）挂到 aiohttp 应用上。

为什么需要这一层
----------------
`remake_panel.py` 是一个纯标准库 `http.server` 服务，它的 `H.do_GET/do_POST`
里写死了 `self.send_response / self.send_header / self.end_headers /
self.wfile.write / self.headers / self.rfile.read / self.path`。

而 SelfLift 出片台（`tools/serve_app.py`）跑在 aiohttp 上。两个服务本来各占一个
端口（8900 / 8901），用户要开两个窗口、记两个地址——没必要。

合并的关键事实
--------------
1. **路由命名空间不冲突**：复刻导演台全部走 `/api/*`，出片台全部走
   `/h3-long-video/*`，没有一条重叠。
2. **复刻导演台的 handler 对 socket 的依赖极小**：只用到上面列的 7 个成员，
   不读 `client_address`、不读 `request_version`、不自己写 chunked 编码。

所以这里做一个**伪 handler**：把 aiohttp 的 `request` 适配成 `H` 期望的形状，
把 `H` 输出的字节收集到内存里再交给 aiohttp 返回。业务代码（600 行路由、
子进程管理、切段、合成）一行都不动。

代价与边界
----------
- 响应体在内存里攒齐再发。为避免大文件走内存（`/api/media` 的成片、`/api/src-video`
  的源视频可能有几十上百 MB），这两条路由**单独**用 `web.FileResponse` 实现，
  不进伪 handler。FileResponse 自带 Range 支持，时间轴 seek 依赖它。
- `H` 是 `BaseHTTPRequestHandler` 子类，这里只调它的方法而**不**调用 `__init__`
  （那会尝试读 socket）。因此 `H` 里除了 `path / headers / rfile / wfile /
  send_response / send_header / end_headers / _body` 之外不能再引用别的实例状态
  ——新增 handler 方法时要注意。
"""
from __future__ import annotations

import io
import os
import sys
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class _FakeWFile:
    """收字节的 wfile。`H` 里只用到 write()。"""

    __slots__ = ("buf",)

    def __init__(self):
        self.buf = io.BytesIO()

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self.buf.write(data)

    def flush(self):
        pass


class _FakeRFile:
    """按 Content-Length 供字节的 rfile。`H._body()` 只用到 read(n)。"""

    __slots__ = ("_buf",)

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body or b"")

    def read(self, n=-1):
        return self._buf.read(n)


class _Headers:
    """大小写不敏感的 headers 视图，对齐 email.message 的 .get() 行为。"""

    __slots__ = ("_d",)

    def __init__(self, d):
        self._d = {str(k).lower(): v for k, v in (d or {}).items()}

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)

    def __getitem__(self, key):
        return self._d[str(key).lower()]

    def __contains__(self, key):
        return str(key).lower() in self._d


class _FakeHandler:
    """把 aiohttp request 适配成 remake_panel.H 期望的形状。

    只实现 H 真正用到的 7 个成员：path / headers / rfile / wfile /
    send_response / send_header / end_headers。
    """

    __slots__ = ("path", "headers", "rfile", "wfile", "_code", "_headers")

    def __init__(self, path: str, headers: dict, body: bytes):
        self.path = path
        self.headers = _Headers(headers)
        self.rfile = _FakeRFile(body)
        self.wfile = _FakeWFile()
        self._code = 200
        self._headers = []          # [(name, value)]，保序

    # ---- BaseHTTPRequestHandler 的响应 API ----
    def send_response(self, code, message=None):
        self._code = int(code)

    def send_header(self, name, value):
        self._headers.append((str(name), str(value)))

    def end_headers(self):
        pass                        # 头收齐了；aiohttp 那边按 _headers 一次性发出

    def log_message(self, *a):
        pass

    # ---- H.do_POST 内部会调 self._body() ----
    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    # ---- 结果 ----
    @property
    def status(self):
        return self._code

    @property
    def result_headers(self):
        return list(self._headers)

    @property
    def body(self) -> bytes:
        return self.wfile.buf.getvalue()


LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>H3 工作台</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f1115;color:#e2e8f0;
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{width:min(760px,92vw)}
  h1{font-size:20px;font-weight:600;margin:0 0 6px}
  .sub{color:#8b97a8;font-size:13px;margin-bottom:26px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:640px){.grid{grid-template-columns:1fr}}
  a.card{display:block;padding:22px 20px;border-radius:12px;text-decoration:none;
         background:#171a21;border:1px solid #262b36;color:#e2e8f0;
         transition:border-color .15s,transform .15s,background .15s}
  a.card:hover{border-color:#3d7eff;background:#1a1f28;transform:translateY(-2px)}
  .tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:99px;
       background:#1e3a5f;color:#7cb0ff;margin-bottom:10px}
  .name{font-size:16px;font-weight:600;margin-bottom:6px}
  .desc{font-size:12.5px;color:#8b97a8;line-height:1.65}
  .foot{margin-top:22px;font-size:12px;color:#5d6675}
  code{background:#1a1f28;padding:1px 5px;border-radius:4px;color:#9fb4d0}
</style></head><body><div class="wrap">
  <h1>H3 工作台</h1>
  <div class="sub">一个服务、一个端口，两个面板都在这里。</div>
  <div class="grid">
    <a class="card" href="/remake">
      <span class="tag">黑鹤二采</span>
      <div class="name">视频复刻导演台</div>
      <div class="desc">切段 · 链式首帧 · 逐段跑片 · 原音贴回 · 合成成片</div>
    </a>
    <a class="card" href="/h3-long-video/app">
      <span class="tag">SelfLift</span>
      <div class="name">出片台</div>
      <div class="desc">参考图条带 · 全片提示词 · 分段卡片 · 尾帧接首帧</div>
    </a>
  </div>
  <div class="foot">ComfyUI 只当算力后端（<code>:8188</code>），页面与它进程隔离。</div>
</div></body></html>
"""


def _resolve_media(panel, request):
    """复刻导演台 `/api/media` 的文件定位逻辑。

    2026-11-17：定位逻辑已收进 `remake_panel.media_path`（原先这里和
    `H.do_GET` 各写一份，加下载功能时两边都得改，漏改就「能看不能下」）。
    这里只保留 query 解析，避免两处分叉。

    注意 query 的取法：H 里用 `parse_qs()`，返回的是 list，所以写 `q.get("film")[0]`；
    而 aiohttp 的 `request.query` 是 MultiDict，`get()` 直接返回字符串。照抄 `[0]`
    会把片名截成第一个字（`复刻_1117` → `复`），所以这里统一走 parse_qs。
    """
    q = parse_qs(request.query_string, keep_blank_values=True)
    name = q.get("film", [None])[0]
    if not name:
        return None, None, 1, False
    try:
        idx = int(q.get("idx", ["1"])[0])
    except (TypeError, ValueError):
        idx = 1
    kind = q.get("kind", ["clip"])[0]
    dl = q.get("dl", ["0"])[0] in ("1", "true", "yes")
    wd = panel.work_dir(name)
    return wd, panel.media_path(wd, kind, idx), idx, dl


def register_remake_routes(routes, web, panel):
    """把复刻导演台挂进 aiohttp 的 RouteTableDef。

    参数
    ----
    routes : aiohttp.web.RouteTableDef
    web    : aiohttp.web 模块（由调用方传入，避免本模块硬依赖 aiohttp）
    panel  : 已 import 的 remake_panel 模块（复用它的 H 类与常量）
    """

    # ---- 大文件：走 FileResponse（流式 + 自带 Range，时间轴 seek 依赖它） ----
    # 必须在通配 /api/{tail:.*} 之前注册，aiohttp 按注册顺序匹配。
    @routes.get("/api/src-video")
    async def remake_src_video(request):
        name = request.query.get("film")
        if not name:
            return web.json_response({"error": "缺 film"}, status=400)
        st = panel.load_state(panel.work_dir(name))
        fp = st.get("src")
        if not fp or not os.path.exists(fp):
            return web.json_response(
                {"error": "源视频不存在（项目里没记录或已移动）"}, status=404)
        return web.FileResponse(fp, headers={"Cache-Control": "no-store"})

    @routes.get("/api/media")
    async def remake_media(request):
        wd, fp, idx, dl = _resolve_media(panel, request)
        if not fp or not os.path.exists(fp):
            return web.json_response({"error": "文件不存在：" + str(fp)},
                                     status=404)
        headers = {"Cache-Control": "no-store"}
        if dl:
            # 下载：带中文文件名的 attachment。FileResponse 保留 Range 能力，
            # 所以手机上「下载」大文件也能断点续传/边下边看。
            q = parse_qs(request.query_string, keep_blank_values=True)
            kind = q.get("kind", ["clip"])[0]
            headers["Content-Disposition"] = panel.content_disposition(
                panel.media_download_name(wd, kind, idx))
        return web.FileResponse(fp, headers=headers)

    async def _dispatch(request):
        # H 内部自己做 urlparse(self.path)，所以这里传原始完整路径。
        # `/remake/api/x` 这种别名前缀要先剥掉，否则 H 认不出路由。
        raw = request.path_qs
        if raw.startswith("/remake/"):
            raw = raw[len("/remake"):]
        elif raw == "/remake":
            raw = "/"

        body = b""
        if request.method == "POST":
            body = await request.read()

        h = _FakeHandler(
            path=raw,
            headers=dict(request.headers),
            body=body,
        )
        # 关键：不改 panel.H，直接调它的方法。H 的实例方法不碰 self 之外的状态。
        try:
            if request.method == "POST":
                panel.H.do_POST(h)
            else:
                panel.H.do_GET(h)
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return web.json_response(
                {"error": f"{type(exc).__name__}: {exc}"}, status=500)

        # Content-Length 由 aiohttp 按 body 重算；transfer-encoding 不能带过去
        headers = {k: v for k, v in h.result_headers
                   if k.lower() not in ("content-length", "transfer-encoding")}
        return web.Response(body=h.body, status=h.status, headers=headers)

    @routes.get("/")
    async def launcher(request):
        """根路径 = 入口页，两个面板二选一。"""
        return web.Response(text=LAUNCHER_HTML,
                            content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    @routes.get("/remake")
    @routes.get("/remake/")
    async def remake_index(request):
        html = os.path.join(ROOT_DIR, "web", "remake_app.html")
        return web.FileResponse(html, headers={"Cache-Control": "no-store"})

    # 前端全部硬编码 /api/*，所以这里通配接管，具体分发交给 H.do_GET/do_POST
    @routes.get("/api/{tail:.*}")
    async def remake_api_get(request):
        return await _dispatch(request)

    @routes.post("/api/{tail:.*}")
    async def remake_api_post(request):
        return await _dispatch(request)

    return routes
