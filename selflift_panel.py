"""SelfLift 逐段人在回路 —— Director 面板 ↔ run_selflift.py 的 HTTP 桥。

2026-11-11 用户定：长视频默认走 SelfLift 尾帧接首帧；Director 面板要能
「先一段段跑 → 看每段首尾帧 → 换 seed 重出 / 指定任意图作首帧 → 改提示词」。

本模块只做路由与状态编排，**生成实现仍是 skill/scripts/run_selflift.py 唯一入口**，
不复制任何出片逻辑。旧 Director 链路（首尾帧 → push/resume）完全不动。

路由注册方式和 director.py 里现有 18 个路由一致：由 director.py import 时调用
register_routes(PromptServer.instance.routes, web)，无需改 __init__.py。
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import urllib.request

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
# worker 子进程句柄单独放，绝不能塞进 TASKS —— _status 会 dict(TASKS[key]) 后
# json_response，Popen 对象不可序列化。
PROCS = {}
# 「取消全片」是服务端状态：浏览器标签页各自有 state.queue，只清前端内存的话
# 另一个标签页 / 旧页面照样继续往下提交段。
CANCELLED = set()
COMFY_API = os.environ.get("H3_COMFY_API", "http://127.0.0.1:8188").rstrip("/")
LOCK = threading.Lock()
_SCHEMA = None


def _schema_module():
    """按文件路径加载 selflift_schema.py，避免依赖 custom_nodes 的 sys.path。"""
    global _SCHEMA
    if _SCHEMA is not None:
        return _SCHEMA
    import importlib.util

    path = Path(__file__).with_name("selflift_schema.py")
    spec = importlib.util.spec_from_file_location("h3_selflift_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SCHEMA = module
    return module


def _safe(raw, must_exist=True):
    """只允许片目录与 ComfyUI input 下的文件，防路径穿越。"""
    p = Path(str(raw or "")).expanduser().resolve()
    roots = [OUTPUT_ROOT.resolve(), INPUT_ROOT.resolve()]
    if not any(p == r or r in p.parents for r in roots):
        raise ValueError(f"路径不在允许目录内：{p}")
    if must_exist and not p.is_file():
        raise FileNotFoundError(str(p))
    return p


# ---------------------------------------------------------------------------
# 片名 / 片编号（2026-11-12）
# 目录末尾的 MMDD_HHMMSS 是这部片永远不会变的编号。改名时把目录也改成
# `<名字>_<编号>`，cfg 里所有指向旧目录的绝对路径一起换掉，成片文件名同样
# 带上编号 —— 页面上的名字、磁盘上的文件夹、导出的视频三者始终对得上。
# ---------------------------------------------------------------------------
FILM_ID_RE = re.compile(r"(\d{4}_\d{6})$")
PLACEHOLDER_TITLES = {"", "film", "segments-template", "segments_template",
                      "未命名长片", "未命名", "untitled"}


def _film_id(folder, cfg=None):
    """与 driver.film_id_of 同规则：目录编号 → prefix 编号 → cfg.film_id → 目录名。"""
    m = FILM_ID_RE.search(str(folder or ""))
    if m:
        return m.group(1)
    m = FILM_ID_RE.search(str((cfg or {}).get("prefix") or ""))
    if m:
        return m.group(1)
    return str((cfg or {}).get("film_id") or folder or "").strip()


def _film_final_name(title, fid):
    """成片名 = 名字_编号_成片.mp4；名字本身已带编号时不重复。"""
    base = str(title or "").strip()
    if FILM_ID_RE.search(base):
        return f"{base}_成片.mp4"
    if re.fullmatch(r"\d{4}_\d{6}", str(fid or "")):
        return f"{base}_{fid}_成片.mp4"
    return f"{base}_成片.mp4"


def list_films_local(limit=80):
    """扫描 OUTPUT_ROOT 下所有含 segments.json 的目录，按最近动过的 mtime 倒序。

    规则与 driver.list_films 完全一致（标题/编号/文件夹/更新时间/进度/成片），但**不
    import driver**——driver 顶层会 `import pipeline`，牵动 PIL/qa_lib/direction，首屏
    刷新下拉不该付这个代价。首屏 bootstrap 总是先调它把下拉建出来，所以即使
    localStorage 里没有任何片、也没有 ?cfg=，下拉也能从文件夹里读路径直接填充。
    """
    out = []
    try:
        entries = list(os.scandir(OUTPUT_ROOT))
    except OSError:
        return out
    for ent in entries:
        try:
            if not ent.is_dir():
                continue
            cfg_path = os.path.join(ent.path, "segments.json")
            if not os.path.isfile(cfg_path):
                continue
            with open(cfg_path, encoding="utf-8") as f:
                c = json.load(f)
            segs = c.get("segments") or []
            prefix = str(c.get("prefix") or "")
            ts = os.path.getmtime(cfg_path)
            done = 0
            sd = os.path.join(ent.path, "segments")
            for sg in segs:
                vid = os.path.join(sd, f"{prefix}_{sg.get('id', '')}.mp4")
                if os.path.isfile(vid):
                    done += 1
                    ts = max(ts, os.path.getmtime(vid))
            has_final = False
            fd = os.path.join(ent.path, "final")
            if os.path.isdir(fd):
                for fn in os.scandir(fd):
                    try:
                        if fn.is_file():
                            ts = max(ts, fn.stat().st_mtime)
                            if fn.name.lower().endswith(".mp4"):
                                has_final = True
                    except OSError:
                        pass
            fid = _film_id(ent.name, c)
            title = str(c.get("title") or ent.name)
            t = title.strip().lower()
            disp = "未命名" if t in PLACEHOLDER_TITLES else title
            if t in PLACEHOLDER_TITLES and fid:
                disp = f"未命名 #{fid}"
            parts = [disp]
            if re.fullmatch(r"\d{4}_\d{6}", str(fid or "")) and f"#{fid}" not in disp:
                parts.append(f"#{fid}")
            updated_text = time.strftime("%m-%d %H:%M", time.localtime(ts))
            parts += [updated_text, f"{done}/{len(segs)}段"]
            if has_final:
                parts.append("✓成片")
            label = " · ".join(str(p) for p in parts if p)
            out.append({
                "path": os.path.abspath(cfg_path),
                "dir": ent.name,
                "title": title,
                "film_id": fid,
                "display": disp,
                "label": label,
                "search": " ".join((title, fid, ent.name, prefix)).lower(),
                "prefix": prefix,
                "updated": ts,
                "updated_text": updated_text,
                "segments": len(segs),
                "done": done,
                "has_final": has_final,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out[:limit]


def _rewrite_paths(node, old_dir, new_dir):
    """递归替换 cfg 里的绝对路径（正反斜杠两种写法都覆盖）。"""
    pairs = [(str(old_dir), str(new_dir)),
             (str(old_dir).replace("\\", "/"), str(new_dir).replace("\\", "/"))]

    def conv(s):
        for a, b in pairs:
            if a:
                s = s.replace(a, b)
        return s

    if isinstance(node, dict):
        return {k: _rewrite_paths(v, old_dir, new_dir) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_paths(v, old_dir, new_dir) for v in node]
    return conv(node) if isinstance(node, str) else node


def _delete_tree(path):
    """删除整目录，**绝不让异常逃出线程**。

    `shutil.rmtree` 在带「安全删除」垫片的环境里（如 WorkBuddy 的 sitecustomize：
    _safe_shutil_rmtree → _try_trash → 批量删除守卫）会抛 SystemExit。SystemExit 属于
    BaseException，不是 Exception —— 在线程池里抛出会顺着 aiohttp 的事件循环把整个
    服务带走（实测：删一部真实片，8900 直接退出）。所以这里连 BaseException 一起接住，
    失败也只是一次 400，不让删片把出片台弄挂。
    """
    try:
        shutil.rmtree(path)
    except BaseException as exc:  # noqa: BLE001 —— SystemExit 也要接
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from None


def _rename_film(cfg_path, new_title, rename_folder=True):
    """给本片起名，并同步目录名 / cfg 内绝对路径 / 成片文件名。

    cfg 路径会变，所以调用方（页面）必须拿返回的 cfg 重新切换当前片。
    任何一步失败都把目录名回滚，绝不留下「cfg 指向不存在的目录」的半成品。
    """
    cfg_path = Path(str(cfg_path)).resolve()
    title = str(new_title or "").strip()
    if not title:
        raise ValueError("片名不能为空")
    if any(ch in title for ch in '\\/:*?"<>|'):
        raise ValueError(r'片名不能包含 \ / : * ? " < > |')
    if len(title) > 40:
        raise ValueError("片名最多 40 个字")

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    old_dir = cfg_path.parent
    fid = _film_id(old_dir.name, raw)
    numbered = bool(re.fullmatch(r"\d{4}_\d{6}", fid))
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:32] or "film"
    if numbered and not slug.endswith("_" + fid):
        new_name = f"{slug}_{fid}"
    else:
        new_name = slug
    new_dir = old_dir.with_name(new_name) if (rename_folder and numbered) else old_dir
    if new_dir != old_dir and new_dir.exists():
        raise FileExistsError(f"已存在同名目录，换个片名再试：{new_dir.name}")

    old_final = str(raw.get("final_name") or "").strip()
    new_final = _film_final_name(title, fid)
    updated = (_rewrite_paths(raw, old_dir, new_dir)
               if new_dir != old_dir else copy.deepcopy(raw))
    updated["title"] = title
    updated["film_id"] = fid
    if not old_final or re.search(r"_成片(?:\.mp4)?$", old_final):
        updated["final_name"] = new_final

    moved = new_dir != old_dir
    final_renamed = []          # [(src, dst)]，失败时逐个回滚
    try:
        if moved:
            old_dir.rename(new_dir)
        new_cfg = new_dir / cfg_path.name
        new_cfg.write_text(json.dumps(updated, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        if old_final and new_final != old_final:
            def parts(name):
                stem, ext = (name[:-4], name[-4:]) if name.lower().endswith(".mp4") \
                    else (name, ".mp4")
                return stem, ext

            old_stem, old_ext = parts(old_final)
            new_stem, new_ext = parts(new_final)
            # 主成片 + merge 生成的「_静音」副产物一起改名，别留旧名字的残留
            for suffix in ("", "_静音"):
                src = new_dir / "final" / (old_stem + suffix + old_ext)
                if not src.is_file() and not old_final.lower().endswith(".mp4"):
                    src = new_dir / "final" / (old_final + suffix + ".mp4")
                dst = new_dir / "final" / (new_stem + suffix + new_ext)
                if src.is_file() and not dst.exists():
                    src.rename(dst)
                    final_renamed.append((src, dst))
    except Exception:
        for src, dst in reversed(final_renamed):
            dst.rename(src)
        if moved and new_dir.exists():
            new_dir.rename(old_dir)
        raise
    return {
        "cfg": str(new_cfg), "title": title, "film_id": fid,
        "dir": new_dir.name, "final_name": updated.get("final_name"),
        "old_cfg": str(cfg_path), "moved": moved,
        "final_renamed": bool(final_renamed),
    }


def _media_url(path):
    """媒体 URL 自带版本号（文件 mtime_ns + size）。

    重跑一段后文件路径不变、内容变了。以前靠前端 bust(seg.mtime) 拼版本号，
    有两个坑：① 页面没刷新过就跑的是旧 JS，压根没有 bust()，首屏缩略图
    一直吃浏览器缓存（表现为「只有第一段没更新、其他像正常」——其他段是
    lazy 加载，滚动到才请求）；② 首帧/尾帧/视频各有自己的 mtime，统一用
    段视频 mtime 代替并不准。

    版本号由服务端生成后，first_url / last_url / video_url 本身就变了；
    cardSignature 里本来就含这几个 URL，于是**即使旧页面不刷新**也会重建
    卡片并重发图片请求。前端 bust() 检测到已有 v= 会自动跳过，不会重复拼。
    """
    if not path:
        return None
    url = "/h3-long-video/selflift/media?path=" + quote(str(path), safe="")
    try:
        st = os.stat(str(path))
        ver = f"{st.st_mtime_ns:x}-{st.st_size}"
    except OSError:
        ver = "na"
    return url + "&v=" + ver


# ----------------------------------------------------------------------------
# 逐段「动作参考视频」：软参考（只接管动作/节奏/机位，不接管画风/场景）。
# 上传时把视频前 n 秒截出来存进 ComfyUI input，并在本段 body 最前面注入一段
# 带哨兵的动作参考说明；删除时把这一整段说明干净剥离。
# 哨兵用 <<<VIDEO_REF>>> 包裹，保证删除时可被确定性正则移除，不会因为用户手改
# 提示词而残留半句。
# ----------------------------------------------------------------------------
VIDEO_REF_SENTINEL = "<<<VIDEO_REF>>>"
VIDEO_REF_DIRECTIVE = (
    "【动作参考视频】参考视频仅用于动作指导：画面中的人物动作、节奏、运动幅度、"
    "朝向和机位需尽量与参考视频一致；不改变本段的人物、场景与画风设定。"
)
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv", ".wmv")
VIDEO_REF_MAX_SECONDS = 15  # 链路 reference_video_policy=official_2_to_15s 的上限


def _strip_video_ref(text):
    """把带哨兵的动作参考说明整段移除（用户可能手改过中间内容也一并清掉）。"""
    if not text:
        return ""
    return re.sub(re.escape(VIDEO_REF_SENTINEL) + r".*?" + re.escape(VIDEO_REF_SENTINEL),
                  "", text, flags=re.S).strip()


def _inject_video_ref(text):
    """在提示词最前面注入动作参考说明（先去掉旧的，避免重复）。"""
    text = _strip_video_ref(text or "")
    block = VIDEO_REF_SENTINEL + VIDEO_REF_DIRECTIVE + VIDEO_REF_SENTINEL
    return (block + "\n" + text) if text.strip() else block


def _crop_video_first_seconds(src, dst, seconds):
    """ffmpeg 截取视频前 seconds 秒 → dst（h264/yuv420p，无音频）。

    参考视频只喂帧批次给 ref_videos，不需要音轨；-an 省掉音频编码坑。
    -ss 放 -i 前做输入端 seek，截点干净。
    """
    cmd = ["ffmpeg", "-y", "-ss", "0", "-t", str(seconds), "-i", str(src),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("截取参考视频失败：" + (_decode(r.stderr))[-400:])


def _save_png(field, dst: Path) -> Path:
    """把上传的图片统一解码成 PNG 存到 dst。

    ComfyUI 的 LoadImage 按**扩展名**判断，且不吃所有格式：从网页另存下来的图常带着
    `.webp` 后缀但内容是 JPEG（名字里还夹着 `f=JPEG`、`u=` 之类参数），提交时会直接
    `Invalid image file: xxx.webp`（400 prompt_outputs_failed_validation）。
    所以这里一律 PIL 解码再存 PNG，顺手把脏文件名丢掉。
    """
    raw = field.file.read()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"不是能识别的图片文件（{getattr(field, 'filename', '?')}）：{exc}") from exc
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "PNG")
    return dst


# 子进程一律用 UTF-8 输出。
# Windows 下 Python 的 stdout 默认走 locale 编码（cp936/GBK），而我们按 utf-8 解码，
# 中文就全成乱码——症状很迷惑：页面按钮（静态 HTML）正常，但段标题/提示词（来自
# status 的 JSON）全是方块。ComfyUI venv 里跑和在 cmd 里双击启动环境不同，靠这个
# 显式指定才稳定。
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
             "PYTHONUNBUFFERED": "1"}


def _decode(raw):
    """子进程输出解码：优先 utf-8，失败回退 gbk（兼容旧日志/旧环境）。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(cfg, args, timeout=120):
    """同步调用唯一驱动；面板的 set / status 这类秒级操作走这里。

    ⚠ 这是**阻塞**调用（子进程 import numpy/pipeline，通常 0.3~2 秒）。
    handler 里一律走 `_run_async`（asyncio.to_thread），不要直接 await 这个。
    """
    cmd = [sys.executable, str(DRIVER)] + list(args) + ["--cfg", str(cfg)]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=CHILD_ENV)
    out, err = _decode(proc.stdout), _decode(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"driver 失败（退出码 {proc.returncode}）：{(err or out)[-600:]}"
        )
    return out.strip()


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
                             ("video", "video_url"),
                             ("ref_video", "ref_video_url")):
            seg[url_key] = _media_url(seg.get(key))
        seg["ref_images"] = [
            {**r, "url": _media_url(r.get("path"))}
            for r in seg.get("ref_images", [])
        ]
        seg["bg_plate_url"] = _media_url(seg.get("bg_plate_path"))
    data["ref_images"] = [
        {**r, "url": _media_url(r.get("path"))} for r in data.get("ref_images", [])
    ]
    data["bg_plate_url"] = _media_url(data.get("bg_plate_path"))
    data["finals"] = [
        {"name": name, "url": _media_url(str(cfg_dir / "final" / name))}
        for name in data.get("finals", [])
    ]
    with LOCK:
        data["task"] = dict(TASKS.get(str(cfg), {}))
        data["cancelled"] = str(cfg) in CANCELLED
        st = RUNALL.get(str(cfg))
        if st is not None:
            data["runall"] = {
                "active": bool(st.get("active")),
                "current": st.get("current"),
                "pending": list(st.get("pending") or []),
                "done": list(st.get("done") or []),
                "merging": bool(st.get("merging")),
                "error": st.get("error") or "",
                "total": st.get("total") or 0,
                "started": st.get("started"),
                "finished": st.get("finished"),
            }
    data["ok"] = True
    return data


