from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONTROLLER_PATH = ROOT / "run_h3_long_video.py"
JOB_ROOT = ROOT / "jobs"

_ACTIVE_LOCK = threading.Lock()
_ACTIVE_JOB = None


def _load_base_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _normalize_storyboard(value):
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
        if not prompt:
            raise ValueError(f"Segment {index + 1} has an empty prompt")
        try:
            seconds = float(item.get("seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Segment {index + 1} has an invalid duration") from exc
        if seconds <= 0:
            raise ValueError(f"Segment {index + 1} has an invalid duration")
        segments.append(
            {
                "seconds": seconds,
                "two_pass": bool(item.get("two_pass", False)),
                "prompt": prompt,
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
    (job_dir / "status.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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
                "force": ("BOOLEAN", {"default": False}),
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
        force,
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
            segments = _normalize_storyboard(storyboard)
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

            config = _load_base_config()
            config.update(overrides)
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
                "ui": {"text": (status,)},
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

