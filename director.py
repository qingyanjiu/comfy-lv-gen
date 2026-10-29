from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
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
JOB_ROOT = ROOT / "jobs"

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_JOB = None

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
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id or ""):
        raise ValueError("Invalid job id")
    job_dir = (JOB_ROOT / job_id).resolve()
    if job_dir.parent != JOB_ROOT.resolve():
        raise ValueError("Invalid job path")
    return job_dir


def _job_config(job_dir):
    path = job_dir / "config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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
                "url": f"/h3-long-video/media/{job_id}/segment/{index}",
            }
        )
    return segments


def _job_keyframes(job_dir, job_id):
    state_path = job_dir / ".state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    keyframes = []
    for item in sorted(
        state.get("keyframes", []),
        key=lambda value: value.get("segment", 0),
    ):
        start_frame = Path(item.get("start_frame", ""))
        end_frame = Path(item.get("end_frame", ""))
        if not start_frame.is_file() or not end_frame.is_file():
            continue
        index = int(item["segment"])
        keyframes.append(
            {
                "index": index,
                "start_url": (
                    f"/h3-long-video/media/{job_id}/keyframe/{index}/start"
                ),
                "end_url": (
                    f"/h3-long-video/media/{job_id}/keyframe/{index}/end"
                ),
            }
        )
    return keyframes


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
                "url": f"/h3-long-video/media/{job_id}/transition/{index}",
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
            segments = _job_segments(job_dir, job_id)
            keyframes = _job_keyframes(job_dir, job_id)
            transitions = _job_transitions(job_dir, job_id)
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
            return web.json_response(
                {
                    **status,
                    "job_id": job_id,
                    "segments": segments,
                    "completed_segments": len(segments),
                    "keyframes": keyframes,
                    "completed_keyframes": len(keyframes),
                    "transitions": transitions,
                    "phase": state.get("phase"),
                    "final_url": (
                        f"/h3-long-video/media/{job_id}/final"
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


def _run_worker(command, job_dir):
    global _ACTIVE_JOB
    try:
        # The outer ComfyUI prompt must finish before its child prompt is queued.
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
            _write_status(
                job_dir,
                status="completed",
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
                "sampler": (SAMPLER_OPTIONS, {"default": "res_multistep"}),
                "scheduler": (SCHEDULER_OPTIONS, {"default": "simple"}),
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
        **reference_images,
    ):
        global _ACTIVE_JOB

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

            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
            job_dir = JOB_ROOT / job_id
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

            config["steps"] = int(steps)
            config["two_pass_steps"] = int(two_pass_steps)
            config["keyframe_steps"] = int(keyframe_steps)
            config["sampler"] = sampler
            config["scheduler"] = scheduler
            config["two_pass_scheduler"] = scheduler
            config["megapixels"] = float(megapixels)
            config["final_width"] = int(final_width)
            config["final_height"] = int(final_height)
            if config["megapixels"] <= 0:
                raise ValueError("megapixels must be greater than 0")
            if config["final_width"] >= config["final_height"]:
                raise ValueError("final resolution must remain portrait")
            ratio = config["final_width"] / config["final_height"]
            if abs(ratio - (9 / 16)) > 0.02:
                raise ValueError("final resolution must keep the 9:16 portrait preset")
            config["segments"] = segments
            config["reference_images"] = saved_references
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
            status = (
                f"Job {job_id} scheduled. "
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


NODE_CLASS_MAPPINGS = {"H3LongVideoDirector": H3LongVideoDirector}
NODE_DISPLAY_NAME_MAPPINGS = {
    "H3LongVideoDirector": "MiniMax H3 Long Video Director"
}