async def _run_async(cfg, args, timeout=120):
    """把阻塞的 driver 调用挪出 event loop。

    以前 `_run` 直接在 aiohttp handler 里跑 subprocess.run：网页每 3 秒轮询一次
    status，就等于每 3 秒把 ComfyUI 的整个 HTTP 服务冻住 0.3~2 秒（前端 websocket
    心跳、进度条、队列查询一起卡）。现在统一丢线程池，ComfyUI 只管模型。
    """
    return await asyncio.to_thread(_run, cfg, args, timeout)


async def _status_async(cfg):
    return await asyncio.to_thread(_status, cfg)


def _exec_task(cfg, argv, kind, task):
    """真正执行一个 driver 子进程：流式读输出写进 task，阻塞到子进程结束。

    以前用 subprocess.run(capture_output=True) —— 进程结束前 task["log"] 永远是
    空字符串，跑 4 分钟网页上什么都不显示，只在失败时甩一坨出来。
    """
    key = str(cfg)
    text = ""
    line_no = 0
    try:
        proc = subprocess.Popen(
            [sys.executable, str(DRIVER)] + list(argv) + ["--cfg", key],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=CHILD_ENV, bufsize=0,
        )
        with LOCK:
            PROCS[key] = proc
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text += _decode(raw)
            line_no += 1
            if len(text) > 60000:          # 滚动窗口，别把内存吃满
                text = text[-45000:]
            task["log"] = text
            task["lines"] = line_no
        proc.wait()
        task["returncode"] = proc.returncode
        if proc.returncode != 0:
            task["error"] = f"退出码 {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        task["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with LOCK:
            was_cancelled = key in CANCELLED
            PROCS.pop(key, None)
        if was_cancelled:
            task["returncode"] = -1
            task["error"] = "已取消"
            text += "\n[已取消] 当前段已中断，ComfyUI 队列已清空。\n"
        task["log"] = text
        task["lines"] = line_no
        task["running"] = False
        task["finished"] = time.time()
    return task


