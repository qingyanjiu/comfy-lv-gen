"""单图出片台 —— 从 ComfyUI input 目录选一张参考图，直接出一段 SelfLift 视频。

给工作台首页加的第三个应用（与出片台 / 复刻台并列），页面在 web/single_app.html。

设计边界：
- **生成实现零重复**：只写 cfg，然后调 selflift_panel 的进程管理（_start / TASKS），
  最终仍是 `run_selflift.py seg 1` 唯一驱动。这里不碰 to_api / submit / build_wf。
- **图片来源**：只读 ComfyUI-Shared/input 目录，选中的图统一转成 PNG 另存一份，
  避免 jpg 冒充 .png 的 `Invalid image file` 400，也不改用户原图。
- **音频**：cfg["audio"] 必须写 "native"。默认 off 会让 to_api 断开音轨，
  产物就没有 `-audio` 后缀，页面列表按用户要求只扫 `*-audio.mp4` 就看不到。
- **Picture 编号**：单段、无首帧、无空镜板 → ref_image_0 就是 `<Picture 1>`。
  身份锚用 `{pic:1}` 占位符交给 seg_prompt 展开，以后要接首帧也不会错位。
- **分辨率**：以 base.json 节点 115 的 multiple=32 为准（与 nodes_resolution.py
  的默认 8 不同），公式 total = mp * 1024**2，再 round(x/32)*32。

路由全部挂在 `/single/*` 命名空间，与出片台 `/h3-long-video/*`、复刻台 `/api/*` 不冲突。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import selflift_panel as SP

# 与 serve_app.py / director.py 里注册时一致；页面显示的 build 号改这里即可
SINGLE_BUILD = "2026-11-24a-single"

INPUT_ROOT = SP.INPUT_ROOT                      # D:/Comfy-Desktop/ComfyUI-Shared/input
COMFY_OUTPUT = Path("D:/Comfy-Desktop/ComfyUI-Shared/output")
JOB_ROOT = SP.OUTPUT_ROOT / "_single"           # D:/Comfy-Desktop/output/_single/<jobid>

WORKFLOW = Path(os.environ.get(
    "H3_SINGLE_WORKFLOW",
    "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/assets/H3-SelfLift_单图参考.base.json",
))

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
ASPECT_RATIOS = {
    "1:1 (Square)": (1, 1),
    "2:3 (Portrait Photo)": (2, 3),
    "3:2 (Photo)": (3, 2),
    "3:4 (Portrait Standard)": (3, 4),
    "4:3 (Standard)": (4, 3),
    "9:16 (Portrait Widescreen)": (9, 16),
    "16:9 (Widescreen)": (16, 9),
    "21:9 (Ultrawide)": (21, 9),
}
DEFAULT_ASPECT = "16:9 (Widescreen)"
DEFAULT_MP = 0.5
DEFAULT_SECONDS = 5.0
MP_MIN, MP_MAX = 0.1, 2.0          # 8GB 显存兜底；超了 ComfyUI 会 OOM

# jobid -> cfg 的 Path。只用来查 SP.TASKS 与防并发，服务重启后任务本来也断了。
JOBS: dict[str, Path] = {}

# 身份锚：单图 = 唯一 ref = P1；用 {pic:1} 而不是写死 <Picture 1>，
# seg_prompt 会按本段实际接线展开，将来接首帧也不会错位。
HEAD = ("<Picture {pic:1}> 是唯一的人物参考图。人物的脸型、五官、发型、发色与身材比例"
        "完全以它为准，全程保持同一个人。")
TAIL = "写实电影摄影，真实皮肤质感与光学散景。画面不出现文字、水印、字幕。"


def calc_resolution(mp: float, aspect: str, multiple: int = 32) -> tuple[int, int]:
    """与 H3-SelfLift base.json 节点 115（ResolutionSelector）完全一致。"""
    if aspect not in ASPECT_RATIOS:
        raise ValueError(f"不支持的比例：{aspect}")
    w_ratio, h_ratio = ASPECT_RATIOS[aspect]
    total = float(mp) * 1024 * 1024
    scale = math.sqrt(total / (w_ratio * h_ratio))
    return (round(w_ratio * scale / multiple) * multiple,
            round(h_ratio * scale / multiple) * multiple)


def list_images() -> list[dict]:
    """列 input 目录顶层图片（不递归、不读子目录）。按 mtime 倒序。"""
    out = []
    if not INPUT_ROOT.is_dir():
        return out
    for p in INPUT_ROOT.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: -x["mtime"])
    return out


def _png_copy(name: str) -> str:
    """把选中的 input 图转成 PNG 另存，返回新文件名。内容相同则复用。"""
    src = INPUT_ROOT / name
    if not src.is_file() or src.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"图片不存在或类型不支持：{name}")
    digest = hashlib.md5(src.read_bytes()).hexdigest()[:10]
    dst_name = f"single_{digest}.png"
    dst = INPUT_ROOT / dst_name
    if not dst.is_file():
        from PIL import Image
        with Image.open(src) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst, "PNG")
    return dst_name


def _new_seed() -> int:
    # 100000 是已知坏种子；随机也避开它。
    while True:
        n = random.SystemRandom().randint(100001, 2_000_000_000)
        if n != 100000:
            return n


def create_job(image: str, prompt: str, mp: float, aspect: str,
               seed: int | None = None, seconds: float = DEFAULT_SECONDS) -> dict:
    """建最小 cfg 并交给出片台的进程管理器跑单段。返回 job 信息。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    if aspect not in ASPECT_RATIOS:
        raise ValueError(f"不支持的比例：{aspect}")
    mp = float(mp)
    if not MP_MIN <= mp <= MP_MAX:
        raise ValueError(f"百万像素要在 {MP_MIN} ~ {MP_MAX} 之间（8GB 显存限制）")
    seconds = float(seconds or DEFAULT_SECONDS)
    if not 1.0 <= seconds <= 20.0:
        raise ValueError("时长要在 1 ~ 20 秒之间")
    if not WORKFLOW.is_file():
        raise ValueError(f"找不到 base 工作流：{WORKFLOW}")

    # 5060Ti 8GB 同时跑两份 SelfLift 必 OOM：只要出片台/单图台有任务在跑就拦。
    # SP.TASKS 的 key 就是 cfg 路径字符串，和 _start 内部保持一致。
    busy = [k for k, t in SP.TASKS.items() if t.get("running")]
    if busy:
        raise ValueError(f"已有任务在跑（{busy[0]}），等它结束再提交")

    png_name = _png_copy(image)
    jobid = time.strftime("single_%Y%m%d_%H%M%S_") + f"{random.randint(1000, 9999)}"
    job_dir = JOB_ROOT / jobid
    job_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = job_dir / "segments.json"
    seed = int(seed) if seed else _new_seed()
    cfg = {
        "title": f"单图出片 {jobid}",
        "prefix": jobid,
        "workflow": str(WORKFLOW),
        "ref_images": [{"path": str(INPUT_ROOT / png_name),
                        "comfy_name": png_name, "label": "人物 A"}],
        "fps": 24.0,
        "seconds": seconds,
        "megapixels": mp,
        "aspect_ratio": aspect,
        "audio": "native",           # 关键：要产出 *-audio.mp4
        "quality": "fast",
        "head": HEAD,
        "tail": TAIL,
        "segments": [{
            "id": "s01", "title": "单图生成", "seed": seed, "body": prompt,
        }],
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, err = SP._start(cfg_path, ["seg", "1"], "单图出片")
    if not ok:
        raise ValueError(err or "任务启动失败")
    JOBS[jobid] = cfg_path
    return {"job": jobid, "seed": seed, "image": png_name,
            "resolution": calc_resolution(mp, aspect)}


def _stage_from_log(text: str) -> str:
    """从 driver 日志里取最近的阶段行，作为页面上的「进度」。

    ComfyUI 的 history API 不返回采样步数总数，拿不到真实百分比；
    run_selflift 会定时打印「...渲染中 X分XX秒 队列 running=1 pending=0」，
    这是能给出的最细粒度进度。别前端瞎编百分比。
    """
    stage = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("====="):
            stage = "已提交，准备采样"
        elif line.startswith("prompt_id="):
            stage = "已进入 ComfyUI 队列"
        elif "渲染中" in line:
            stage = line.lstrip(". ").strip()
    return stage


def job_status(jobid: str) -> dict:
    cfg = JOBS.get(jobid)
    if cfg is None:
        # 服务重启后 JOBS 丢了，但任务本身也随进程结束；明确报出来别假装跑着
        return {"job": jobid, "running": False, "error": "任务不存在（服务可能重启过）"}
    task = SP.TASKS.get(str(cfg), {})
    prefix = jobid
    video = None
    for p in COMFY_OUTPUT.glob(f"{prefix}*-audio.mp4"):
        video = p.name
        break
    log = task.get("log") or ""
    return {
        "job": jobid,
        "running": bool(task.get("running")),
        "error": task.get("error") or "",
        "returncode": task.get("returncode"),
        "log": log,
        "lines": int(task.get("lines") or 0),
        "started": task.get("started"),
        "elapsed": (int(time.time() - task["started"])
                    if task.get("started") and task.get("running") else None),
        "stage": _stage_from_log(log),
        "finished": task.get("finished"),
        "video": video,
        "video_url": f"/single/video?name={quote(video)}" if video else None,
    }


def list_videos() -> list[dict]:
    """只扫 ComfyUI output 顶层带 audio 的 mp4（用户明确要求）。"""
    out = []
    if not COMFY_OUTPUT.is_dir():
        return out
    for p in COMFY_OUTPUT.iterdir():
        if not p.is_file() or p.suffix.lower() != ".mp4":
            continue
        if not p.stem.lower().endswith("-audio"):
            continue
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: -x["mtime"])
    return out


