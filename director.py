from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from aiohttp import web
    from server import PromptServer
except ImportError:
    web = None
    PromptServer = None


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONTROLLER_PATH = ROOT / "run_h3_long_video.py"


def _resolve_jobs_root():
    """任务目录默认在包内 ``jobs/``，可以在 config.json 用 ``jobs_dir`` 指到别处。

    放外部的理由：任务目录里全是关键帧和中间产物，塞在 custom_nodes 里既占安装盘、
    也会被自定义节点更新/清理扫到。
    """
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ROOT / "jobs"
    raw = str(config.get("jobs_dir") or "").strip()
    if not raw:
        return ROOT / "jobs"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


JOB_ROOT = _resolve_jobs_root()


def _migrate_legacy_jobs():
    """任务目录换到外部后，把还在包内 jobs/ 里跑完的任务搬过去。

    正在 running/scheduled 的不动（worker 用的是绝对路径，搬了就断）；
    搬完清空的话连旧目录一起删掉，省得下次又 confusion。
    """
    legacy = ROOT / "jobs"
    if JOB_ROOT == legacy or not legacy.is_dir():
        return
    moved = 0
    try:
        entries = sorted(legacy.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_dir() or not (path / "config.json").is_file():
            continue
        try:
            status = json.loads((path / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        if str(status.get("status") or "") in {"running", "scheduled"}:
            continue
        target = JOB_ROOT / path.name
        if target.exists():
            continue
        try:
            JOB_ROOT.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved += 1
        except OSError:
            continue
    if moved:
        print(f"[H3] 已把 {moved} 个旧任务从 {legacy} 迁移到 {JOB_ROOT}", flush=True)
        try:
            if not any(legacy.iterdir()):
                legacy.rmdir()
        except OSError:
            pass


_migrate_legacy_jobs()

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_JOB = None

# The Director can stop after the keyframe stage so a human can review (and
# replace) every first/last frame before the expensive video pass starts.
STAGE_REVIEW = "关键帧 → 确认 → 视频"
STAGE_ALL = "一次跑完（不暂停）"
STAGE_VIDEOS = "继续生成视频"
STAGE_OPTIONS = [STAGE_REVIEW, STAGE_ALL, STAGE_VIDEOS]
STAGE_TO_FLAG = {
    STAGE_REVIEW: "keyframes",
    STAGE_ALL: "all",
    STAGE_VIDEOS: "videos",
}

SAMPLER_OPTIONS = [
    "euler",
    "euler_cfg_pp",
    "euler_ancestral",
    "euler_ancestral_cfg_pp",
    "heun",
    "heunpp2",
    "exp_heun_2_x0",
    "exp_heun_2_x0_sde",
    "dpm_2",
    "dpm_2_ancestral",
    "lms",
    "dpm_fast",
    "dpm_adaptive",
    "dpmpp_2s_ancestral",
    "dpmpp_2s_ancestral_cfg_pp",
    "dpmpp_sde",
    "dpmpp_sde_gpu",
    "dpmpp_2m",
    "dpmpp_2m_cfg_pp",
    "dpmpp_2m_sde",
    "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun",
    "dpmpp_2m_sde_heun_gpu",
    "dpmpp_3m_sde",
    "dpmpp_3m_sde_gpu",
    "ddpm",
    "lcm",
    "ipndm",
    "ipndm_v",
    "deis",
    "cfgpp_ud10_ab",
    "res_multistep",
    "res_multistep_cfg_pp",
    "res_multistep_ancestral",
    "res_multistep_ancestral_cfg_pp",
    "gradient_estimation",
    "gradient_estimation_cfg_pp",
    "er_sde",
    "seeds_2",
    "seeds_3",
    "sa_solver",
    "sa_solver_pece",
    "ddim",
    "uni_pc",
    "uni_pc_bh2",
]

SCHEDULER_OPTIONS = [
    "simple",
    "sgm_uniform",
    "karras",
    "exponential",
    "ddim_uniform",
    "beta",
    "normal",
    "linear_quadratic",
    "kl_optimal",
]


def _sampler_options():
    """Merge the curated defaults with everything registered in this
    ComfyUI process, so sampler packs (RES4LYF etc.) appear automatically."""
    try:
        import comfy.samplers

        names = set(SAMPLER_OPTIONS)
        names.update(comfy.samplers.SAMPLER_NAMES)
        names.update(comfy.samplers.KSAMPLER_NAMES)
        return sorted(names)
    except Exception:
        return list(SAMPLER_OPTIONS)


def _scheduler_options():
    try:
        import comfy.samplers

        names = set(SCHEDULER_OPTIONS)
        names.update(comfy.samplers.SCHEDULER_NAMES)
        return sorted(names)
    except Exception:
        return list(SCHEDULER_OPTIONS)


def _load_base_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _normalize_storyboard(
    value,
    two_pass_mode="跟随每段 JSON",
    reference_notes="",
    transition_default_seconds=1.0,
    chain_segments=True,
):
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Storyboard JSON is invalid: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("segments")
    if not isinstance(data, list) or not data:
        raise ValueError("Storyboard must contain a non-empty segments array")

    segments = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Segment {index + 1} must be an object")
        prompt = str(item.get("prompt", "")).strip()
        end_frame_prompt = str(item.get("end_frame_prompt", "")).strip()
        start_frame_prompt = str(item.get("start_frame_prompt", "")).strip()
        if not prompt:
            raise ValueError(f"Segment {index + 1} has an empty prompt")
        try:
            seconds = float(item.get("seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Segment {index + 1} has an invalid duration") from exc
        if seconds <= 0:
            raise ValueError(f"Segment {index + 1} has an invalid duration")
        hold_composition = item.get("hold_composition")
        if hold_composition is not None:
            hold_composition = bool(hold_composition)
        continue_from_previous = item.get("continue_from_previous")
        if continue_from_previous is not None:
            continue_from_previous = bool(continue_from_previous)
        transition_seconds = float(
            item.get("transition_seconds", transition_default_seconds)
        )
        effective_continue = (
            True
            if index == 0
            else (
                continue_from_previous
                if continue_from_previous is not None
                else bool(chain_segments)
            )
        )
        if transition_seconds < 0 or (
            index > 0 and not effective_continue and transition_seconds <= 0
        ):
            raise ValueError(
                f"Segment {index + 1} has an invalid transition duration"
            )
        transition_prompt = str(item.get("transition_prompt", "")).strip()
        transition_prompt = transition_prompt or (
            "电影感平滑过渡，从上一镜头结尾自然转变到下一镜头开始；"
            "保持人物身份、脸型、发型和身体比例一致，动作与场景连续，"
            "无黑场、无对白、无水印。"
        )
        free_end = item.get("free_end")
        if free_end is not None:
            free_end = bool(free_end)
        two_pass = bool(item.get("two_pass", False))
        if two_pass_mode == "全部一采":
            two_pass = False
        elif two_pass_mode == "全部二采":
            two_pass = True
        elif two_pass_mode != "跟随每段 JSON":
            raise ValueError(f"Unknown two-pass mode: {two_pass_mode}")
        prefix = str(reference_notes or "").strip()
        final_prompt = f"{prefix}\n\n{prompt}" if prefix else prompt
        segments.append(
            {
                "seconds": seconds,
                "two_pass": two_pass,
                "prompt": final_prompt,
                "end_frame_prompt": (
                    f"{prefix}\n\n{end_frame_prompt}" if prefix and end_frame_prompt else end_frame_prompt or final_prompt
                ),
                "start_frame_prompt": (
                    f"{prefix}\n\n{start_frame_prompt}" if prefix and start_frame_prompt else start_frame_prompt or final_prompt
                ),
                "hold_composition": hold_composition,
                "free_end": bool(free_end),
                "continue_from_previous": continue_from_previous,
                "transition_seconds": transition_seconds,
                "transition_prompt": (
                    f"{prefix}\n\n{transition_prompt}"
                    if prefix
                    else transition_prompt
                ),
            }
        )
    return segments


def _save_image(image, output):
    if hasattr(image, "detach"):
        image = image.detach().cpu().numpy()
    image = np.asarray(image)
    if image.ndim == 4:
        image = image[0]
    if image.ndim != 3:
        raise ValueError(f"Unsupported reference image shape: {image.shape}")
    if image.dtype != np.uint8:
        image = np.clip(image, 0.0, 1.0)
        image = (image * 255.0).round().astype(np.uint8)
    if image.shape[2] == 1:
        image = np.repeat(image, 3, axis=2)
    if image.shape[2] == 4:
        pil_image = Image.fromarray(image, mode="RGBA")
    else:
        pil_image = Image.fromarray(image[:, :, :3], mode="RGB")
    output.parent.mkdir(parents=True, exist_ok=True)
    pil_image.save(output)


def _resolve_seed(seed, randomize):
    if randomize:
        return secrets.randbits(50)
    return int(seed)


def _write_status(job_dir, **values):
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **values,
    }
    temporary = job_dir / "status.json.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, job_dir / "status.json")


def _job_directory(job_id):
    """把任务号换成任务目录，顺便挡住路径穿越。

    任务号现在允许中文（``20261101-走位测试``），所以不再用白名单正则，
    改成黑名单：路径分隔符、Windows 非法字符、控制字符一律拒绝，
    再用「解析后的父目录必须正好是 JOB_ROOT」兜底。
    """
    text = str(job_id or "")
    if not text or text in {".", ".."} or _INVALID_NAME_CHARS.search(text):
        raise ValueError("Invalid job id")
    job_dir = (JOB_ROOT / text).resolve()
    if job_dir.parent != JOB_ROOT.resolve():
        raise ValueError("Invalid job path")
    return job_dir


# 文件名里不能出现的字符。任务名会直接成为文件夹名，所以先过一遍这个。
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
JOB_NAME_MAX = 40


def _sanitize_job_name(name):
    """把用户起的名字收成能当文件夹名的样子。

    中文原样保留；空格换成 ``-``（路径里带空格后面调用脚本很容易翻车）；
    首尾的 ``-`` / ``.`` 会被 Windows 嫌弃，也去掉。
    """
    text = _INVALID_NAME_CHARS.sub("", str(name or ""))
    text = re.sub(r"\s+", "-", text).strip()
    text = text.strip("-._")
    return text[:JOB_NAME_MAX].strip("-._")


def _new_job_id(job_name=""):
    """``日期-名称``；没起名就退回老的纯时间戳 + 随机后缀。

    同一天重名就往后挂 ``-2`` / ``-3``，保证目录不撞。
    """
    cleaned = _sanitize_job_name(job_name)
    if not cleaned:
        return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    base = f"{datetime.now():%Y%m%d}-{cleaned}"
    candidate = base
    counter = 2
    while (JOB_ROOT / candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _job_date(job_id):
    """任务号前缀里的日期，返回 ``YYYY-MM-DD``；认不出来就返回空串。"""
    match = re.match(r"^(\d{4})(\d{2})(\d{2})", str(job_id or ""))
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return ""
    return f"{year}-{month}-{day}"


def _job_name_of(job_id, job_config=None):
    """任务名：优先 ``config.job_name``，否则从 ``日期-名称`` 里切出来。"""
    name = str((job_config or {}).get("job_name") or "").strip()
    if name:
        return name
    match = re.match(r"^\d{8}-(.+)$", str(job_id or ""))
    return match.group(1) if match else ""


def _dir_size(path):
    total = 0
    try:
        for item in Path(path).rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _rewrite_path_prefix(value, old_prefix, new_prefix):
    """递归把 JSON 里以旧任务目录开头的绝对路径改成新目录。

    只认「整段目录 + 分隔符」：``.../jobs/A`` 不会误伤 ``.../jobs/AB/x.png``。
    """
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if normalized.startswith(old_prefix) and (
            len(normalized) == len(old_prefix)
            or normalized[len(old_prefix)] in "/"
        ):
            return new_prefix + normalized[len(old_prefix):]
        return value
    if isinstance(value, list):
        return [_rewrite_path_prefix(item, old_prefix, new_prefix) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_path_prefix(item, old_prefix, new_prefix)
            for key, item in value.items()
        }
    return value


def _rewrite_job_paths(job_dir, old_dir, new_dir):
    """任务文件夹改名后，把 config/status/state 里写死的绝对路径同步过去。

    runner 只认 config 里的路径，不同步的话改名之后续跑会找不到首尾帧。
    """
    old_prefix = Path(old_dir).as_posix()
    new_prefix = Path(new_dir).as_posix()
    for name in ("config.json", "status.json", ".state.json"):
        path = job_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                _rewrite_path_prefix(data, old_prefix, new_prefix),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _job_config(job_dir):
    path = job_dir / "config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class JobBusyError(RuntimeError):
    """任务还在跑，不允许删 / 改名。"""


def _job_status(job_dir):
    try:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ensure_idle(job_id, job_dir, action):
    with _ACTIVE_LOCK:
        if _ACTIVE_JOB == job_id:
            raise JobBusyError(f"这个任务还在跑，等它结束再{action}")
    if str(_job_status(job_dir).get("status") or "") in {"running", "scheduled"}:
        raise JobBusyError(f"任务还在运行或排队，先在 ComfyUI 里中断再{action}")


def _job_output_files(job_dir):
    """本任务写在任务目录**之外**的文件：分段视频、转场、关键帧草稿。

    `.state.json` 里逐条记着这些绝对路径，所以按它删是精确的——不按 seed 去
    `h3_long_video/` 里通配，免得误删别的任务（关掉随机 seed 时两个任务可能同 seed）。
    """
    try:
        state = json.loads((job_dir / ".state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    root = job_dir.resolve()
    files = []
    for group in ("segments", "transitions", "keyframes"):
        for item in state.get(group) or []:
            if not isinstance(item, dict):
                continue
            for key in ("video", "path", "start_path", "end_path"):
                raw = item.get(key)
                if not raw:
                    continue
                path = Path(str(raw))
                try:
                    if not path.is_file() or root in path.resolve().parents:
                        continue
                except OSError:
                    continue
                files.append(path)
    return files


def _delete_job(job_id):
    """删掉任务目录 + 它自己生成的分段视频，返回释放的字节数。

    别的任务 `.state.json` 里也引用到的文件不删（同一个 seed 跑两次会共用文件）。
    """
    job_dir = _job_directory(job_id)
    if not job_dir.is_dir():
        raise FileNotFoundError(f"任务 {job_id} 不存在")
    _ensure_idle(job_id, job_dir, "删除")
    outputs = _job_output_files(job_dir)
    shared = set()
    for sibling in JOB_ROOT.iterdir():
        if sibling.name == job_id or not sibling.is_dir():
            continue
        for path in _job_output_files(sibling):
            try:
                shared.add(str(path.resolve()))
            except OSError:
                continue
    freed = _dir_size(job_dir)
    removed_outputs = 0
    for path in outputs:
        try:
            if str(path.resolve()) in shared:
                continue
            size = path.stat().st_size
            path.unlink()
            freed += size
            removed_outputs += 1
        except OSError:
            continue
    shutil.rmtree(job_dir)
    print(
        f"[H3] 已删除任务 {job_id}（任务目录 + {removed_outputs} 个产物，"
        f"释放 {freed} 字节）",
        flush=True,
    )
    return {
        "ok": True,
        "job_id": job_id,
        "freed": freed,
        "outputs": removed_outputs,
    }


def _rename_job(job_id, name):
    """改任务名：目录跟着变成「日期-名称」，里面的绝对路径一起改写。

    日期用任务自己那天，不用今天——不然改个名字，任务在列表里就跳到今天了。
    """
    job_dir = _job_directory(job_id)
    if not job_dir.is_dir():
        raise FileNotFoundError(f"任务 {job_id} 不存在")
    cleaned = _sanitize_job_name(name)
    if not cleaned:
        raise ValueError("名字不能为空，也不能全是非法字符")
    _ensure_idle(job_id, job_dir, "改名")
    date = _job_date(job_id) or f"{datetime.now():%Y-%m-%d}"
    base = f"{date.replace('-', '')}-{cleaned}"
    new_id = base
    counter = 2
    while (JOB_ROOT / new_id).exists() and new_id != job_id:
        new_id = f"{base}-{counter}"
        counter += 1
    if new_id != job_id:
        target = JOB_ROOT / new_id
        job_dir.rename(target)
        try:
            _rewrite_job_paths(target, job_dir, target)
        except Exception:
            # 路径没改写成功就挪回去，别留一个指不到首尾帧的任务
            target.rename(job_dir)
            raise
        job_dir = target
    config_path = job_dir / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["job_name"] = cleaned
        temporary = config_path.with_name("config.json.tmp")
        temporary.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, config_path)
    status_path = job_dir / "status.json"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["job_id"] = new_id
            temporary = status_path.with_name("status.json.tmp")
            temporary.write_text(
                json.dumps(status, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, status_path)
        except (OSError, json.JSONDecodeError):
            pass
    print(f"[H3] 任务 {job_id} 改名为 {new_id}", flush=True)
    return {"ok": True, "job_id": new_id, "name": cleaned, "label": new_id}


def _job_segments(job_dir, job_id):
    state_path = job_dir / ".state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    segments = []
    for item in sorted(state.get("segments", []), key=lambda value: value.get("segment", 0)):
        video = Path(item.get("video", ""))
        if not video.is_file():
            continue
        index = int(item["segment"])
        segments.append(
            {
                "index": index,
                "seconds": item.get("seconds"),
                "two_pass": bool(item.get("two_pass", False)),
                "filename": video.name,
                "url": (
                    f"/h3-long-video/media/{job_id}/segment/{index}"
                    f"?v={_frame_version(video, '')}"
                ),
            }
        )
    return segments


def _keyframe_source(signature):
    text = str(signature or "")
    for prefix in (
        "manual",
        "explicit",
        "hold",
        "opening-draft",
        "free",
        "from-video",
        "pending-video",
    ):
        if text.startswith(f"{prefix}:"):
            return prefix
    return "ai"


def _frame_version(path, signature):
    """Short token that changes whenever the frame image on disk changes."""
    try:
        stamp = Path(path).stat().st_mtime_ns
    except OSError:
        stamp = 0
    digest = hashlib.sha1(
        f"{Path(path).as_posix()}|{signature}|{stamp}".encode("utf-8")
    ).hexdigest()
    return digest[:8]


def _has_original(entry):
    """True when a replaced keyframe still has the AI image it replaced on disk."""
    if not isinstance(entry, dict):
        return False
    path = entry.get("path")
    return bool(path) and Path(str(path)).is_file()


def _job_keyframes(job_dir, job_id):
    state_path = job_dir / ".state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    overrides = state.get("keyframe_overrides") or {}
    attempts = state.get("keyframe_attempts") or {}
    keyframes = []
    by_index = {}
    for item in sorted(
        state.get("keyframes", []),
        key=lambda value: value.get("segment", 0),
    ):
        raw_start = item.get("start_frame")
        raw_end = item.get("end_frame")
        start_frame = Path(raw_start) if raw_start else None
        end_frame = Path(raw_end) if raw_end else None
        start_ok = bool(start_frame and start_frame.is_file())
        end_ok = bool(end_frame and end_frame.is_file())
        # A free-end segment has no end frame at all and the segment after it
        # has no start frame until that video is rendered; those cards still
        # belong in the panel, just with a placeholder on the missing side.
        index = int(item["segment"])
        entry = overrides.get(str(index)) or overrides.get(index) or {}
        attempt_entry = attempts.get(str(index)) or attempts.get(index) or {}
        # A cleared sha1 means "this frame is queued for a redraw". The media
        # URL carries a version token anyway so the browser drops its cached
        # image as soon as a frame is regenerated or replaced.
        start_sha = str(item.get("start_sha1") or "")
        end_sha = str(item.get("end_sha1") or "")
        start_path = start_frame.as_posix() if start_ok else ""
        end_path = end_frame.as_posix() if end_ok else ""
        entry_data = {
            "index": index,
            "start_url": (
                f"/h3-long-video/media/{job_id}/keyframe/{index}/start"
                f"?v={_frame_version(start_path, start_sha)}"
                if start_ok
                else None
            ),
            "end_url": (
                f"/h3-long-video/media/{job_id}/keyframe/{index}/end"
                f"?v={_frame_version(end_path, end_sha)}"
                if end_ok
                else None
            ),
            "start_available": start_ok,
            "end_available": end_ok,
            "start_attempt": int(attempt_entry.get("start", 0) or 0),
            "end_attempt": int(attempt_entry.get("end", 0) or 0),
            "start_pending": start_ok and not start_sha,
            "end_pending": end_ok and not end_sha,
            "start_source": item.get("start_source")
            or _keyframe_source(item.get("start_sha1")),
            "end_source": item.get("end_source")
            or _keyframe_source(item.get("end_sha1")),
            "start_overridden": bool(entry.get("start")),
            "end_overridden": bool(entry.get("end")),
            # 只有"替换"进来的图才存了原图，才有得还原；push 灌进来的手动帧
            # 没有原图，还原它等于把它标成待重画 —— 面板要据此把按钮置灰。
            "start_has_original": _has_original(entry.get("start_original")),
            "end_has_original": _has_original(entry.get("end_original")),
            "start_name": start_frame.name if start_ok else "",
            "end_name": end_frame.name if end_ok else "",
            "chain": bool(item.get("continue_from_previous", True)),
            "free_end": bool(item.get("free_end")),
            "start_path": start_path,
            "end_path": end_path,
            "start_inherited": False,
            # 链式段：上一段尾帧重画了、这一段还没轮到 → True（面板显示"待同步"）
            "start_stale": False,
        }
        # A chained start frame *is* the previous segment's end frame, byte for
        # byte. Serving it from that segment keeps the panel in sync the moment
        # the upstream end frame is re-rendered, instead of waiting for this
        # segment's record to be rewritten (which made the next segment look
        # stale for the whole duration of a cascade re-render).
        previous = by_index.get(index - 1)
        if previous is not None and entry_data["chain"] and previous["end_path"]:
            # 链式段的首帧按契约就等于上一段的尾帧。上一段刚重画完、这一段还
            # 没轮到重跑时，记录里存的还是旧文件 —— 一样按上一段展示，但标记
            # start_stale，让面板能说"待同步"，而不是偷偷显示一张过期图。
            entry_data.update(
                start_url=previous["end_url"],
                start_source=previous["end_source"],
                start_pending=previous["end_pending"],
                start_attempt=previous["end_attempt"],
                start_inherited=True,
                start_stale=previous["end_path"] != start_path,
            )
        by_index[index] = entry_data
        keyframes.append(entry_data)
    return keyframes


IDLE_STATUSES = {"awaiting_review", "completed", "failed", "interrupted"}


def _chained_after(config, index):
    """第 index 段之后还有哪些段是链式接着它的（首帧继承它的尾帧）。

    重画某段尾帧时，这些段的首帧都得跟着换，它们的尾帧也会因为首帧变了而重出。
    返回段号列表（1 起），遇到第一个不链式的段就停。
    """
    segments = config.get("segments") or []
    chain_default = bool(config.get("chain_segments", True))
    out = []
    for pos in range(index, len(segments)):
        item = segments[pos]
        flag = item.get("continue_from_previous")
        cont = chain_default if flag is None else bool(flag)
        if not cont:
            break
        out.append(pos + 1)
    return out


def _segment_total(job_config, state, keyframes):
    """How many segments this job is supposed to produce."""
    for value in (
        len(job_config.get("segments") or []),
        int(state.get("review_segments") or 0),
        int(state.get("keyframe_total") or 0),
        len(keyframes),
    ):
        if value:
            return int(value)
    return 0


def _keyframes_ready(keyframes, segment_total):
    """True when every segment has both frames on disk and nothing is queued."""
    if not keyframes or not segment_total:
        return False
    if len(keyframes) < segment_total:
        return False
    return all(
        item.get("start_available", True)
        and item.get("end_available", True)
        and not item.get("start_pending")
        and not item.get("end_pending")
        for item in keyframes
    )


def _review_state(status, state, keyframes, segment_total, done_segments):
    """Which action (if any) the panel should offer after a reload.

    ``awaiting_review`` alone is not enough: a job that died while rendering
    videos (or an older job that predates the review gate) has a complete
    keyframe set but a false flag, which used to leave the panel without any
    button to continue.
    """
    status_name = status.get("status")
    if state.get("awaiting_review"):
        return "gate"
    if status_name not in IDLE_STATUSES:
        return None
    ready = _keyframes_ready(keyframes, segment_total)
    if ready and segment_total and done_segments < segment_total:
        return "resume"
    if not ready and status_name in {"failed", "interrupted"}:
        return "keyframes"
    return None


def _job_transitions(job_dir, job_id):
    state_path = job_dir / ".state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    transitions = []
    for item in sorted(
        state.get("transitions", []),
        key=lambda value: value.get("to_segment", 0),
    ):
        video = Path(item.get("video", ""))
        if not video.is_file():
            continue
        index = int(item["to_segment"])
        transitions.append(
            {
                "index": index,
                "seconds": item.get("seconds"),
                "filename": video.name,
                "url": (
                    f"/h3-long-video/media/{job_id}/transition/{index}"
                    f"?v={_frame_version(video, '')}"
                ),
            }
        )
    return transitions


def _final_video(job_dir):
    config = _job_config(job_dir)
    if not config:
        return None
    output = (
        Path(config.get("final_output_dir", config.get("comfy_output_dir", "")))
        / config.get("output_prefix", "h3_long_video")
        / "long_video.mp4"
    )
    return output if output.is_file() else None


def _refresh_stale_status(job_dir, status):
    """Mark a job as interrupted when its worker process died unnoticed."""
    if status.get("status") != "running":
        return status
    pid = status.get("pid")
    if not pid:
        return status
    try:
        import psutil

        alive = psutil.pid_exists(int(pid))
    except ImportError:
        return status
    except Exception:
        return status
    if not alive:
        error = "worker process is no longer alive"
        _write_status(job_dir, status="interrupted", error=error)
        return {**status, "status": "interrupted", "error": error}
    return status


if PromptServer is not None:

    @PromptServer.instance.routes.get("/h3-long-video/status/{job_id}")
    async def h3_long_video_status(request):
        try:
            job_id = request.match_info["job_id"]
            job_dir = _job_directory(job_id)
            status_path = job_dir / "status.json"
            try:
                status = (
                    json.loads(status_path.read_text(encoding="utf-8"))
                    if status_path.is_file()
                    else {"status": "unknown"}
                )
            except (json.JSONDecodeError, OSError):
                status = {"status": "unknown"}
            status = _refresh_stale_status(job_dir, status)
            segments = _job_segments(job_dir, job_id)
            keyframes = _job_keyframes(job_dir, job_id)
            transitions = _job_transitions(job_dir, job_id)
            job_config = _job_config(job_dir)
            state_path = job_dir / ".state.json"
            try:
                state = (
                    json.loads(state_path.read_text(encoding="utf-8"))
                    if state_path.is_file()
                    else {}
                )
            except (json.JSONDecodeError, OSError):
                state = {}
            final_video = _final_video(job_dir)
            segment_total = _segment_total(job_config, state, keyframes)
            script = [
                {
                    "index": order,
                    "seconds": item.get("seconds"),
                    "prompt": item.get("prompt", ""),
                    # 首/尾帧单独写的提示词。空 = 跟主提示词一样（runner 会回落）。
                    "start_frame_prompt": str(item.get("start_frame_prompt") or ""),
                    "end_frame_prompt": str(item.get("end_frame_prompt") or ""),
                    "two_pass": bool(item.get("two_pass", False)),
                }
                for order, item in enumerate(job_config.get("segments") or [], 1)
            ]
            return web.json_response(
                {
                    **status,
                    "job_id": job_id,
                    "job_name": _job_name_of(job_id, job_config),
                    "script": script,
                    "segments": segments,
                    "completed_segments": len(segments),
                    "segment_total": segment_total,
                    "keyframes": keyframes,
                    "completed_keyframes": len(keyframes),
                    "keyframes_ready": _keyframes_ready(keyframes, segment_total),
                    "transitions": transitions,
                    "phase": state.get("phase"),
                    "stage": state.get("stage") or job_config.get("stage") or "all",
                    "awaiting_review": bool(state.get("awaiting_review")),
                    "review_state": _review_state(
                        status, state, keyframes, segment_total, len(segments)
                    ),
                    "review_segments": state.get("review_segments"),
                    "seed": job_config.get("base_seed"),
                    "final_url": (
                        f"/h3-long-video/media/{job_id}/final"
                        f"?v={_frame_version(final_video, '')}"
                        if final_video is not None
                        else None
                    ),
                    "final_filename": final_video.name if final_video else None,
                }
            )
        except Exception as exc:
            return web.json_response(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.get("/h3-long-video/jobs")
    async def h3_long_video_jobs(request):
        """Recent jobs, newest first, so the panel can pick one to look at."""
        try:
            if not JOB_ROOT.is_dir():
                return web.json_response({"ok": True, "jobs": []})
            entries = []
            for path in JOB_ROOT.iterdir():
                if not path.is_dir() or not (path / "config.json").is_file():
                    continue
                try:
                    entries.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            entries.sort(key=lambda item: item[0], reverse=True)
            jobs = []
            for mtime, path in entries[:40]:
                job_id = path.name
                try:
                    status = json.loads(
                        (path / "status.json").read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    status = {}
                config = _job_config(path)
                segments = config.get("segments") or []
                jobs.append(
                    {
                        "job_id": job_id,
                        # 下拉框直接用 job_id（新任务就是「日期-名称」），
                        # 老任务没有命名，就把日期单独拆出来方便看。
                        "label": job_id,
                        "name": _job_name_of(job_id, config),
                        "date": _job_date(job_id),
                        "status": status.get("status", "unknown"),
                        "segments": len(segments),
                        "completed_segments": len(_job_segments(path, job_id)),
                        "seconds": round(
                            sum(float(item.get("seconds") or 0) for item in segments),
                            2,
                        ),
                        "has_final": _final_video(path) is not None,
                        "bytes": _dir_size(path),
                        "mtime": mtime,
                    }
                )
            return web.json_response({"ok": True, "jobs": jobs})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post("/h3-long-video/delete/{job_id}")
    async def h3_long_video_delete(request):
        """删掉一个任务的整个文件夹（首尾帧、日志、中间产物）。"""
        try:
            return web.json_response(_delete_job(request.match_info["job_id"]))
        except JobBusyError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post("/h3-long-video/rename/{job_id}")
    async def h3_long_video_rename(request):
        """改任务名，目录名跟着变成「日期-名称」。"""
        try:
            payload = await request.json()
            return web.json_response(
                _rename_job(request.match_info["job_id"], payload.get("name"))
            )
        except JobBusyError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post("/h3-long-video/script/{job_id}/{index}")
    async def h3_long_video_edit_script(request):
        """Edit one segment's prompt / seconds while its keyframes are on screen."""
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            job_dir = _job_directory(job_id)
            config_path = job_dir / "config.json"
            if not config_path.is_file():
                raise FileNotFoundError(f"任务 {job_id} 没有 config.json")
            payload = await request.json()
            config = json.loads(config_path.read_text(encoding="utf-8"))
            segments = config.get("segments") or []
            if not 1 <= index <= len(segments):
                raise ValueError(f"段号 {index} 超出范围（共 {len(segments)} 段）")
            segment = segments[index - 1]
            changed = []
            if payload.get("seconds") is not None:
                seconds = float(payload["seconds"])
                if not 1.0 <= seconds <= 30.0:
                    raise ValueError("秒数要在 1–30 秒之间")
                if float(segment.get("seconds") or 0) != seconds:
                    segment["seconds"] = seconds
                    changed.append("seconds")
            if payload.get("prompt") is not None:
                prompt = str(payload["prompt"]).strip()
                if not prompt:
                    raise ValueError("提示词不能为空")
                if segment.get("prompt") != prompt:
                    segment["prompt"] = prompt
                    changed.append("prompt")
            # 首帧 / 尾帧各自的提示词。允许留空——留空等于「跟主提示词一样」，
            # runner 里 `end_frame_prompt or prompt` 会自己回落。
            for field in ("start_frame_prompt", "end_frame_prompt"):
                if payload.get(field) is None:
                    continue
                value = str(payload[field]).strip()
                if str(segment.get(field) or "") != value:
                    segment[field] = value
                    changed.append(field)
            if payload.get("two_pass") is not None:
                two_pass = bool(payload["two_pass"])
                if bool(segment.get("two_pass", False)) != two_pass:
                    segment["two_pass"] = two_pass
                    changed.append("two_pass")
            if changed:
                temporary = config_path.with_name("config.json.tmp")
                temporary.write_text(
                    json.dumps(config, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(temporary, config_path)

            queued = False
            note = None
            sides = []
            cascade = []
            # 影响关键帧画面的字段：主提示词（首/尾帧没单独写时的回落）、
            # 以及首/尾帧各自的提示词。
            frame_fields = {"prompt", "start_frame_prompt", "end_frame_prompt"}
            # 用户显式勾了「连手动图一起重出」：这时即使文字没改动也要重画。
            force = bool(payload.get("force"))
            if payload.get("redraw") and not (frame_fields & set(changed)) and not force:
                note = "只有时长变化，关键帧不用重画；生成视频时会按新时长跑这一段"
            elif payload.get("redraw"):
                try:
                    state = _load_job_state(job_dir)
                except FileNotFoundError:
                    state = None
                if state is None:
                    note = "该任务还没有关键帧，先点「继续生成关键帧」跑一次"
                else:
                    # 提示词改的是哪一侧，就只重画哪一侧：
                    #   start 帧用 start_frame_prompt，空则回落到主提示词；
                    #   end   帧用 end_frame_prompt，空则回落到主提示词。
                    # 这样改尾帧提示词不会把已经满意的首帧一起重摇。
                    sides = []
                    if "start_frame_prompt" in changed or (
                        "prompt" in changed
                        and not str(segment.get("start_frame_prompt") or "").strip()
                    ):
                        sides.append("start")
                    if "end_frame_prompt" in changed or (
                        "prompt" in changed
                        and not str(segment.get("end_frame_prompt") or "").strip()
                    ):
                        sides.append("end")
                    if not sides:
                        if force:
                            # 勾了「连手动图一起重出」但没指明改哪侧：两侧都按当前词重出
                            sides = ["start", "end"]
                        else:
                            note = (
                                "这一段的首帧、尾帧都写了专属提示词，改主提示词不影响关键帧；"
                                "生成视频时会用新词。想重出某一帧就用那张卡上的「重画」。"
                            )
                    if sides:
                        cleared = False
                        manual_only = True
                        for value in state.get("keyframes", []):
                            try:
                                same = int(value.get("segment", -1)) == index
                            except (TypeError, ValueError):
                                continue
                            if not same:
                                continue
                            for side in sides:
                                is_manual = value.get(f"{side}_source") == "manual"
                                # 手动放进去的图默认不覆盖，只刷新 AI 生成的那一侧
                                if is_manual and not force:
                                    continue
                                if is_manual:
                                    # 强制重画 = 撤掉那张手动图，交回给 AI
                                    state.setdefault("keyframe_overrides", {}).setdefault(
                                        str(index), {}
                                    ).pop(side, None)
                                    attempts = state.setdefault(
                                        "keyframe_attempts", {}
                                    ).setdefault(str(index), {})
                                    attempts[side] = int(attempts.get(side, 0) or 0) + 1
                                manual_only = False
                                value[f"{side}_sha1"] = ""
                                value[f"{side}_prompt_id"] = None
                                value[f"{side}_source"] = "ai"
                                cleared = True
                        if cleared:
                            _save_job_state(job_dir, state)
                            started, reason = _start_stage_worker(
                                job_dir, job_id, "keyframes", tag="script"
                            )
                            queued = started
                            note = None if started else reason
                            if "end" in sides:
                                # 尾帧变了，后面链式段的首帧也得跟着走（它们的尾帧
                                # 会因为首帧变了而重出）。这里只用于告知，不额外改
                                # state —— runner 的链式传递自己会把它们带上。
                                cascade = _chained_after(config, index)
                        elif manual_only:
                            note = (
                                "这一段的首尾帧是你自己放进去的图，默认不会按提示词重画。"
                                "要按新词重出：勾上「连我自己放的图一起重出」再点一次，"
                                "或在对应那张卡上点「重画」。"
                            )
                        else:
                            note = "这一段的帧没有可重画的部分"
            return web.json_response(
                {
                    "ok": True,
                    "job_id": job_id,
                    "index": index,
                    "changed": changed,
                    "queued": queued,
                    "note": note,
                    # 这次实际重画了哪几帧（start/end），前端用它说清楚做了什么
                    "sides": sides,
                    # 尾帧变了会连带重出的后续段号（链式继承）
                    "cascade": cascade,
                    "segment": {
                        "index": index,
                        "seconds": segment.get("seconds"),
                        "prompt": segment.get("prompt", ""),
                        "start_frame_prompt": str(
                            segment.get("start_frame_prompt") or ""
                        ),
                        "end_frame_prompt": str(
                            segment.get("end_frame_prompt") or ""
                        ),
                        "two_pass": bool(segment.get("two_pass", False)),
                    },
                }
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.get("/h3-long-video/media/{job_id}/segment/{index}")
    async def h3_long_video_segment_media(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            job_dir = _job_directory(job_id)
            segment = next(
                item for item in _job_segments(job_dir, job_id) if item["index"] == index
            )
            state = json.loads((job_dir / ".state.json").read_text(encoding="utf-8"))
            source = next(
                item for item in state["segments"] if int(item["segment"]) == index
            )
            video = Path(source["video"]).resolve()
            if not video.is_file():
                raise FileNotFoundError(video)
            return web.FileResponse(video, headers={"Content-Type": "video/mp4"})
        except Exception:
            raise web.HTTPNotFound()

    @PromptServer.instance.routes.get(
        "/h3-long-video/media/{job_id}/keyframe/{index}/{side}"
    )
    async def h3_long_video_keyframe_media(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            side = request.match_info["side"]
            if side not in {"start", "end"}:
                raise ValueError("Invalid keyframe side")
            job_dir = _job_directory(job_id)
            state = json.loads((job_dir / ".state.json").read_text(encoding="utf-8"))
            item = next(
                value
                for value in state["keyframes"]
                if int(value["segment"]) == index
            )
            image = Path(item[f"{side}_frame"]).resolve()
            if not image.is_file():
                raise FileNotFoundError(image)
            return web.FileResponse(image, headers={"Content-Type": "image/png"})
        except Exception:
            raise web.HTTPNotFound()

    @PromptServer.instance.routes.get(
        "/h3-long-video/media/{job_id}/transition/{index}"
    )
    async def h3_long_video_transition_media(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            job_dir = _job_directory(job_id)
            state = json.loads((job_dir / ".state.json").read_text(encoding="utf-8"))
            item = next(
                value
                for value in state["transitions"]
                if int(value["to_segment"]) == index
            )
            video = Path(item["video"]).resolve()
            if not video.is_file():
                raise FileNotFoundError(video)
            return web.FileResponse(video, headers={"Content-Type": "video/mp4"})
        except Exception:
            raise web.HTTPNotFound()

    @PromptServer.instance.routes.get("/h3-long-video/media/{job_id}/final")
    async def h3_long_video_final_media(request):
        try:
            job_id = request.match_info["job_id"]
            final_video = _final_video(_job_directory(job_id))
            if final_video is None:
                raise FileNotFoundError("Final video is not ready")
            return web.FileResponse(final_video, headers={"Content-Type": "video/mp4"})
        except Exception:
            raise web.HTTPNotFound()


    def _load_job_state(job_dir):
        path = job_dir / ".state.json"
        if not path.is_file():
            raise FileNotFoundError("该任务还没有状态文件，请先运行一次生成")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_job_state(job_dir, state):
        path = job_dir / ".state.json"
        temporary = path.with_name(".state.json.tmp")
        temporary.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _keyframe_item(state, index):
        for value in state.get("keyframes", []):
            try:
                if int(value.get("segment", -1)) == index:
                    return value
            except (TypeError, ValueError):
                continue
        raise KeyError(f"第 {index} 段关键帧还不存在")

    async def _write_keyframe_image(target, request):
        """Persist an uploaded/referenced image and return the PNG path."""
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/"):
            data = None
            reader = await request.multipart()
            async for part in reader:
                if part.name in {"image", "file"}:
                    data = await part.read()
            if not data:
                raise ValueError("上传的图片内容为空")
            image = Image.open(BytesIO(data))
        else:
            payload = {}
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if payload.get("image_base64"):
                raw = str(payload["image_base64"])
                if raw.startswith("data:") and "," in raw:
                    raw = raw.split(",", 1)[1]
                image = Image.open(BytesIO(base64.b64decode(raw)))
            elif payload.get("path"):
                source = Path(str(payload["path"])).expanduser()
                if not source.is_file():
                    raise FileNotFoundError(f"图片不存在：{source}")
                image = Image.open(source)
            else:
                raise ValueError("缺少 image 文件，或 path / image_base64 字段")
        image.convert("RGB").save(target)
        return target

    @PromptServer.instance.routes.post(
        "/h3-long-video/keyframe/{job_id}/{index}/{side}/replace"
    )
    async def h3_long_video_replace_keyframe(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            side = request.match_info["side"]
            if side not in {"start", "end"}:
                raise ValueError("Invalid keyframe side")
            job_dir = _job_directory(job_id)
            state = _load_job_state(job_dir)
            item = _keyframe_item(state, index)

            target_dir = job_dir / "keyframes"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"seg{index:02d}_{side}_manual.png"
            await _write_keyframe_image(target, request)

            overrides = state.setdefault("keyframe_overrides", {})
            entry = overrides.setdefault(str(index), {})
            if entry.get(side) != target.as_posix():
                entry[f"{side}_original"] = {
                    "path": item.get(f"{side}_frame"),
                    "sha1": item.get(f"{side}_sha1"),
                    "prompt_id": item.get(f"{side}_prompt_id"),
                }
            entry[side] = target.as_posix()
            item[f"{side}_frame"] = target.as_posix()
            item[f"{side}_sha1"] = f"manual:{target.as_posix()}"
            item[f"{side}_prompt_id"] = None
            item[f"{side}_source"] = "manual"
            _save_job_state(job_dir, state)
            # Replacing a frame changes every downstream chained frame. Run the
            # keyframe stage right away so the tree is consistent *before* the
            # human hits "confirm" (otherwise those re-renders would happen
            # inside the video stage, where nobody expects them).
            queued = False
            note = None
            run_now = request.query.get("run", "1") not in ("0", "false", "no")
            if run_now and (job_dir / "config.json").is_file():
                started, reason = _start_stage_worker(
                    job_dir, job_id, "keyframes", tag="replace"
                )
                queued = started
                note = None if started else reason
            return web.json_response(
                {
                    "ok": True,
                    "job_id": job_id,
                    "index": index,
                    "side": side,
                    "path": target.as_posix(),
                    "url": f"/h3-long-video/media/{job_id}/keyframe/{index}/{side}",
                    "queued": queued,
                    "note": note,
                }
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post(
        "/h3-long-video/keyframe/{job_id}/{index}/{side}/reroll"
    )
    async def h3_long_video_reroll_keyframe(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            side = request.match_info["side"]
            if side not in {"start", "end"}:
                raise ValueError("Invalid keyframe side")
            job_dir = _job_directory(job_id)
            state = _load_job_state(job_dir)
            item = _keyframe_item(state, index)

            overrides = state.setdefault("keyframe_overrides", {})
            overrides.setdefault(str(index), {}).pop(side, None)
            attempts = state.setdefault("keyframe_attempts", {})
            entry = attempts.setdefault(str(index), {})
            entry[side] = int(entry.get(side, 0) or 0) + 1
            # Clearing the signature makes the runner regenerate this frame,
            # and the bumped attempt feeds a different keyframe seed.
            item[f"{side}_sha1"] = ""
            item[f"{side}_prompt_id"] = None
            item[f"{side}_source"] = "ai"
            _save_job_state(job_dir, state)

            # ``run`` defaults to true so a click actually redraws the frame
            # instead of only tagging it. The batch button passes run=false for
            # every frame except the last one, so a whole batch costs one run.
            payload = {}
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            queued = False
            if payload.get("run", True) and (job_dir / "config.json").is_file():
                started, reason = _start_stage_worker(
                    job_dir, job_id, "keyframes", tag="reroll"
                )
                if not started:
                    # The mark is already saved; report it as a soft failure so
                    # the panel can tell the user to press 确认 later instead of
                    # pretending the request was rejected outright.
                    return web.json_response(
                        {
                            "ok": True,
                            "job_id": job_id,
                            "index": index,
                            "side": side,
                            "attempt": entry[side],
                            "queued": False,
                            "note": reason,
                        }
                    )
                queued = True
            return web.json_response(
                {
                    "ok": True,
                    "job_id": job_id,
                    "index": index,
                    "side": side,
                    "attempt": entry[side],
                    "queued": queued,
                }
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post(
        "/h3-long-video/keyframe/{job_id}/{index}/{side}/reset"
    )
    async def h3_long_video_reset_keyframe(request):
        try:
            job_id = request.match_info["job_id"]
            index = int(request.match_info["index"])
            side = request.match_info["side"]
            if side not in {"start", "end"}:
                raise ValueError("Invalid keyframe side")
            job_dir = _job_directory(job_id)
            state = _load_job_state(job_dir)
            item = _keyframe_item(state, index)

            overrides = state.setdefault("keyframe_overrides", {})
            entry = overrides.setdefault(str(index), {})
            original = entry.get(f"{side}_original")
            if not _has_original(original):
                # 没有原图可还（典型：push 灌进来的 segNN_*_manual.png）。
                # 以前这里会把 sha1 清空、source 改成 ai，卡片立刻变成"待重画"，
                # 下一次跑关键帧就重画了 —— 用户点的是"还原"，不该偷偷排重画。
                raise ValueError(
                    "这一侧没有可还原的原图（这张图是从外部灌进来的）。"
                    "想按提示词重出请点「重画」，想换图请点「替换」。"
                )
            entry.pop(f"{side}_original", None)
            entry.pop(side, None)
            item[f"{side}_frame"] = original["path"]
            item[f"{side}_sha1"] = original.get("sha1")
            item[f"{side}_prompt_id"] = original.get("prompt_id")
            item[f"{side}_source"] = _keyframe_source(original.get("sha1"))
            _save_job_state(job_dir, state)
            return web.json_response(
                {"ok": True, "job_id": job_id, "index": index, "side": side}
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post("/h3-long-video/keyframes/{job_id}")
    async def h3_long_video_run_keyframes(request):
        """Redraw/complete the keyframe stage for an existing (idle) job."""
        try:
            job_id = request.match_info["job_id"]
            job_dir = _job_directory(job_id)
            if not (job_dir / "config.json").is_file():
                raise FileNotFoundError(f"任务 {job_id} 没有 config.json，无法继续")
            started, reason = _start_stage_worker(
                job_dir, job_id, "keyframes", tag="keyframes"
            )
            if not started:
                return web.json_response(
                    {
                        "ok": False,
                        "job_id": job_id,
                        "queued": False,
                        "note": reason,
                        "error": reason,
                    },
                    status=409,
                )
            return web.json_response({"ok": True, "job_id": job_id, "queued": True})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )

    @PromptServer.instance.routes.post("/h3-long-video/resume/{job_id}")
    async def h3_long_video_resume(request):
        global _ACTIVE_JOB
        try:
            job_id = request.match_info["job_id"]
            job_dir = _job_directory(job_id)
            config_path = job_dir / "config.json"
            if not config_path.is_file():
                raise FileNotFoundError(
                    f"任务 {job_id} 没有 config.json，无法继续"
                )
            payload = {}
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            with _ACTIVE_LOCK:
                if _ACTIVE_JOB is not None:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": f"任务 {_ACTIVE_JOB} 正在运行，请等它结束",
                        },
                        status=409,
                    )
                _ACTIVE_JOB = job_id
            command = [
                sys.executable,
                str(CONTROLLER_PATH),
                "--config",
                str(config_path),
                "--stage",
                "videos",
            ]
            if payload.get("force"):
                command.append("--force")
            try:
                worker = threading.Thread(
                    target=_run_worker,
                    args=(command, job_dir),
                    name=f"h3-long-video-resume-{job_id}",
                    daemon=True,
                )
                worker.start()
            except Exception:
                with _ACTIVE_LOCK:
                    _ACTIVE_JOB = None
                raise
            return web.json_response(
                {
                    "ok": True,
                    "job_id": job_id,
                    "status_url": f"/h3-long-video/status/{job_id}",
                }
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status=400,
            )


def _start_stage_worker(job_dir, job_id, stage, *, force=False, tag="stage"):
    """Run one stage of the controller in the background.

    Returns ``(True, None)`` when the worker was started, or
    ``(False, reason)`` when another job is already occupying the slot.
    """
    global _ACTIVE_JOB
    with _ACTIVE_LOCK:
        if _ACTIVE_JOB is not None:
            return False, f"任务 {_ACTIVE_JOB} 正在运行，请等它结束"
        _ACTIVE_JOB = job_id
    command = [
        sys.executable,
        str(CONTROLLER_PATH),
        "--config",
        str(job_dir / "config.json"),
        "--stage",
        stage,
    ]
    if force:
        command.append("--force")
    try:
        worker = threading.Thread(
            target=_run_worker,
            args=(command, job_dir),
            name=f"h3-long-video-{tag}-{job_id}",
            daemon=True,
        )
        worker.start()
    except Exception:
        with _ACTIVE_LOCK:
            _ACTIVE_JOB = None
        raise
    return True, None


def _wait_for_comfy_idle(comfy_url, timeout=90.0):
    """Block until ComfyUI's running queue is empty (best effort)."""
    try:
        import requests
    except ImportError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{comfy_url}/queue", timeout=5)
            if response.status_code == 200 and not response.json().get(
                "queue_running"
            ):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _run_worker(command, job_dir):
    global _ACTIVE_JOB
    try:
        # The outer ComfyUI prompt must finish before its child prompt is queued.
        try:
            comfy_url = json.loads(
                (job_dir / "config.json").read_text(encoding="utf-8")
            ).get("comfy_url")
        except Exception:
            comfy_url = None
        if not (comfy_url and _wait_for_comfy_idle(comfy_url)):
            time.sleep(2.0)
        log_path = job_dir / "run.log"
        with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            _write_status(
                job_dir,
                status="running",
                pid=process.pid,
                log=str(log_path),
                config=str(job_dir / "config.json"),
            )
            return_code = process.wait()
        if return_code == 0:
            # The runner can stop after the keyframe stage and wait for a human
            # to review (and optionally replace) the generated frames.
            try:
                state = json.loads(
                    (job_dir / ".state.json").read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                state = {}
            final_status = (
                "awaiting_review" if state.get("awaiting_review") else "completed"
            )
            _write_status(
                job_dir,
                status=final_status,
                return_code=return_code,
                log=str(log_path),
                config=str(job_dir / "config.json"),
            )
        else:
            _write_status(
                job_dir,
                status="failed",
                return_code=return_code,
                log=str(log_path),
                config=str(job_dir / "config.json"),
            )
    except Exception as exc:
        _write_status(
            job_dir,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_JOB = None


class H3LongVideoDirector:
    CATEGORY = "MiniMax H3"
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "status")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        optional = {
            f"reference_{index}": ("IMAGE",)
            for index in range(2, 10)
        }
        return {
            "required": {
                "storyboard": (
                    "STRING",
                    {
                        "multiline": True,
                        "dynamicPrompts": False,
                        "default": '{"segments":[{"seconds":6,"two_pass":false,"prompt":"第一段提示词"}]}',
                    },
                ),
                "reference_notes": (
                    "STRING",
                    {
                        "multiline": True,
                        "dynamicPrompts": False,
                        "default": (
                            "Picture 1（参考图1）是<女主角>的主要身份、脸型、五官、发型和"
                            "身高比例基准；只有在剧情已经换好最终服装后才参考它的服装，"
                            "宿舍试穿阶段必须按每段提示词更换衣服。\n"
                            "Picture 2（参考图2）是中性表情脸型参考，用于稳定眼睛、"
                            "鼻子、脸型和闭嘴嘴宽；不要复制它的白色上衣和背景。\n"
                            "所有段落必须保持与这些参考图一致的<女主角>身份，"
                            "不得混入其他人物的脸或发型；服装状态严格跟随各段剧情。"
                        ),
                    },
                ),
                "steps": ("INT", {"default": 6, "min": 1, "max": 200, "step": 1}),
                "two_pass_steps": (
                    "INT",
                    {"default": 5, "min": 1, "max": 200, "step": 1},
                ),
                "sampler": (_sampler_options(), {"default": "res_multistep"}),
                "scheduler": (_scheduler_options(), {"default": "simple"}),
                "megapixels": (
                    "FLOAT",
                    {"default": 0.2, "min": 0.05, "max": 4.0, "step": 0.01},
                ),
                "final_width": (
                    "INT",
                    {"default": 720, "min": 256, "max": 4096, "step": 8},
                ),
                "final_height": (
                    "INT",
                    {"default": 1280, "min": 256, "max": 4096, "step": 8},
                ),
                "two_pass_mode": (
                    ["跟随每段 JSON", "全部一采", "全部二采"],
                    {"default": "跟随每段 JSON"},
                ),
                "force": ("BOOLEAN", {"default": False}),
                "keyframe_steps": (
                    "INT",
                    {"default": 6, "min": 1, "max": 200, "step": 1},
                ),
                "reference_1": ("IMAGE",),
            },
            "optional": {
                **optional,
                "advanced_overrides": (
                    "STRING",
                    {
                        "multiline": True,
                        "dynamicPrompts": False,
                        "default": "{}",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1125899906842623,
                        "step": 1,
                    },
                ),
                "randomize_seed": ("BOOLEAN", {"default": True}),
                "first_frame": ("IMAGE",),
                "stage": (STAGE_OPTIONS, {"default": STAGE_REVIEW}),
                "resume_job_id": (
                    "STRING",
                    {
                        "multiline": False,
                        "dynamicPrompts": False,
                        "default": "",
                    },
                ),
                # 放在最后一个：老工作流的 widgets_values 是按顺序存的，
                # 插在中间会把 resume_job_id 的值顶到新控件上。
                "job_name": (
                    "STRING",
                    {
                        "multiline": False,
                        "dynamicPrompts": False,
                        "default": "",
                    },
                ),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    @classmethod
    def execute(
        cls,
        storyboard,
        reference_notes,
        steps,
        two_pass_steps,
        sampler,
        scheduler,
        megapixels,
        final_width,
        final_height,
        two_pass_mode,
        force,
        keyframe_steps,
        reference_1,
        advanced_overrides="{}",
        seed=0,
        randomize_seed=True,
        first_frame=None,
        stage=STAGE_REVIEW,
        job_name="",
        resume_job_id="",
        **reference_images,
    ):
        global _ACTIVE_JOB

        requested_stage = STAGE_TO_FLAG.get(str(stage), "keyframes")
        resume_job_id = str(resume_job_id or "").strip()
        if requested_stage == "videos":
            if not resume_job_id:
                raise ValueError(
                    "「继续生成视频」需要填写 resume_job_id（要继续的任务 ID）"
                )
            job_id = resume_job_id
            job_dir = _job_directory(job_id)
            if not (job_dir / "config.json").is_file():
                raise FileNotFoundError(
                    f"任务 {job_id} 不存在或缺少 config.json，无法继续"
                )
            command = [
                sys.executable,
                str(CONTROLLER_PATH),
                "--config",
                str(job_dir / "config.json"),
                "--stage",
                "videos",
            ]
            if force:
                command.append("--force")
            with _ACTIVE_LOCK:
                if _ACTIVE_JOB is not None:
                    raise RuntimeError(
                        f"H3 long-video job {_ACTIVE_JOB} is still running"
                    )
                _ACTIVE_JOB = job_id
            _write_status(
                job_dir,
                status="scheduled",
                job_id=job_id,
                config=str(job_dir / "config.json"),
            )
            worker = threading.Thread(
                target=_run_worker,
                args=(command, job_dir),
                name=f"h3-long-video-resume-{job_id}",
                daemon=True,
            )
            worker.start()
            status = (
                f"Job {job_id} resumed: rendering segment videos from the "
                "reviewed keyframes."
            )
            return {
                "ui": {
                    "text": (status,),
                    "h3_director": [
                        {
                            "job_id": job_id,
                            "status": status,
                            "status_url": f"/h3-long-video/status/{job_id}",
                        }
                    ],
                },
                "result": (job_id, status),
            }

        with _ACTIVE_LOCK:
            if _ACTIVE_JOB is not None:
                raise RuntimeError(
                    f"H3 long-video job {_ACTIVE_JOB} is still running"
                )
            _ACTIVE_JOB = "reserved"

        try:
            try:
                overrides = json.loads(advanced_overrides or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"advanced_overrides is invalid JSON: {exc}") from exc
            if not isinstance(overrides, dict):
                raise ValueError("advanced_overrides must be a JSON object")
            if "segments" in overrides or "reference_images" in overrides:
                raise ValueError(
                    "segments and reference_images are controlled by the Director node"
                )
            config = _load_base_config()
            config.update(overrides)
            segments = _normalize_storyboard(
                storyboard,
                two_pass_mode,
                reference_notes,
                float(config.get("transition_default_seconds", 1.0)),
                bool(config.get("chain_segments", True)),
            )

            job_id = _new_job_id(job_name)
            job_dir = JOB_ROOT / job_id
            job_dir.parent.mkdir(parents=True, exist_ok=True)
            job_dir.mkdir(parents=True, exist_ok=False)

            supplied_images = [reference_1]
            for index in range(2, 10):
                image = reference_images.get(f"reference_{index}")
                if image is not None:
                    supplied_images.append(image)

            saved_references = []
            for index, image in enumerate(supplied_images, start=1):
                output = job_dir / f"reference_{index}.png"
                _save_image(image, output)
                saved_references.append(output.as_posix())

            opening_frame = None
            if first_frame is not None:
                opening_frame = job_dir / "opening_frame.png"
                _save_image(first_frame, opening_frame)

            config["steps"] = int(steps)
            config["two_pass_steps"] = int(two_pass_steps)
            config["keyframe_steps"] = int(keyframe_steps)
            config["sampler"] = sampler
            config["scheduler"] = scheduler
            config["two_pass_scheduler"] = scheduler
            config["megapixels"] = float(megapixels)
            config["final_width"] = int(final_width)
            config["final_height"] = int(final_height)
            actual_seed = _resolve_seed(seed, randomize_seed)
            config["base_seed"] = actual_seed
            if config["megapixels"] <= 0:
                raise ValueError("megapixels must be greater than 0")
            if config["final_width"] >= config["final_height"]:
                raise ValueError("final resolution must remain portrait")
            ratio = config["final_width"] / config["final_height"]
            if abs(ratio - (9 / 16)) > 0.02:
                raise ValueError("final resolution must keep the 9:16 portrait preset")
            config["segments"] = segments
            config["reference_images"] = saved_references
            if opening_frame is not None:
                config["opening_frame"] = opening_frame.as_posix()
            config["stage"] = requested_stage
            config["job_name"] = _sanitize_job_name(job_name)
            config_path = job_dir / "config.json"
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(CONTROLLER_PATH),
                "--config",
                str(config_path),
                "--stage",
                requested_stage,
            ]
            if force:
                command.append("--force")

            _write_status(
                job_dir,
                status="scheduled",
                job_id=job_id,
                config=str(config_path),
                references=saved_references,
            )
            with _ACTIVE_LOCK:
                _ACTIVE_JOB = job_id
            worker = threading.Thread(
                target=_run_worker,
                args=(command, job_dir),
                name=f"h3-long-video-{job_id}",
                daemon=True,
            )
            worker.start()
            stage_hint = (
                "关键帧生成完成后会暂停等待确认，确认后再生成视频。"
                if requested_stage == "keyframes"
                else "关键帧与视频一次跑完。"
            )
            status = (
                f"Job {job_id} scheduled with seed {actual_seed}. {stage_hint} "
                f"Status: {(job_dir / 'status.json').as_posix()}"
            )
            return {
                "ui": {
                    "text": (status,),
                    "h3_director": [
                        {
                            "job_id": job_id,
                            "status": status,
                            "status_url": f"/h3-long-video/status/{job_id}",
                        }
                    ],
                },
                "result": (job_id, status),
            }
        except Exception:
            with _ACTIVE_LOCK:
                _ACTIVE_JOB = None
            raise


class H3SingleClipDirector:
    CATEGORY = "MiniMax H3"
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("job_id", "status")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
                "reference_1": ("IMAGE",),
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "dynamicPrompts": False,
                        "default": (
                            "<女主角>从首帧姿态自然过渡到尾帧姿态。"
                            "保持参考图中的身份、脸型、五官、发型和身体比例一致。"
                            "9:16竖屏完整全身构图，摄影机不推进、不缩放，"
                            "头顶、身体和鞋底始终完整入镜。面部与嘴型稳定，"
                            "双唇自然闭合，嘴宽与参考图一致。"
                        ),
                    },
                ),
                "seconds": (
                    "FLOAT",
                    {"default": 5.0, "min": 5.0, "max": 60.0, "step": 0.5},
                ),
                "steps": ("INT", {"default": 6, "min": 1, "max": 200, "step": 1}),
                "two_pass_steps": (
                    "INT",
                    {"default": 5, "min": 1, "max": 200, "step": 1},
                ),
                "sampler": (_sampler_options(), {"default": "res_multistep"}),
                "scheduler": (_scheduler_options(), {"default": "simple"}),
                "megapixels": (
                    "FLOAT",
                    {"default": 0.2, "min": 0.05, "max": 4.0, "step": 0.01},
                ),
                "final_width": (
                    "INT",
                    {"default": 704, "min": 256, "max": 4096, "step": 8},
                ),
                "final_height": (
                    "INT",
                    {"default": 1216, "min": 256, "max": 4096, "step": 8},
                ),
                "ref_image_size": (
                    ["match", "max"],
                    {"default": "match"},
                ),
                "two_pass": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                **{
                    f"reference_{index}": ("IMAGE",)
                    for index in range(2, 10)
                },
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1125899906842623,
                        "step": 1,
                    },
                ),
                "randomize_seed": ("BOOLEAN", {"default": True}),
                "job_name": (
                    "STRING",
                    {
                        "multiline": False,
                        "dynamicPrompts": False,
                        "default": "",
                    },
                ),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")

    @classmethod
    def execute(
        cls,
        first_frame,
        last_frame,
        reference_1,
        prompt,
        seconds,
        steps,
        two_pass_steps,
        sampler,
        scheduler,
        megapixels,
        final_width,
        final_height,
        ref_image_size,
        two_pass,
        seed=0,
        randomize_seed=True,
        job_name="",
        **reference_images,
    ):
        global _ACTIVE_JOB

        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt cannot be empty")
        seconds = float(seconds)
        if seconds <= 0:
            raise ValueError("seconds must be greater than 0")
        final_width = int(final_width)
        final_height = int(final_height)
        ratio = final_width / final_height if final_height else 0
        if final_width >= final_height or abs(ratio - (9 / 16)) > 0.02:
            raise ValueError("final resolution must remain a 9:16 portrait preset")
        if float(megapixels) <= 0:
            raise ValueError("megapixels must be greater than 0")

        with _ACTIVE_LOCK:
            if _ACTIVE_JOB is not None:
                raise RuntimeError(
                    f"H3 job {_ACTIVE_JOB} is still running"
                )
            _ACTIVE_JOB = "reserved"

        try:
            job_id = _new_job_id(job_name)
            job_dir = JOB_ROOT / job_id
            job_dir.parent.mkdir(parents=True, exist_ok=True)
            job_dir.mkdir(parents=True, exist_ok=False)

            fixed_first = job_dir / "first_frame.png"
            fixed_last = job_dir / "last_frame.png"
            _save_image(first_frame, fixed_first)
            _save_image(last_frame, fixed_last)

            supplied_images = [reference_1]
            for index in range(2, 10):
                image = reference_images.get(f"reference_{index}")
                if image is not None:
                    supplied_images.append(image)

            saved_references = []
            for index, image in enumerate(supplied_images, start=1):
                output = job_dir / f"reference_{index}.png"
                _save_image(image, output)
                saved_references.append(output.as_posix())

            config = _load_base_config()
            config.update(
                {
                    "single_clip": True,
                    "fixed_first_frame": fixed_first.as_posix(),
                    "fixed_last_frame": fixed_last.as_posix(),
                    "reference_images": saved_references,
                    "segments": [
                        {
                            "seconds": seconds,
                            "two_pass": bool(two_pass),
                            "prompt": prompt,
                        }
                    ],
                    "steps": int(steps),
                    "two_pass_steps": int(two_pass_steps),
                    "sampler": sampler,
                    "scheduler": scheduler,
                    "two_pass_scheduler": scheduler,
                    "megapixels": float(megapixels),
                    "final_width": final_width,
                    "final_height": final_height,
                    "ref_image_size": ref_image_size,
                    "output_prefix": "h3_single_clip",
                    "job_name": _sanitize_job_name(job_name),
                }
            )
            actual_seed = _resolve_seed(seed, randomize_seed)
            config["base_seed"] = actual_seed

            config_path = job_dir / "config.json"
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(CONTROLLER_PATH),
                "--config",
                str(config_path),
            ]
            _write_status(
                job_dir,
                status="scheduled",
                job_id=job_id,
                config=str(config_path),
                references=saved_references,
            )
            with _ACTIVE_LOCK:
                _ACTIVE_JOB = job_id
            worker = threading.Thread(
                target=_run_worker,
                args=(command, job_dir),
                name=f"h3-single-clip-{job_id}",
                daemon=True,
            )
            worker.start()
            status = (
                f"Single clip {job_id} scheduled with seed {actual_seed}. "
                f"Status: {(job_dir / 'status.json').as_posix()}"
            )
            return {
                "ui": {
                    "text": (status,),
                    "h3_single_clip": [
                        {
                            "job_id": job_id,
                            "status": status,
                            "status_url": f"/h3-long-video/status/{job_id}",
                        }
                    ],
                },
                "result": (job_id, status),
            }
        except Exception:
            with _ACTIVE_LOCK:
                _ACTIVE_JOB = None
            raise


NODE_CLASS_MAPPINGS = {
    "H3LongVideoDirector": H3LongVideoDirector,
    "H3SingleClipDirector": H3SingleClipDirector,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "H3LongVideoDirector": "MiniMax H3 Long Video Director",
    "H3SingleClipDirector": "MiniMax H3 Single Clip (First + Last + Refs)",
}