def _start(cfg, argv, kind):
    """单段生成 / 拼接要 3~4 分钟，必须异步线程 + 状态轮询，不能卡住 HTTP。"""
    key = str(cfg)
    with LOCK:
        # 只拦「跑段」：取消后仍允许手动拼接已经跑完的那些段
        if key in CANCELLED and kind.startswith("段"):
            return False, "已取消全片：请重新点「一键跑全片」解除取消状态"
        cur = TASKS.get(key)
        if cur and cur.get("running"):
            return False, f"已有任务在跑：{cur.get('kind')}"
        task = {
            "running": True, "kind": kind, "started": time.time(),
            "log": "", "error": "", "returncode": None, "finished": None,
        }
        TASKS[key] = task

    threading.Thread(target=_exec_task, args=(cfg, argv, kind, task),
                     daemon=True, name="h3-selflift-worker").start()
    return True, None


def _run_blocking(cfg, argv, kind):
    """同步跑一个 driver 任务，阻塞到结束，返回 (ok, err)。

    给服务端跑全片队列用——队列必须「等这一段真的跑完」才推进下一段。
    """
    key = str(cfg)
    with LOCK:
        if key in CANCELLED:
            return False, "已取消"
        task = {
            "running": True, "kind": kind, "started": time.time(),
            "log": "", "error": "", "returncode": None, "finished": None,
        }
        TASKS[key] = task
    _exec_task(cfg, argv, kind, task)
    ok = task.get("returncode") == 0 and not task.get("error")
    return ok, task.get("error") or ""