def register_routes(routes, web):
    @routes.get("/single")
    @routes.get("/single/")
    async def single_page(request):
        page = Path(__file__).with_name("web") / "single_app.html"
        return web.FileResponse(str(page), headers={"Cache-Control": "no-store"})

    @routes.get("/single/api/images")
    async def single_images(request):
        return web.json_response({"ok": True, "images": list_images()})

    @routes.get("/single/api/resolution")
    async def single_resolution(request):
        try:
            mp = float(request.query.get("mp", DEFAULT_MP))
            aspect = request.query.get("aspect", DEFAULT_ASPECT)
            if not MP_MIN <= mp <= MP_MAX:
                return web.json_response({"error": "MP 超出范围"}, status=400)
            w, h = calc_resolution(mp, aspect)
            return web.json_response({"ok": True, "width": w, "height": h})
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    @routes.post("/single/api/generate")
    async def single_generate(request):
        try:
            data = await request.json()
            result = create_job(
                image=str(data.get("image") or ""),
                prompt=str(data.get("prompt") or ""),
                mp=data.get("mp") or DEFAULT_MP,
                aspect=str(data.get("aspect") or DEFAULT_ASPECT),
                seed=data.get("seed") or None,
                seconds=data.get("seconds") or DEFAULT_SECONDS,
            )
            return web.json_response({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                     status=400)

    @routes.get("/single/api/status")
    async def single_status(request):
        jobid = request.query.get("job", "")
        if not jobid:
            return web.json_response({"error": "缺 job"}, status=400)
        return web.json_response(job_status(jobid))

    @routes.get("/single/api/videos")
    async def single_videos(request):
        return web.json_response({"ok": True, "videos": list_videos(),
                                  "build": SINGLE_BUILD})

    @routes.get("/single/video")
    async def single_video(request):
        # 只允许 basename，且必须是 output 顶层 *-audio.mp4，防目录穿越
        name = request.query.get("name", "")
        if (not name or Path(name).name != name
                or not name.lower().endswith("-audio.mp4")):
            return web.json_response({"error": "非法文件名"}, status=400)
        fp = COMFY_OUTPUT / name
        if not fp.is_file():
            return web.json_response({"error": "文件不存在"}, status=404)
        return web.FileResponse(str(fp), headers={
            "Cache-Control": "no-store", "Accept-Ranges": "bytes",
            "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
        })

    return routes
