"""SelfLift 逐段人在回路 —— Director 面板 ↔ run_selflift.py 的 HTTP 桥。

2026-11-11 用户定：长视频默认走 SelfLift 尾帧接首帧；Director 面板要能
「先一段段跑 → 看每段首尾帧 → 换 seed 重出 / 指定任意图作首帧 → 改提示词」。

本模块只做路由与状态编排，**生成实现仍是 skill/scripts/run_selflift.py 唯一入口**，
不复制任何出片逻辑。旧 Director 链路（首尾帧 → push/resume）完全不动。

路由注册方式和 director.py 里现有 18 个路由一致：由 director.py import 时调用
register_routes(PromptServer.instance.routes, web)，无需改 __init__.py。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DRIVER = Path(os.environ.get(
    "H3_SELFLIFT_DRIVER",
    "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts/run_selflift.py",
))
TEMPLATE = Path(
    "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/assets/segments.selflift.example.json"
)
OUTPUT_ROOT = Path("D:/Comfy-Desktop/output")
INPUT_ROOT = Path("D:/Comfy-Desktop/ComfyUI-Shared/input")

TASKS = {}
LOCK = threading.Lock()


def _safe(raw, must_exist=True):
    """只允许片目录与 ComfyUI input 下的文件，防路径穿越。"""
    p = Path(str(raw or "")).expanduser().resolve()
    roots = [OUTPUT_ROOT.resolve(), INPUT_ROOT.resolve()]
    if not any(p == r or r in p.parents for r in roots):
        raise ValueError(f"路径不在允许目录内：{p}")
    if must_exist and not p.is_file():
        raise FileNotFoundError(str(p))
    return p


def _media_url(path):
    if not path:
        return None
    return "/h3-long-video/selflift/media?path=" + quote(str(path), safe="")


def _run(cfg, args, timeout=120):
    """同步调用唯一驱动；面板的 set / status 这类秒级操作走这里。"""
    cmd = [sys.executable, str(DRIVER)] + list(args) + ["--cfg", str(cfg)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"driver 失败（退出码 {proc.returncode}）："
            f"{(proc.stderr or proc.stdout or '')[-600:]}"
        )
    return (proc.stdout or "").strip()


def _status(cfg):
    """读逐段状态，补抽缺失首帧，把绝对路径换成面板可访问的 media URL。"""
    raw = _run(cfg, ["status"], timeout=60)
    line = next((ln for ln in reversed(raw.splitlines()) if ln.strip().startswith("{")), None)
    if not line:
        raise RuntimeError("status 没有返回 JSON")
    data = json.loads(line)

    cfg_dir = Path(data["cfg"]).parent
    refs = cfg_dir / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    prefix = data.get("prefix") or ""
    for seg in data.get("segments", []):
        # 历史片例没有单独的首帧图，这里就地补抽，不重生成视频
        if seg.get("video") and not seg.get("first_frame"):
            dst = refs / f"{prefix}_s{seg['index']:02d}_first.png"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", seg["video"], "-vf",
                 "select='eq(n,0)'", "-frames:v", "1", "-update", "1", str(dst)],
                check=False,
            )
            if dst.is_file():
                seg["first_frame"] = str(dst)
        for key, url_key in (("first_frame", "first_url"),
                             ("last_frame", "last_url"),
                             ("video", "video_url")):
            seg[url_key] = _media_url(seg.get(key))
    data["finals"] = [
        {"name": name, "url": _media_url(str(cfg_dir / "final" / name))}
        for name in data.get("finals", [])
    ]
    with LOCK:
        data["task"] = dict(TASKS.get(str(cfg), {}))
    data["ok"] = True
    return data


def _start(cfg, argv, kind):
    """单段生成 / 拼接要 3~4 分钟，必须异步线程 + 状态轮询，不能卡住 HTTP。"""
    key = str(cfg)
    with LOCK:
        cur = TASKS.get(key)
        if cur and cur.get("running"):
            return False, f"已有任务在跑：{cur.get('kind')}"
        task = {
            "running": True, "kind": kind, "started": time.time(),
            "log": "", "error": "", "returncode": None, "finished": None,
        }
        TASKS[key] = task

    def worker():
        try:
            proc = subprocess.run(
                [sys.executable, str(DRIVER)] + list(argv) + ["--cfg", key],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            task["log"] = ((proc.stdout or "") + (proc.stderr or ""))[-6000:]
            task["returncode"] = proc.returncode
            if proc.returncode != 0:
                task["error"] = f"退出码 {proc.returncode}"
        except Exception as exc:  # noqa: BLE001
            task["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            task["running"] = False
            task["finished"] = time.time()

    threading.Thread(target=worker, daemon=True, name="h3-selflift-worker").start()
    return True, None


def register_routes(routes, web):
    """由 director.py 在 `if PromptServer is not None:` 块内调用。"""

    @routes.get("/h3-long-video/selflift/status")
    async def selflift_status(request):
        try:
            cfg = _safe(request.query.get("cfg"))
            return web.json_response(_status(cfg))
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/set")
    async def selflift_set(request):
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            seg = int(p.get("seg") or 0)
            args = ["set", "--seg", str(seg)]
            if p.get("seed") is not None:
                args += ["--seed", str(int(p["seed"]))]
            if p.get("body") is not None:
                args += ["--body", str(p["body"])]
            if p.get("first_frame") is not None:
                args += ["--first-frame", str(p["first_frame"])]
            if p.get("last_frame") is not None:
                args += ["--last-frame", str(p["last_frame"])]
            if p.get("link_mode") is not None:
                args += ["--link-mode", str(p["link_mode"])]
            if p.get("link_transition") is not None:
                args += ["--link-transition", str(p["link_transition"])]
            return web.json_response({"ok": True, "log": _run(cfg, args)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/run")
    async def selflift_run(request):
        """跑 / 重出单段。带 seed 时先写回 cfg 再出片（换 seed 重出）。"""
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            seg = int(p.get("seg") or 1)
            if p.get("seed") is not None:
                _run(cfg, ["set", "--seg", str(seg), "--seed", str(int(p["seed"]))])
            ok, err = _start(cfg, ["seg", str(seg)], f"段 {seg}")
            if not ok:
                return web.json_response({"ok": False, "error": err}, status=409)
            return web.json_response({"ok": True, "started": True, "seg": seg})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/merge")
    async def selflift_merge(request):
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            join = str(p.get("join") or "auto")
            if join not in ("black", "dissolve", "cut", "auto"):
                raise ValueError(f"未知过场方式：{join}")
            ok, err = _start(cfg, ["merge", "--join", join], f"拼接 {join}")
            if not ok:
                return web.json_response({"ok": False, "error": err}, status=409)
            return web.json_response({"ok": True, "started": True, "join": join})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/insert")
    async def selflift_insert(request):
        """在第 after 段(1起)之后插入一个新段（默认链式承接），重编号并重命名产物。"""
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            after = int(p.get("after") or 1)
            log = _run(cfg, ["insert", "--after", str(after)], timeout=60)
            return web.json_response({"ok": True, "after": after, "log": log})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/init")
    async def selflift_init(request):
        """从 skill 模板开一个新片目录，产物自动写到分镜 json 同目录。"""
        try:
            p = await request.json()
            title = str(p.get("title") or "").strip() or "未命名长片"
            slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:32] or "film"
            stamp = datetime.now().strftime("%m%d_%H%M%S")
            out_dir = OUTPUT_ROOT / f"{slug}_{stamp}"
            for sub in ("refs", "segments", "qa", "final"):
                (out_dir / sub).mkdir(parents=True, exist_ok=True)
            cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
            prefix = f"sl_{stamp}"
            cfg.update({
                "title": title,
                "prefix": prefix,
                "final_name": f"{title}_成片",
                "ref_image": str(out_dir / "refs" / "girl_ref.png"),
                "bg_plate_path": str(out_dir / "refs" / "bg_plate.png"),
                "comfy_ref_name": f"{prefix}_girl_ref.png",
                "bg_plate": f"{prefix}_bg_plate.png",
            })
            cfg.pop("_template_note", None)
            cfg_path = out_dir / "segments.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            return web.json_response({"ok": True, "cfg": str(cfg_path),
                                      "note": "先用 ref / plate 造人物图与无人空镜板，再逐段出片"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/refimg")
    async def selflift_refimg(request):
        """上传人物参考图 / 场景空镜板，直接写回 cfg（省得手改 json）。"""
        try:
            form = await request.post()
            cfg = _safe(form.get("cfg"))
            kind = str(form.get("kind") or "ref").strip().lower()
            if kind not in ("ref", "plate"):
                raise ValueError(f"kind 只能是 ref 或 plate：{kind}")
            field = form.get("file")
            if field is None or not hasattr(field, "file"):
                raise ValueError("缺少 file 字段")
            with open(cfg, encoding="utf-8") as f:
                data = json.load(f)
            prefix = str(data.get("prefix") or "selflift")
            refs = cfg.parent / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(field.filename or "image.png")).name)
            if not safe.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                safe += ".png"
            dst = refs / f"{prefix}_{kind}_{int(time.time())}_{safe}"
            with open(dst, "wb") as out:
                out.write(field.file.read())
            if kind == "ref":
                data["ref_image"] = str(dst)
                data["comfy_ref_name"] = dst.name
            else:
                data["bg_plate_path"] = str(dst)
                data["bg_plate"] = dst.name
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return web.json_response({
                "ok": True, "kind": kind, "path": str(dst),
                "field": "ref_image" if kind == "ref" else "bg_plate_path",
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/upload")
    async def selflift_upload(request):
        """上传任意图片作某段首帧：存进 ComfyUI input，再写 first_frame_override。"""
        try:
            form = await request.post()
            cfg = _safe(form.get("cfg"))
            seg = int(form.get("seg") or 1)
            field = form.get("file")
            if field is None or not hasattr(field, "file"):
                raise ValueError("缺少 file 字段")
            name = re.sub(r"[^\w.\-]", "_", Path(str(field.filename or "frame.png")).name)
            dst_name = f"selflift_s{seg:02d}_{int(time.time())}_{name}"
            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            dst = INPUT_ROOT / dst_name
            with open(dst, "wb") as out:
                out.write(field.file.read())
            _run(cfg, ["set", "--seg", str(seg), "--first-frame", dst_name])
            return web.json_response({"ok": True, "filename": dst_name, "seg": seg})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/frame")
    async def selflift_frame(request):
        """改尾帧的两种语义（用户 07:51 指出首帧能改、尾帧不能改后补）：

        - ``next_first``（默认）：替换第 N 段尾帧帧图，并锁为第 N+1 段首帧。
          **不重出第 N 段**，只改变喂给下游的那一帧；覆盖尾帧 png 会让后续段自动 stale。
        - ``last_lock``：把图片接到第 N 段 238 节点的 ``last_frame`` 输入，
          **必须重出第 N 段**才生效。实验性：task_type=auto 会切 FL2VA/Hybrid。
        """
        try:
            form = await request.post()
            cfg = _safe(form.get("cfg"))
            seg = int(form.get("seg") or 1)
            mode = str(form.get("mode") or "next_first")
            if mode not in ("next_first", "last_lock"):
                raise ValueError(f"未知模式：{mode}")
            field = form.get("file")
            if field is None or not hasattr(field, "file"):
                raise ValueError("缺少 file 字段")
            name = re.sub(r"[^\w.\-]", "_", Path(str(field.filename or "frame.png")).name)
            dst_name = f"selflift_s{seg:02d}_{mode}_{int(time.time())}_{name}"
            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            dst = INPUT_ROOT / dst_name
            with open(dst, "wb") as out:
                out.write(field.file.read())

            state = _status(cfg)
            total = len(state.get("segments") or [])
            if not 1 <= seg <= total:
                raise ValueError(f"段号 {seg} 超出范围（共 {total} 段）")

            if mode == "next_first":
                refs = Path(state["cfg"]).parent / "refs"
                refs.mkdir(parents=True, exist_ok=True)
                target = refs / f"{state.get('prefix') or ''}_s{seg:02d}_last.png"
                shutil.copyfile(dst, target)  # 更新尾帧图（预览/资产）
                if seg < total:
                    nxt = next((s for s in state["segments"] if s["index"] == seg + 1), {})
                    nlmode = (nxt.get("link") or {}).get("mode", "chain")
                    if nlmode == "transition":
                        note = (f"已替换第 {seg} 段尾帧图；第 {seg + 1} 段是「转场接续」（独立），"
                                f"不会被锁定/强制重出，接缝按它的转场类型处理。"
                                f"要改本段画面请重出本段")
                    else:
                        _run(cfg, ["set", "--seg", str(seg + 1), "--first-frame", dst_name])
                        note = (f"已替换第 {seg} 段尾帧；第 {seg + 1} 段首帧锁定为它，"
                                f"第 {seg + 1}…{total} 段请按序重跑")
                else:
                    note = f"已替换第 {seg} 段尾帧（末段没有下一段）"
            else:
                _run(cfg, ["set", "--seg", str(seg), "--last-frame", dst_name])
                note = (f"第 {seg} 段尾帧已锁定（{dst_name}）；"
                        "点「生成本段 / 重出」后才会生效，task_type 会切 FL2VA/Hybrid")
            return web.json_response({"ok": True, "filename": dst_name,
                                      "mode": mode, "seg": seg, "note": note})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.get("/h3-long-video/selflift/media")
    async def selflift_media(request):
        try:
            p = _safe(request.query.get("path"))
            return web.FileResponse(str(p))
        except Exception as exc:  # noqa: BLE001
            return web.Response(status=404, text=str(exc))