# 服务端跑全片队列：key=cfg -> {pending, current, done, active, error, merging, ...}
# 队列放服务端而不是浏览器，关页面 / 刷新 / 断网 / 切到别的片都不会中断——
# 以前队列只在 JS 的 state.queue 里，页面一刷新后面几段就永远不提交了。
RUNALL = {}


def _runall_worker(cfg, segs):
    key = str(cfg)
    st = RUNALL[key]
    try:
        for seg in segs:
            with LOCK:
                if key in CANCELLED:
                    st["error"] = "已取消"
                    break
            st["current"] = seg
            ok, err = _run_blocking(cfg, ["seg", str(seg)], f"段 {seg}")
            if not ok:
                st["error"] = f"第 {seg} 段失败：{err or '未知原因'}"
                break
            # 退出码 0 也可能是「ComfyUI 中途重启丢了 history」，再确认产物真落地
            try:
                data = _status(cfg)
                item = next((s for s in data.get("segments", [])
                             if s.get("index") == seg), None)
                if not item or not item.get("done"):
                    st["error"] = (f"第 {seg} 段任务结束但视频没生成"
                                   "（多半是 ComfyUI 中途重启丢了 history）")
                    break
            except Exception as exc:  # noqa: BLE001
                st["error"] = f"第 {seg} 段检查产物失败：{exc}"
                break
            st["pending"] = [x for x in st["pending"] if x != seg]
            st["done"].append(seg)
        else:
            # 全部成功才拼接（中途 break 不会走到这里）
            with LOCK:
                cancelled = key in CANCELLED
            if not cancelled:
                st["current"] = None
                st["merging"] = True
                ok, err = _run_blocking(cfg, ["merge"], "拼接")
                st["merging"] = False
                if not ok:
                    st["error"] = f"拼接失败：{err or '未知原因'}"
    finally:
        st["active"] = False
        st["merging"] = False
        st["current"] = None
        st["finished"] = time.time()



def register_routes(routes, web):
    """由 director.py 在 `if PromptServer is not None:` 块内调用。"""

    @routes.get("/h3-long-video/selflift/status")
    async def selflift_status(request):
        try:
            cfg = _safe(request.query.get("cfg"))
            return web.json_response(await _status_async(cfg))
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.get("/h3-long-video/selflift/films")
    async def selflift_films(request):
        """只读片清单，不依赖某个具体 cfg——首屏没片历史时也能把下拉从文件夹里建出来。"""
        try:
            return web.json_response({"ok": True, "films": list_films_local()})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", "films": []})

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
            if p.get("head") is not None:
                args += ["--head", str(p["head"])]
            if p.get("tail") is not None:
                args += ["--tail", str(p["tail"])]
            if p.get("seg_head") is not None:
                args += ["--seg-head", str(p["seg_head"])]
            if p.get("first_frame") is not None:
                args += ["--first-frame", str(p["first_frame"])]
            if p.get("last_frame") is not None:
                args += ["--last-frame", str(p["last_frame"])]
            if p.get("link_mode") is not None:
                args += ["--link-mode", str(p["link_mode"])]
            if p.get("link_transition") is not None:
                args += ["--link-transition", str(p["link_transition"])]
            if p.get("megapixels") is not None:
                args += ["--megapixels", str(float(p["megapixels"]))]
            if p.get("aspect_ratio") is not None:
                args += ["--aspect-ratio", str(p["aspect_ratio"])]
            if p.get("join") is not None:
                args += ["--join", str(p["join"])]
            return web.json_response({"ok": True, "log": await _run_async(cfg, args)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/cancel")
    async def selflift_cancel(request):
        """取消全片（服务端级）/ 解除取消状态。

        op=cancel：打取消标志、终止 worker 子进程、中断 ComfyUI 正在执行的
        prompt 并清空 pending 队列；op=clear：重新开始跑片时解除标志。
        """
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            key = str(cfg)
            op = str(p.get("op") or "cancel").strip().lower()
            with LOCK:
                if op == "clear":
                    CANCELLED.discard(key)
                    return web.json_response({"ok": True, "cancelled": False})
                CANCELLED.add(key)
                proc = PROCS.pop(key, None)
                task = TASKS.get(key)
                # 服务端队列也要停，否则它会在这一段结束后接着提交下一段
                st = RUNALL.get(key)
                if st is not None:
                    st["error"] = "已取消"
                    st["active"] = False
            stopped = False
            if proc is not None and proc.poll() is None:
                proc.terminate()
                stopped = True
            # 算力后端也要停，否则 prompt 会自己跑完并占着显存
            for path, payload in (("/interrupt", {}), ("/queue", {"clear": True})):
                try:
                    req = urllib.request.Request(
                        COMFY_API + path, data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10).read()
                except Exception:
                    pass
            if task is not None:
                task["log"] = ((task.get("log") or "")
                               + "\n[已取消] 当前段已中断，ComfyUI 队列已清空。\n")
            return web.json_response({"ok": True, "cancelled": True,
                                      "worker_stopped": stopped})
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
                await _run_async(cfg, ["set", "--seg", str(seg), "--seed", str(int(p["seed"]))])
            ok, err = _start(cfg, ["seg", str(seg)], f"段 {seg}")
            if not ok:
                return web.json_response({"ok": False, "error": err}, status=409)
            return web.json_response({"ok": True, "started": True, "seg": seg})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/runall")
    async def selflift_runall(request):
        """跑全片（服务端队列）：逐段跑，全成功自动拼接。

        队列在服务端，浏览器关掉 / 刷新 / 断网 / 切到别的片都不会中断，
        页面重开只是重新读一次状态。op=stop 取消并中断当前段。
        """
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            key = str(cfg)
            op = str(p.get("op") or "start").strip().lower()

            if op == "stop":
                with LOCK:
                    CANCELLED.add(key)
                    proc = PROCS.pop(key, None)
                    st = RUNALL.get(key)
                    if st is not None:
                        st["error"] = "已取消"
                        st["active"] = False
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                # 算力后端也要停，否则 prompt 自己跑完还占着显存
                for path, payload in (("/interrupt", {}), ("/queue", {"clear": True})):
                    try:
                        req = urllib.request.Request(
                            COMFY_API + path, data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(req, timeout=10).read()
                    except Exception:
                        pass
                return web.json_response({"ok": True, "stopped": True})

            if op != "start":
                raise ValueError(f"未知操作：{op}")

            with LOCK:
                st = RUNALL.get(key)
                if st and st.get("active"):
                    return web.json_response(
                        {"ok": False, "error": "全片队列已在跑"}, status=409)
                cur = TASKS.get(key)
                if cur and cur.get("running"):
                    return web.json_response(
                        {"ok": False, "error": f"已有任务在跑：{cur.get('kind')}"}, status=409)
                CANCELLED.discard(key)

            data = await _status_async(cfg)
            segs = p.get("segs")
            if not segs:
                segs = [s["index"] for s in data.get("segments", [])
                        if not s.get("done") or s.get("stale")]
            segs = [int(x) for x in segs]
            if not segs:
                return web.json_response(
                    {"ok": False, "error": "所有段都已完成，直接点「拼接成片」即可"})

            RUNALL[key] = {
                "pending": list(segs), "current": None, "done": [],
                "active": True, "merging": False, "error": "",
                "started": time.time(), "finished": None, "total": len(segs),
            }
            threading.Thread(target=_runall_worker, args=(cfg, list(segs)),
                             daemon=True, name="h3-selflift-runall").start()
            return web.json_response({"ok": True, "started": True, "segs": segs})
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
            log = await _run_async(cfg, ["insert", "--after", str(after)], timeout=60)
            return web.json_response({"ok": True, "after": after, "log": log})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.get("/h3-long-video/app")
    async def selflift_app(request):
        """独立出片网页（同源，免 CORS）：浏览器打开 /h3-long-video/app 即可。"""
        page = Path(__file__).with_name("web") / "selflift_app.html"
        if not page.is_file():
            return web.Response(text=f"缺少页面文件：{page}", status=404)
        return web.Response(text=page.read_text(encoding="utf-8"),
                            content_type="text/html", charset="utf-8")

    @routes.get("/h3-long-video/selflift/template")
    async def selflift_template(request):
        """下载可直接照着改的分镜模板（合法 JSON，字段含义写在 _字段说明 里）。"""
        data = _schema_module().build_template()
        text = json.dumps(data, ensure_ascii=False, indent=2)
        return web.Response(
            text=text, content_type="application/json", charset="utf-8",
            headers={"Content-Disposition":
                     'attachment; filename="segments-template.json"'})

    @routes.post("/h3-long-video/selflift/preview")
    async def selflift_preview(request):
        """只做切分预览，不建目录（网页导入弹窗用）。"""
        try:
            body = await request.json()
            payload = body.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload.strip())
                except json.JSONDecodeError:
                    pass
            data = _schema_module().normalize(
                payload,
                title=(str(body.get("title") or "").strip() or None),
                seconds=body.get("seconds"),
                target_chars=int(body.get("target_chars") or 130),
                max_segments=int(body.get("max_segments") or 16),
            )
            return web.json_response({
                "ok": True, "title": data["title"], "seconds": data["seconds"],
                "segments": [{"id": s["id"], "title": s["title"], "chars": len(s["body"])}
                             for s in data["segments"]],
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/import")
    async def selflift_import(request):
        """导入任意格式的分镜 JSON：自动切段 → 建片目录 → 写 segments.json。"""
        try:
            body = await request.json()
            payload = body.get("payload")
            if isinstance(payload, str):
                text = payload.strip()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = text          # 纯文本也接受
            if payload is None:
                raise ValueError("缺少 payload")

            schema = _schema_module()
            data = schema.normalize(
                payload,
                title=(str(body.get("title") or "").strip() or None),
                seconds=body.get("seconds"),
                target_chars=int(body.get("target_chars") or 130),
                max_segments=int(body.get("max_segments") or 16),
            )
            if not data["segments"]:
                raise ValueError("切分后没有任何段")

            stamp = datetime.now().strftime("%m%d_%H%M%S")
            # 没给片名时别再叫「segments-template」：那样每部片同名，只能靠时间猜。
            # 用首段标题当默认名，目录天然就是 `<名字>_<编号>`。
            if (not str(body.get("title") or "").strip()
                    and str(data["title"]).strip().lower() in PLACEHOLDER_TITLES):
                first = str(data["segments"][0].get("title") or "").strip()
                data["title"] = (re.sub(r"\s+", " ", first)[:20].strip()
                                 or f"片_{stamp}")
            slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", data["title"])[:32] or "film"
            if slug.endswith("_" + stamp):
                out_dir = OUTPUT_ROOT / slug
            else:
                out_dir = OUTPUT_ROOT / f"{slug}_{stamp}"
            base = out_dir
            n = 2
            while out_dir.exists():            # 同一秒重复导入别覆盖已有片
                out_dir = OUTPUT_ROOT / f"{base.name}_{n}"
                n += 1
            for sub in ("refs", "segments", "qa", "final"):
                (out_dir / sub).mkdir(parents=True, exist_ok=True)
            prefix = f"sl_{stamp}"

            cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
            cfg.pop("_template_note", None)
            ref_path = str(data.get("ref_image") or "").strip()
            plate_path = str(data.get("bg_plate_path") or "").strip()
            has_ref = bool(ref_path and Path(ref_path).is_file())
            has_plate = bool(plate_path and Path(plate_path).is_file())
            cfg.update({
                "title": data["title"],
                "film_id": stamp,
                "prefix": prefix,
                "final_name": _film_final_name(data["title"], stamp),
                "seconds": data["seconds"],
                "fps": data["fps"],
                "megapixels": data["megapixels"],
                "length": int(17 * round(data["seconds"] * data["fps"] / 17) + 5),
                "head": data["head"],
                "tail": data["tail"],
                # 没有参考图就留空，别写一个「看起来合法但不存在」的占位路径——
                # 那种路径会被误当成真实人物，跑片时 ComfyUI 报 Invalid image file。
                "ref_image": ref_path if has_ref else "",
                "bg_plate_path": plate_path if has_plate else "",
                "comfy_ref_name": f"{prefix}_girl_ref.png" if has_ref else "",
                "bg_plate": Path(plate_path).name if has_plate else "",
                "join_mode": "dissolve",
                "join_dissolve_seconds": 0.25,
                "chain_first_frame": True,
            })
            # 模板里填好的人物参考图（多人）：保住顺序，comfy_name 换成新 prefix 下的唯一名。
            # 文件不存在的项直接跳过（避免建片后跑片报「找不到参考图」）。
            refs_in = data.get("ref_images") or []
            kept = []
            for i, r in enumerate(refs_in):
                p = Path(str(r.get("path") or ""))
                if not p.is_file():
                    continue
                kept.append({
                    "path": str(p),
                    "comfy_name": f"{prefix}_ref{i}{p.suffix or '.png'}",
                    "label": str(r.get("label") or f"人物 {chr(65 + i)}"),
                })
            if kept:
                cfg["ref_images"] = kept
                cfg["ref_image"] = kept[0]["path"]
                cfg["comfy_ref_name"] = kept[0]["comfy_name"]
            segments = []
            seed_base = int(body.get("seed_base") or 0)
            for index, seg in enumerate(data["segments"], 1):
                item = {
                    "id": seg["id"],
                    "title": seg["title"],
                    "seed": int(seg.get("seed") or 0) or (
                        (seed_base + index * 37) if seed_base
                        else random.randint(300000, 900000)),
                    "body": seg["body"],
                }
                if seg.get("head"):
                    item["head"] = seg["head"]
                for key in ("first_frame_override", "last_frame_override"):
                    if seg.get(key):
                        item[key] = seg[key]
                if index > 1:
                    link = seg.get("link")
                    item["link"] = (link if isinstance(link, dict)
                                    else {"mode": "chain", "transition": "dissolve"})
                segments.append(item)
            cfg["segments"] = segments

            cfg_path = out_dir / "segments.json"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            return web.json_response({
                "ok": True, "cfg": str(cfg_path), "title": cfg["title"],
                "segments": len(segments), "seconds": cfg["seconds"],
                "note": "已建片；传参考图后点「一键跑全片」",
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/rename")
    async def selflift_rename(request):
        """给本片起名字：片名、文件夹名、成片名一起带上同一个编号。

        返回新的 cfg 路径 —— 目录改名后旧路径就失效了，页面必须立刻切过去。
        """
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))
            key = str(cfg)
            with LOCK:
                if (TASKS.get(key, {}).get("running") or key in PROCS):
                    raise RuntimeError("本片正在跑任务，跑完再改名（改名会换掉片目录）")
            result = await asyncio.to_thread(
                _rename_film, cfg, p.get("title"), bool(p.get("folder", True)))
            new_key = str(result["cfg"])
            with LOCK:
                # 旧路径上的任务/取消标记搬过去，别留幽灵状态
                if key in TASKS:
                    TASKS[new_key] = TASKS.pop(key)
                if key in PROCS:
                    PROCS[new_key] = PROCS.pop(key)
                if key in CANCELLED:
                    CANCELLED.discard(key)
                    CANCELLED.add(new_key)
            result["ok"] = True
            return web.json_response(result)
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/delete")
    async def selflift_delete(request):
        """删除整部片：删掉它的输出目录（含 segments.json / 分段 / 成片 / refs）。

        只接受 OUTPUT_ROOT 下的直接片目录（即一个含 segments.json 的子目录），且要求
        本片没在跑任务。页面会在调用前弹窗让用户输入片名二次确认，这里只做最后的路径
        安全校验与文件删除，避免误删别的目录或 OUTPUT_ROOT 自身。
        """
        try:
            p = await request.json()
            cfg = _safe(p.get("cfg"))            # segments.json，必须在 OUTPUT_ROOT 下
            key = str(cfg)
            film_dir = cfg.parent
            root = OUTPUT_ROOT.resolve()
            # 护栏：只删 OUTPUT_ROOT 的直接子目录，绝不碰 OUTPUT_ROOT 自身或别的目录
            if film_dir.parent.resolve() != root:
                raise ValueError("只允许删除 OUTPUT_ROOT 下的片目录")
            if not film_dir.is_dir():
                raise FileNotFoundError(str(film_dir))
            with LOCK:
                if (TASKS.get(key, {}).get("running") or key in PROCS):
                    raise RuntimeError("本片正在跑任务，跑完再删除")
            await asyncio.to_thread(_delete_tree, film_dir)
            if film_dir.exists():
                raise RuntimeError(f"删除未完成，目录仍存在：{film_dir}")
            with LOCK:
                # 清掉这部片的任务/进程/取消态，别留幽灵状态
                TASKS.pop(key, None)
                PROCS.pop(key, None)
                CANCELLED.discard(key)
            return web.json_response({"ok": True, "dir": film_dir.name})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)

    @routes.post("/h3-long-video/selflift/init")
    async def selflift_init(request):
        """从 skill 模板开一个新片目录，产物自动写到分镜 json 同目录。"""
        try:
            p = await request.json()
            stamp = datetime.now().strftime("%m%d_%H%M%S")
            title = str(p.get("title") or "").strip() or f"片_{stamp}"
            slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:32] or "film"
            out_dir = OUTPUT_ROOT / (slug if slug.endswith("_" + stamp)
                                     else f"{slug}_{stamp}")
            for sub in ("refs", "segments", "qa", "final"):
                (out_dir / sub).mkdir(parents=True, exist_ok=True)
            cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
            prefix = f"sl_{stamp}"
            cfg.update({
                "title": title,
                "film_id": stamp,
                "prefix": prefix,
                "final_name": _film_final_name(title, stamp),
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
        """上传/删除参考图或空镜板；seg>0 时改的是该段的**段级覆盖**。

        kind=ref & index=N 指定本段第 N 张参考图；kind=plate 改本段背景板。
        op=reset 删掉段级覆盖、回落全片；op=none 让本段不接背景板。
        seg=0（缺省）时行为与以前完全一致，改的是全片配置。
        """
        try:
            form = await request.post()
            cfg = _safe(form.get("cfg"))
            kind = str(form.get("kind") or "ref").strip().lower()
            op = str(form.get("op") or "upload").strip().lower()
            if kind not in ("ref", "plate"):
                raise ValueError(f"kind 只能是 ref 或 plate：{kind}")
            idx = int(form.get("index") or 0)
            seg_no = int(form.get("seg") or 0)
            if idx < 0 or seg_no < 0:
                raise ValueError("index / seg 不能为负")
            with open(cfg, encoding="utf-8") as f:
                data = json.load(f)
            prefix = str(data.get("prefix") or "selflift")
            refs = cfg.parent / "refs"
            refs.mkdir(parents=True, exist_ok=True)

            def _save(field, stem):
                """落盘并统一转 PNG（ComfyUI LoadImage 按扩展名判断）。"""
                return _save_png(field, refs / f"{prefix}_{stem}_{int(time.time())}.png")

            target = data
            if seg_no:
                segs = data.get("segments") or []
                if not 1 <= seg_no <= len(segs):
                    raise ValueError(f"段号越界：{seg_no}（共 {len(segs)} 段）")
                target = segs[seg_no - 1]

            field = form.get("file")
            if op not in ("delete", "reset", "none") and (field is None or not hasattr(field, "file")):
                raise ValueError("缺少 file 字段")

            def _valid(arr):
                """剔掉不存在文件的项：占着 Picture 编号却取不到图会直接 400。"""
                return [a for a in arr
                        if isinstance(a, dict) and Path(str(a.get("path") or "")).is_file()]

            if kind == "ref":
                if op == "reset":
                    if target is data:
                        raise ValueError("全片参考图没有『恢复默认』，请逐张删除")
                    target.pop("ref_images", None)
                    out = {"ok": True, "kind": "ref", "seg": seg_no, "reset": True}
                else:
                    arr = target.get("ref_images")
                    if not isinstance(arr, list):
                        # 段级第一次改动：从全片拷一份，绝不改动全片配置
                        arr = copy.deepcopy(data.get("ref_images", [])) if target is not data else []
                        if not arr:  # 兼容老式单图字段，且只在文件真的存在时才搬
                            old_ref = str(data.get("ref_image") or "")
                            if old_ref and Path(old_ref).is_file():
                                arr = [{"path": old_ref,
                                        "comfy_name": str(data.get("comfy_ref_name")
                                                          or Path(old_ref).name),
                                        "label": "人物 A"}]
                    arr = _valid(arr)
                    stem = f"ref_s{seg_no}" if seg_no else f"ref{idx}"
                    if op == "delete":
                        if not 0 <= idx < len(arr):
                            raise ValueError(f"参考图序号越界：{idx}（共 {len(arr)} 张）")
                        gone = arr.pop(idx)
                        result_path = gone.get("path", "")
                    else:
                        idx = min(idx, len(arr))
                        dst = _save(field, stem)
                        item = {"path": str(dst), "comfy_name": dst.name,
                                "label": f"参考 {chr(65 + idx)}"}
                        if 0 <= idx < len(arr):
                            arr[idx] = item
                        else:
                            arr.append(item)
                        result_path = str(dst)
                    for k, a in enumerate(arr):
                        a["label"] = (f"参考 {chr(65 + k)}" if seg_no
                                      else f"人物 {chr(65 + k)}")
                    target["ref_images"] = arr
                    if target is data:
                        if arr:
                            data["ref_image"] = arr[0]["path"]
                            data["comfy_ref_name"] = (arr[0].get("comfy_name")
                                                      or Path(arr[0]["path"]).name)
                        else:
                            data.pop("ref_image", None)
                            data.pop("comfy_ref_name", None)
                    out = {"ok": True, "kind": "ref", "index": idx, "seg": seg_no,
                           "count": len(arr), "path": result_path,
                           "ref_images": [{"label": a.get("label", ""),
                                           "path": a.get("path", ""),
                                           "comfy_name": a.get("comfy_name", "")}
                                          for a in arr]}
            else:
                if op == "reset":
                    if target is data:
                        raise ValueError("全片空镜板没有『恢复默认』")
                    target.pop("bg_plate", None)
                    target.pop("bg_plate_path", None)
                    out = {"ok": True, "kind": "plate", "seg": seg_no, "reset": True}
                elif op in ("delete", "none"):
                    if target is data:
                        data.pop("bg_plate", None)
                        data.pop("bg_plate_path", None)
                        out = {"ok": True, "kind": "plate", "removed": True, "path": ""}
                    else:
                        target["bg_plate"] = ""
                        target["bg_plate_path"] = ""
                        out = {"ok": True, "kind": "plate", "seg": seg_no, "none": True}
                else:
                    dst = _save(field, f"plate_s{seg_no}" if seg_no else "plate")
                    target["bg_plate_path"] = str(dst)
                    target["bg_plate"] = dst.name
                    out = {"ok": True, "kind": "plate", "seg": seg_no,
                           "path": str(dst), "field": "bg_plate_path"}

            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return web.json_response(out)
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
            dst_name = f"selflift_s{seg:02d}_{int(time.time())}_{Path(name).stem}.png"
            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            _save_png(field, INPUT_ROOT / dst_name)
            await _run_async(cfg, ["set", "--seg", str(seg), "--first-frame", dst_name])
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
            dst_name = f"selflift_s{seg:02d}_{mode}_{int(time.time())}_{Path(name).stem}.png"
            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            dst = INPUT_ROOT / dst_name
            _save_png(field, dst)

            state = await _status_async(cfg)
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
                        await _run_async(cfg, ["set", "--seg", str(seg + 1), "--first-frame", dst_name])
                        note = (f"已替换第 {seg} 段尾帧；第 {seg + 1} 段首帧锁定为它，"
                                f"第 {seg + 1}…{total} 段请按序重跑")
                else:
                    note = f"已替换第 {seg} 段尾帧（末段没有下一段）"
            else:
                await _run_async(cfg, ["set", "--seg", str(seg), "--last-frame", dst_name])
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
            # 下载模式：外网穿透（nps/frp）对大视频 + Range/206 的响应处理很差，
            # 在线预览经常整段传不过去；直接让浏览器把整文件取回本地看，比边下边播稳得多。
            if request.query.get("download"):
                fname = p.name
                ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
                disp = (f"attachment; filename=\"{ascii_name}\"; "
                        f"filename*=UTF-8''{quote(fname)}")
                return web.FileResponse(
                    str(p),
                    headers={
                        "Content-Disposition": disp,
                        "Content-Type": "application/octet-stream",
                        "Cache-Control": "public, max-age=60",
                        "Accept-Ranges": "bytes",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            # 预览走 inline：媒体 URL 已经带版本号（?v=<mtime_ns>-<size>），重跑后 URL 字符串
            # 会变，所以不再用 no-store：no-store 会强制经过的代理/隧道禁止缓存、
            # 只能整段缓冲后才往外吐，外网带宽一受限视频就卡住、拖不动。
            # 改成可缓存 + 短 revalidate（版本号变了 URL 就不同，照样立刻拉新）：
            #  - 视频缓存久一点，外网/隧道能边下边播、重复打开秒开
            #  - 图片缓存短一点，重跑后版本号一变浏览器立刻取新图
            ext = p.suffix.lower()
            if ext in VIDEO_EXTS:
                cache = "public, max-age=300, stale-while-revalidate=600"
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                cache = "public, max-age=30, stale-while-revalidate=120"
            else:
                cache = "public, max-age=60"
            return web.FileResponse(
                str(p),
                headers={
                    "Cache-Control": cache,
                    "Content-Disposition": "inline",
                    "Accept-Ranges": "bytes",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception as exc:  # noqa: BLE001
            return web.Response(status=404, text=str(exc))

    @routes.post("/h3-long-video/selflift/refvideo")
    async def selflift_refvideo(request):
        """每段一个动作参考视频（软参考，只指导动作/机位）：

        - 上传：ffmpeg 截取视频前 n 秒（1~15）→ 存 ComfyUI input，写
          ``seg.ref_video`` / ``seg.ref_video_seconds``，并在本段 body 最前面
          注入一段带哨兵的动作参考说明。
        - 删除（op=delete）：清掉字段，并自动剥掉 body 里的动作参考说明。
        """
        try:
            form = await request.post()
            cfg = _safe(form.get("cfg"))
            seg = int(form.get("seg") or 1)
            op = str(form.get("op") or "upload").strip().lower()
            with open(cfg, encoding="utf-8") as f:
                data = json.load(f)
            segs = data.get("segments") or []
            if not 1 <= seg <= len(segs):
                raise ValueError(f"段号 {seg} 超出范围（共 {len(segs)} 段）")
            sg = segs[seg - 1]
            prefix = str(data.get("prefix") or "selflift")

            if op == "delete":
                sg.pop("ref_video", None)
                sg.pop("ref_video_seconds", None)
                sg["body"] = _strip_video_ref(sg.get("body") or "")
                with open(cfg, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return web.json_response({"ok": True, "op": "delete", "seg": seg,
                                          "body": sg.get("body", "")})

            # ---- 上传 + 截取 ----
            field = form.get("file")
            if field is None or not hasattr(field, "file"):
                raise ValueError("缺少 file 字段")
            fname = str(getattr(field, "filename", "") or "video.mp4")
            if Path(fname).suffix.lower() not in VIDEO_EXTS:
                raise ValueError("只支持视频文件（mp4/mov/webm/mkv/avi/m4v/flv/wmv）")
            seconds = float(form.get("seconds") or 8)
            seconds = max(1, min(VIDEO_REF_MAX_SECONDS, seconds))

            INPUT_ROOT.mkdir(parents=True, exist_ok=True)
            refs = cfg.parent / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            tmp = refs / f"_rv_up_{int(time.time())}{Path(fname).suffix}"
            raw = field.file.read()
            if not raw:
                raise ValueError("上传文件为空")
            tmp.write_bytes(raw)

            dst_name = f"{prefix}_s{seg:02d}_refvideo.mp4"
            dst = INPUT_ROOT / dst_name
            try:
                _crop_video_first_seconds(tmp, dst, seconds)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if not dst.is_file():
                raise RuntimeError("截取后未生成参考视频文件")

            sg["ref_video"] = dst_name
            sg["ref_video_seconds"] = seconds
            sg["body"] = _inject_video_ref(sg.get("body") or "")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return web.json_response({
                "ok": True, "op": "upload", "seg": seg,
                "filename": dst_name, "seconds": seconds,
                "url": _media_url(dst_name),
                "body": sg["body"],
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=400)
