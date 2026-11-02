import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import requests


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
# 6: tail_guard_frames -- cached clips rendered before it contain the corrupt
# last frame, so they must not be reused on the strength of the old signature.
GRAPH_VERSION = 6

DEFAULT_KEYFRAME_REANCHOR_PROMPT = (
    "身份重锚定：严格回到参考图（Picture 1 / Picture 2）中的同一人物，"
    "脸型、五官、发型、肤色和身体比例必须与参考图完全一致，"
    "不得继承前帧累积的任何漂移；服装与场景按当前剧情延续。"
)

# The end keyframe is generated as the *last frame of a very short draft*
# (``keyframe_draft_seconds``) that starts from this segment's first frame, and
# the draft has no idea how long the real segment is. Told nothing about the
# camera, it pays for the target pose by changing the lens: the subject drifts
# closer and the framing collapses to a chest-up portrait. A *static* camera lock
# makes it worse (a fixed camera plus walking toward it must zoom in), so the
# default asks for continuity instead: keep the shot size and the full body by
# dollying with the subject. Verified A/B on a 15 s approach shot -- static lock
# gave a chest-up frame, this gives a matching full-body frame.
DEFAULT_KEYFRAME_CAMERA_PROMPT = (
    "Tracking shot: the camera moves with her and pulls back as she walks "
    "forward, so she keeps exactly the same size in the frame and stays fully "
    "visible from head to feet. The background slides past her. Never zoom "
    "into her face, never crop her feet or head."
)


def h3_frame_count(seconds, tail_guard=0):
    """Latent length to ask H3 for, in frames.

    H3's latent timeline is chunked in 17-frame blocks and must start at 5
    (valid lengths are 5, 22, 39, ...), so the requested duration is rounded
    up to the next valid length.

    ``tail_guard`` asks for that many *extra* frames beyond what will be kept.
    It exists because the last frame of a clip whose length lands exactly on a
    valid value comes back corrupted: a vertical smeared ghost, made by the
    model blending the tail of the clip with itself. Normally that frame is
    harmless because it falls past ``output_frames`` and gets trimmed, but when
    ``h3_frame_count(seconds) == output_frames(seconds)`` there is nothing to
    trim, so the ghost becomes the visible last frame -- and, worse, the frame
    that node 38 saves as the next segment's start anchor. Verified on an 8 s
    clip (192 frames, exact fit): frame 191 was a ghost, frame 190 was clean,
    and the next segment started from that ghost.
    """
    requested = max(5, math.ceil(float(seconds) * 24))
    if tail_guard:
        requested += int(tail_guard)
    while requested % 17 != 5:
        requested += 1
    return requested


def tail_guard_frames(config):
    return max(0, int(config.get("tail_guard_frames", 2) or 0))


def output_frames(seconds):
    return max(1, round(float(seconds) * 24))


def should_anchor_last_frame(config, segment):
    hold_composition = segment.get("hold_composition")
    if hold_composition is None:
        hold_composition = config.get("stabilize_full_body", False)
    return bool(hold_composition)


def should_continue_from_previous(config, segment):
    value = segment.get("continue_from_previous")
    if value is None:
        value = config.get("chain_segments", True)
    return bool(value)


def segment_specs(config):
    segments = config.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("config.json must contain a non-empty segments array")
    specs = []
    for index, segment in enumerate(segments):
        prompt = str(segment.get("prompt", "")).strip()
        end_frame_prompt = str(segment.get("end_frame_prompt", "")).strip()
        start_frame_prompt = str(segment.get("start_frame_prompt", "")).strip()
        seconds = float(segment.get("seconds", 0))
        if not prompt:
            raise RuntimeError(f"Segment {index + 1} has an empty prompt")
        if seconds <= 0:
            raise RuntimeError(f"Segment {index + 1} has an invalid duration")
        hold_composition = segment.get("hold_composition")
        if hold_composition is not None:
            hold_composition = bool(hold_composition)
        continue_from_previous = segment.get("continue_from_previous")
        if continue_from_previous is not None:
            continue_from_previous = bool(continue_from_previous)
        transition_seconds = float(
            segment.get("transition_seconds", config.get("transition_default_seconds", 1.0))
        )
        transition_prompt = str(segment.get("transition_prompt", "")).strip()
        effective_continue = (
            True
            if index == 0
            else (
                continue_from_previous
                if continue_from_previous is not None
                else bool(config.get("chain_segments", True))
            )
        )
        if transition_seconds < 0 or (
            index > 0 and not effective_continue and transition_seconds <= 0
        ):
            raise RuntimeError(f"Segment {index + 1} has an invalid transition duration")
        free_end = segment.get("free_end")
        if free_end is not None:
            free_end = bool(free_end)
        specs.append(
            {
                "seconds": seconds,
                "two_pass": bool(segment.get("two_pass", config.get("two_pass_default", False))),
                "free_end": bool(free_end),
                "prompt": prompt,
                "end_frame_prompt": end_frame_prompt or prompt,
                "start_frame_prompt": start_frame_prompt or prompt,
                "hold_composition": hold_composition,
                "continue_from_previous": continue_from_previous,
                "transition_seconds": transition_seconds,
                "transition_prompt": transition_prompt
                or (
                    "电影感平滑过渡，从上一镜头结尾自然转变到下一镜头开始；"
                    "保持人物身份、脸型、发型和身体比例一致，动作与场景连续，"
                    "无黑场、无对白、无水印。"
                ),
            }
        )
    return specs


def validate_config(config):
    transition_mode = str(config.get("transition_mode", "fade")).lower()
    if transition_mode not in {"fade", "generated", "none"}:
        raise RuntimeError("transition_mode must be fade, generated, or none")
    aspect_ratio = str(config.get("aspect_ratio", ""))
    if "9:16" not in aspect_ratio:
        raise RuntimeError("This H3 flow is locked to a 9:16 portrait aspect ratio")
    final_width = int(config.get("final_width", 0))
    final_height = int(config.get("final_height", 0))
    target_ratio = 9 / 16
    actual_ratio = final_width / final_height if final_height else 0
    if final_width >= final_height or abs(actual_ratio - target_ratio) > 0.02:
        raise RuntimeError(
            "final_width/final_height must keep the aligned 9:16 portrait preset; "
            f"got {final_width}x{final_height}"
        )
    if float(config.get("fps", 24)) != 24:
        raise RuntimeError("This H3 flow currently requires fps=24")
    if keyframe_backend(config) in QWEN_BACKENDS:
        keyframe_width, keyframe_height = keyframe_size(config)
        if keyframe_width % 32 or keyframe_height % 32:
            raise RuntimeError(
                "keyframe_width/keyframe_height must be multiples of 32 so the "
                "Qwen backends can hit the size exactly; got "
                f"{keyframe_width}x{keyframe_height}"
            )
        if keyframe_width >= keyframe_height:
            raise RuntimeError(
                "keyframe_width/keyframe_height must stay portrait; got "
                f"{keyframe_width}x{keyframe_height}"
            )
    references = config.get("reference_images")
    if not isinstance(references, list) or not references:
        raise RuntimeError("reference_images must contain at least one image")
    if len(references) > 9:
        raise RuntimeError("reference_images supports at most 9 images")
    for image in references:
        path = input_image_path(config, image)
        if not Path(path).is_file():
            raise RuntimeError(f"Reference image not found: {path}")
    opening_frame = config.get("opening_frame")
    if opening_frame and not Path(input_image_path(config, opening_frame)).is_file():
        raise RuntimeError(f"Opening frame not found: {opening_frame}")
    if config.get("single_clip"):
        for key, label in (
            ("fixed_first_frame", "Fixed first frame"),
            ("fixed_last_frame", "Fixed last frame"),
        ):
            image = config.get(key)
            if not image:
                raise RuntimeError(f"{label} is required for a single clip")
            if not Path(input_image_path(config, image)).is_file():
                raise RuntimeError(f"{label} not found: {image}")


def generation_signature(config, segment, source_prompt_id):
    ignored = {
        "comfy_url",
        "comfy_input_dir",
        "chain_input_dir",
        "comfy_output_dir",
        "final_output_dir",
        "output_prefix",
        "segments",
        # Post-processing / QA options never change the ComfyUI graph itself,
        # so they must not invalidate the generation cache. The re-anchor text
        # is already captured inside the keyframe prompt it modifies.
        "two_pass_blend_frames",
        "audio_boundary_fade",
        "static_compact",
        "static_compact_threshold",
        "static_compact_head_keep",
        "static_compact_mid_keep",
        "static_compact_tail_keep",
        "static_compact_min_seconds",
        "static_compact_mid_min_seconds",
        "static_compact_min_saving",
        "static_compact_creep_threshold",
        "static_compact_creep_factor",
        "static_compact_creep_min_seconds",
        "qa_anchor_ssim_min",
        "qa_face_check",
        "qa_face_sim_min",
        "keyframe_reanchor_every",
        "keyframe_reanchor_prompt",
        # Video-only prompt decoration: it changes the segment prompt itself,
        # which the signature already captures, so toggling it must not throw
        # away the keyframes (that would silently re-render every anchor).
        "video_camera_continuity",
        "video_camera_prompt",
    }
    global_settings = {key: value for key, value in config.items() if key not in ignored}
    payload = {
        "graph_version": GRAPH_VERSION,
        "global_settings": global_settings,
        "segment": segment,
        "source_prompt_id": source_prompt_id,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def input_image_path(config, image):
    path = Path(image)
    if not path.is_absolute():
        path = Path(config["comfy_input_dir"]) / image
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f"Image not found: {path}")
    return path.as_posix()


def build_graph(config, prompt, first_frame, last_frame, seed, seconds, two_pass):
    generated_frames = h3_frame_count(seconds, tail_guard_frames(config))
    final_frames = output_frames(seconds)
    node = {}

    def add(node_id, class_type, **inputs):
        node[str(node_id)] = {"class_type": class_type, "inputs": inputs}
        return str(node_id)

    if first_frame:
        add(
            "1",
            "VHS_LoadImagePath",
            image=input_image_path(config, first_frame),
            custom_width=0,
            custom_height=0,
        )
    if last_frame:
        add(
            "11",
            "VHS_LoadImagePath",
            image=input_image_path(config, last_frame),
            custom_width=0,
            custom_height=0,
        )
    for i, image in enumerate(config["reference_images"]):
        add(
            str(2 + i),
            "VHS_LoadImagePath",
            image=input_image_path(config, image),
            custom_width=0,
            custom_height=0,
        )

    add(
        "20",
        "CLIPLoader",
        clip_name=config["clip_name"],
        type="minimax",
        device="default",
    )
    add("21", "VAELoader", vae_name=config["video_vae_name"])
    add("22", "VAELoader", vae_name=config["audio_vae_name"])
    add("23", "UNETLoader", unet_name=config["unet_name"], weight_dtype="default")
    add(
        "24",
        "LoraLoaderModelOnly",
        model=["23", 0],
        lora_name=config["lora_name"],
        strength_model=config["lora_strength"],
    )
    add("25", "ModelAttentionBackend", model=["24", 0], attention="comfy kitchen attention")
    add("26", "MiniMaxH3SigmaShift", model=["25", 0], shift_video=12, shift_audio=3)
    add(
        "27",
        "ResolutionSelector",
        aspect_ratio=config["aspect_ratio"],
        megapixels=config["megapixels"],
        multiple=32,
    )
    add("28", "PrimitiveStringMultiline", value=prompt)

    h3_inputs = {
        "clip": ["20", 0],
        "vae": ["21", 0],
        "prompt": ["28", 0],
        "width": ["27", 0],
        "height": ["27", 1],
        "length": generated_frames,
        "ref_image_size": config.get("ref_image_size", "match"),
    }
    if first_frame:
        h3_inputs["first_frame"] = ["1", 0]
    if last_frame:
        h3_inputs["last_frame"] = ["11", 0]
    for i in range(len(config["reference_images"])):
        h3_inputs[f"ref_images.ref_image_{i}"] = [str(2 + i), 0]
    add("30", "VRGDG_MiniMaxH3ImageReferenceToVideo", **h3_inputs)

    add("31", "KSamplerSelect", sampler_name=config["sampler"])
    add("32", "RandomNoise", noise_seed=seed)
    add("33", "BasicGuider", model=["26", 0], conditioning=["30", 0])
    add(
        "34",
        "BasicScheduler",
        model=["26", 0],
        scheduler=config["scheduler"],
        steps=config["steps"],
        denoise=1.0,
    )
    add(
        "35",
        "SamplerCustomAdvanced",
        noise=["32", 0],
        guider=["33", 0],
        sampler=["31", 0],
        sigmas=["34", 0],
        latent_image=["30", 1],
    )
    sample_source = "35"
    if two_pass:
        add("48", "LTXVSeparateAVLatent", av_latent=["35", 0])
        add(
            "43",
            "MinimaxH3LatentUpscaler3D",
            latent=["48", 0],
            model_name=config["latent_upscaler_name"],
            mode="scale by multiplier",
            align=32,
            # H3 two-pass temporal chunking is required by the default pipeline.
            enable_temporal_chunking=True,
            force_unload=True,
            device="cuda",
            precision="fp16",
        )
        node["43"]["inputs"]["mode.scale"] = float(config.get("two_pass_scale", 2.0))
        add(
            "49",
            "LTXVConcatAVLatent",
            video_latent=["43", 0],
            audio_latent=["48", 1],
        )
        add("44", "RandomNoise", noise_seed=seed + 1000000)
        add(
            "46",
            "BasicScheduler",
            model=["26", 0],
            scheduler=config.get("two_pass_scheduler", "simple"),
            steps=int(config.get("two_pass_steps", 5)),
            denoise=float(config.get("two_pass_denoise", 0.2)),
        )
        add(
            "50",
            "MMH3TemporalSplitParams",
            chunk_length=153,
            temporal_overlap=17,
            anchor_strength=0.99,
        )
        add(
            "47",
            "MMH3UltimateUpscale",
            model=["26", 0],
            conditioning=["30", 0],
            latent=["49", 0],
            noise=["44", 0],
            sampler=["31", 0],
            sigmas=["46", 0],
            temporal_split_param=["50", 0],
            cfg=1.0,
        )
        sample_source = "47"

    add("36", "VAEDecode", samples=[sample_source, 0], vae=["21", 0])
    add("37", "LTXVAudioVAEDecode", samples=[sample_source, 0], audio_vae=["22", 0])

    add("38", "ImageFromBatch", image=["36", 0], batch_index=final_frames - 1, length=1)
    add(
        "39",
        "SaveImage",
        images=["38", 0],
        filename_prefix=f"{config['output_prefix']}/last_{seed}",
    )
    add("40", "ImageFromBatch", image=["36", 0], batch_index=0, length=final_frames)
    add(
        "41",
        "TrimAudioDuration",
        audio=["37", 0],
        start_index=0.0,
        duration=float(seconds),
    )
    add(
        "42",
        "VHS_VideoCombine",
        images=["40", 0],
        audio=["41", 0],
        frame_rate=24,
        loop_count=0,
        filename_prefix=f"{config['output_prefix']}/segment_{seed}",
        format="video/h264-mp4",
        pingpong=False,
        save_output=True,
    )
    return node


def build_keyframe_graph(config, prompt, first_frame, seed, seconds):
    generated_frames = h3_frame_count(seconds, tail_guard_frames(config))
    final_frames = output_frames(seconds)
    node = {}

    def add(node_id, class_type, **inputs):
        node[str(node_id)] = {"class_type": class_type, "inputs": inputs}
        return str(node_id)

    if first_frame:
        add(
            "1",
            "VHS_LoadImagePath",
            image=input_image_path(config, first_frame),
            custom_width=0,
            custom_height=0,
        )
    for i, image in enumerate(config["reference_images"]):
        add(
            str(2 + i),
            "VHS_LoadImagePath",
            image=input_image_path(config, image),
            custom_width=0,
            custom_height=0,
        )

    add(
        "20",
        "CLIPLoader",
        clip_name=config["clip_name"],
        type="minimax",
        device="default",
    )
    add("21", "VAELoader", vae_name=config["video_vae_name"])
    add("23", "UNETLoader", unet_name=config["unet_name"], weight_dtype="default")
    add(
        "24",
        "LoraLoaderModelOnly",
        model=["23", 0],
        lora_name=config["lora_name"],
        strength_model=config["lora_strength"],
    )
    add("25", "ModelAttentionBackend", model=["24", 0], attention="comfy kitchen attention")
    add("26", "MiniMaxH3SigmaShift", model=["25", 0], shift_video=12, shift_audio=3)
    add(
        "27",
        "ResolutionSelector",
        aspect_ratio=config["aspect_ratio"],
        megapixels=float(config.get("keyframe_megapixels", config["megapixels"])),
        multiple=32,
    )
    add("28", "PrimitiveStringMultiline", value=prompt)

    h3_inputs = {
        "clip": ["20", 0],
        "vae": ["21", 0],
        "prompt": ["28", 0],
        "width": ["27", 0],
        "height": ["27", 1],
        "length": generated_frames,
        "ref_image_size": config.get(
            "keyframe_ref_image_size",
            config.get("ref_image_size", "match"),
        ),
    }
    if first_frame:
        h3_inputs["first_frame"] = ["1", 0]
    for i in range(len(config["reference_images"])):
        h3_inputs[f"ref_images.ref_image_{i}"] = [str(2 + i), 0]
    add("30", "VRGDG_MiniMaxH3ImageReferenceToVideo", **h3_inputs)

    add("31", "KSamplerSelect", sampler_name=config["sampler"])
    add("32", "RandomNoise", noise_seed=seed)
    add("33", "BasicGuider", model=["26", 0], conditioning=["30", 0])
    add(
        "34",
        "BasicScheduler",
        model=["26", 0],
        scheduler=config["scheduler"],
        steps=int(config.get("keyframe_steps", config["steps"])),
        denoise=1.0,
    )
    add(
        "35",
        "SamplerCustomAdvanced",
        noise=["32", 0],
        guider=["33", 0],
        sampler=["31", 0],
        sigmas=["34", 0],
        latent_image=["30", 1],
    )
    add("36", "VAEDecode", samples=["35", 0], vae=["21", 0])
    add("37", "ImageFromBatch", image=["36", 0], batch_index=0, length=1)
    add(
        "38",
        "SaveImage",
        images=["37", 0],
        filename_prefix=f"{config['output_prefix']}/keyframes/first_{seed}",
    )
    add(
        "39",
        "ImageFromBatch",
        image=["36", 0],
        batch_index=final_frames - 1,
        length=1,
    )
    add(
        "40",
        "SaveImage",
        images=["39", 0],
        filename_prefix=f"{config['output_prefix']}/keyframes/last_{seed}",
    )
    return node


# ---------------------------------------------------------------------------
# Qwen keyframe backends
# ---------------------------------------------------------------------------
# ``keyframe_backend`` picks who draws the anchors. "h3" runs H3's own draft
# graph above; the two Qwen values run the edit models here, in the runner, so
# a Director job never has to be opened by hand in ComfyUI.
BACKEND_H3 = "h3"
BACKEND_QWEN2511 = "qwen2511"
BACKEND_QWEN21 = "qwen21"
BACKEND_MANUAL = "manual"
QWEN_BACKENDS = (BACKEND_QWEN2511, BACKEND_QWEN21)

# Keyframe resolution is fixed, on purpose. 608x1056 is exactly what
# ResolutionSelector(9:16, 0.6MP, multiple=32) returns for H3's own draft, so a
# Qwen anchor and an H3 anchor are interchangeable in the same video stage. Both
# numbers are multiples of 32, which is what lets us hand the exact size to
# ImageScale + VAEEncode instead of letting FluxKontextImageScale pick "about
# 1MP" for us -- that node turns a 720x1280 input into 752x1392 (ratio 0.540)
# and H3 then refuses it with "final resolution must remain a 9:16 preset".
DEFAULT_KEYFRAME_WIDTH = 608
DEFAULT_KEYFRAME_HEIGHT = 1056

# Qwen 关键帧的像素预算。2.1 的出图规则是「图1 比例 × 约 1.05MP，对齐 32」，
# 500x625 的参考图因此出 928x1152（skill 里 pipeline 的实测值）。
DEFAULT_QWEN_KEYFRAME_MEGAPIXELS = 1.05

QWEN_KEYFRAME_PRESETS = {
    BACKEND_QWEN2511: {
        "unet": "qwen_image_edit_2511_int8_convrot.safetensors",
        "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "vae": "qwen_image_vae.safetensors",
        # 8-step over 4-step, measured on the same canvas and seed: the 4-step
        # output has waxy skin, mushy fingers and a soft face; 8-step costs
        # ~24 s more per frame and fixes all of it. This backend exists to be
        # the quality option (2.1 is the fast one), so don't sand it down.
        "lora": "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors",
        "steps": 8,
        "cfg": 1.0,
        "shift": 3.1,
        "prefix": "qwen2511_edit",
    },
    BACKEND_QWEN21: {
        # ray 微调版，不是官方 int8。同一张参考图、同一句提示词，官方版会把
        # 头转向 3/4 侧面（人像的"看镜头"变成"看旁边"），ray 版保持正脸。
        # 这条是 2026-11-02 定稿时确认的：关键帧角度是硬要求，官方版一票否决。
        "unet": r"Qwen\Qwen21-rayQwen21_v1_int8_convrot.safetensors",
        "clip": "qwen3vl_8b_int8_convrot.safetensors",
        "vae": "qwen_image_2.1_vae_bf16.safetensors",
        # 不挂 8 步 LoRA：ray 版在 20 步下细节就够，挂 LoRA 反而把皮肤磨平。
        # 8 步 + LoRA 是官方 int8 的省时配方，这里用不上。
        "lora": "",
        "steps": 20,
        "cfg": 1.0,
        "shift": None,
        "prefix": "qwen21_edit",
    },
}


def keyframe_backend(config):
    value = str(config.get("keyframe_backend") or BACKEND_H3).strip().lower()
    return value or BACKEND_H3


def keyframe_size(config):
    """H3 自己出图时的尺寸（固定）。

    Qwen 后端不再用这个，见 ``qwen_keyframe_size``。
    """
    width = int(config.get("keyframe_width") or DEFAULT_KEYFRAME_WIDTH)
    height = int(config.get("keyframe_height") or DEFAULT_KEYFRAME_HEIGHT)
    return width, height


def qwen_keyframe_size(config, canvas=None):
    """Qwen 关键帧的生成尺寸：跟着图1（或画布）的比例走，约 1.05MP，32 对齐。

    以前这里和 H3 共用固定的 608x1056（比例 0.5758）。4:5 的参考图进
    ``ImageScale(crop="center")`` 时，要被塞进这个更窄的框里——左右各砍掉约
    28%，人物被推近、桌面被裁掉，看起来就是"人变大、桌子没了"。

    现在改成按图1 的实际比例算（和 skill 里 pipeline 的出图规则一致），整张
    参考图进模型，画幅差交给后面的扩图补。
    """
    target = None
    if config.get("final_width") and config.get("final_height"):
        target = target_frame_size(config)
    source = canvas
    if not source:
        references = config.get("reference_images") or []
        source = references[0] if references else None
    if source and Path(str(source)).is_file():
        try:
            size = image_size(str(source))
        except Exception:
            # 图坏了/不是真图（单测里的占位文件）就退回目标尺寸，
            # 不能因为一张打不开的参考图让整条关键帧阶段挂掉。
            size = None
        if size:
            # 已经是目标画幅就别绕圈：链式派生的尾帧画布就是扩展后的 9:16，
            # 按 1.05MP 重算会得出 768x1376（比例 0.558），和目标差 1.3%，
            # 白多跑一次扩图。1% 的容差是为了容下 32 对齐带来的零头
            # （1080x1920 会被 2.1 对齐成 1088x1920）。
            if target and aspect_gap(size, target) <= 0.01:
                return target
            ratio = size[0] / size[1] if size[1] else 9 / 16
        elif target:
            return target
        else:
            ratio = 9 / 16
    elif target:
        return target
    else:
        ratio = 9 / 16
    megapixels = float(
        config.get("keyframe_qwen_megapixels") or DEFAULT_QWEN_KEYFRAME_MEGAPIXELS
    )
    width = _align32(math.sqrt(megapixels * 1_000_000 * ratio))
    height = _align32(width / ratio)
    return width, height


# ---------------------------------------------------------------------------
# 关键帧 → 成片分辨率：对齐 / 扩图
#
# 背景：关键帧一直是固定 608x1056（比例 0.5758），成片却是 832x1472（0.5652），
# 差 2.4%。以前靠 ``normalize_video_resolution`` 的 ``scale=W:H`` 硬拉伸圆过去，
# 相当于整片被横向压扁 2.4%。现在改成两头都按用户选的分辨率走：
#
#   1. 关键帧出图后立刻对齐到 final_width x final_height，H3 拿到的首尾帧和成片
#      同尺寸同比例，剪辑阶段不再需要拉伸；
#   2. 比例对不上时，``keyframe_align`` 决定怎么补：
#        crop     —— 等比放大后中心裁切（默认，零额外耗时）
#        outpaint —— 铺灰底走 2.1 扩图 LoRA，把缺的那一截补成真实场景（约 +35s/帧）
#        off      —— 保持旧行为（关键帧维持生成尺寸，交给视频阶段缩放）
#
# 手动替换的帧也走同一套归一，所以用户可以随便丢一张图进来。
KEYFRAME_ALIGN_MODES = ("crop", "outpaint", "off")

# 和 skill 里 ``outpaint_916()`` 用的触发词一致：只描述"把灰区补成场景"，不描述内容。
DEFAULT_OUTPAINT_PROMPT = (
    "Outpaint the image: replace the solid gray areas with a seamless continuation "
    "of the scene, keeping the existing picture unchanged."
)

# 扩图专用预设：官方 int8（ray 微调版在扩图上会乱造角度）+ 扩图 LoRA，20 步。
# 2.1 的 Canvas Stable Diffusion 编号沿用 build_qwen_keyframe_graph 的 2.1 分支。
OUTPAINT_PRESET = {
    "unet": "qwen_image_2.1_int8_convrot.safetensors",
    "clip": "qwen3vl_8b_int8_convrot.safetensors",
    "vae": "qwen_image_2.1_vae_bf16.safetensors",
    "lora": r"Qwen-功能2.1\Qwen21-图像扩展qwen-image-2.1-outpaint-v2.safetensors",
    "steps": 20,
    "cfg": 1.0,
    "shift": None,
    "prefix": "qwen21_outpaint",
}

DEFAULT_OUTPAINT_MEGAPIXELS = 0.75
# 扩图画布的像素上限。8GB 卡实测 864x1536（1.33MP）能跑，再大就 OOM。
MAX_OUTPAINT_MEGAPIXELS = 1.35


def target_frame_size(config):
    """成片分辨率，关键帧要对齐到它。"""
    width = int(config.get("final_width") or 0)
    height = int(config.get("final_height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("final_width/final_height must be set before aligning keyframes")
    return width, height


def keyframe_align_mode(config):
    value = str(config.get("keyframe_align") or "crop").strip().lower()
    return value if value in KEYFRAME_ALIGN_MODES else "crop"


def image_size(path):
    from PIL import Image

    with Image.open(path) as image:
        return int(image.width), int(image.height)


def exact_frame(size, target):
    return int(size[0]) == int(target[0]) and int(size[1]) == int(target[1])


def aspect_gap(size, target):
    """两个尺寸的比例相对差，0.01 = 差 1%。"""
    if not size[1] or not target[1]:
        return 0.0
    return abs((size[0] / size[1]) - (target[0] / target[1])) / (target[0] / target[1])


def cover_geometry(size, target):
    """等比放大到能盖住目标框后的尺寸（中心裁切用）。"""
    scale = max(target[0] / size[0], target[1] / size[1])
    return max(target[0], round(size[0] * scale)), max(target[1], round(size[1] * scale))


def _align32(value):
    return max(32, int(round(value / 32.0)) * 32)


def outpaint_canvas_geometry(size, target, megapixels=None):
    """算出扩图灰底画布：目标比例、约 megapixels 像素预算、32 对齐。

    返回 ``(canvas_w, canvas_h, paste_x, paste_y, scaled_w, scaled_h)``。
    源图等比缩到刚好有一边贴满画布，另一边留灰给模型补。

    宽高都要 32 对齐，单独按比例算会让比例再飘 1% 左右（640x1120 = 0.5714，
    目标是 0.5652），所以这里在预算附近搜一遍，挑「比例误差最小」的那组。
    """
    ratio = target[0] / target[1]
    mp = float(megapixels or DEFAULT_OUTPAINT_MEGAPIXELS)
    # 画布比目标还小的话，扩完还得再放大一次 = 先缩后放，细节白丢。
    # 目标 832x1472 = 1.22MP，4:5 出的 928x1152 正好按原尺寸铺进去，不用缩。
    mp = max(mp, target[0] * target[1] / 1_000_000)
    # 8GB 卡上限：864x1536（1.33MP）是实测跑得动的最大画布，再大会 OOM。
    mp = min(mp, MAX_OUTPAINT_MEGAPIXELS)
    ideal_h = math.sqrt(mp * 1_000_000 / ratio)
    best = None
    for step in range(-6, 7):
        height = _align32(ideal_h + step * 32)
        width = _align32(height * ratio)
        error = abs(width / height - ratio) / ratio
        budget = abs(width * height - mp * 1_000_000) / (mp * 1_000_000)
        # 比例误差权重远高于像素预算：扩图的目的是"按目标比例补齐"，
        # 为了凑像素数把比例搞歪 1%，后面还得再裁一刀，白扩。
        score = error * 20 + budget * 0.5
        if best is None or score < best[0]:
            best = (score, width, height)
    _, canvas_w, canvas_h = best
    scale = min(canvas_w / size[0], canvas_h / size[1])
    scaled_w = max(1, int(round(size[0] * scale)))
    scaled_h = max(1, int(round(size[1] * scale)))
    paste_x = (canvas_w - scaled_w) // 2
    paste_y = (canvas_h - scaled_h) // 2
    return canvas_w, canvas_h, paste_x, paste_y, scaled_w, scaled_h


def _resize_filter(src, dst):
    """放大用 bicubic（lanczos 放大会有振铃，观感像"锐化过度"），缩小用 lanczos。"""
    from PIL import Image

    return Image.BICUBIC if dst[0] * dst[1] > src[0] * src[1] else Image.LANCZOS


def _center_crop_resize(path, target):
    """等比放大后中心裁切到目标比例，再缩放到精确尺寸。"""
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        size = (image.width, image.height)
        cover = cover_geometry(size, target)
        if cover != size:
            image = image.resize(cover, _resize_filter(size, cover))
        left = (cover[0] - target[0]) // 2
        top = (cover[1] - target[1]) // 2
        image = image.crop((left, top, left + target[0], top + target[1]))
        image.save(path)
    return path


def _resize_to(path, target):
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        size = (image.width, image.height)
        if exact_frame(size, target):
            return path
        image = image.resize(target, _resize_filter(size, target))
        image.save(path)
    return path


def _gray_canvas(src_path, target, megapixels=None):
    """把源图贴到目标比例的灰底画布上，返回 (画布路径, 画布尺寸)。

    画布写在 ``comfy_input_dir`` 里，名字带随机后缀，避免并发跑任务时互相覆盖。
    """
    from PIL import Image

    with Image.open(src_path) as image:
        image = image.convert("RGB")
        size = (image.width, image.height)
        canvas_w, canvas_h, px, py, sw, sh = outpaint_canvas_geometry(size, target, megapixels)
        if (sw, sh) != size:
            image = image.resize((sw, sh), _resize_filter(size, (sw, sh)))
        canvas = Image.new("RGB", (canvas_w, canvas_h), (127, 127, 127))
        canvas.paste(image, (px, py))
        return canvas, (canvas_w, canvas_h)


def _save_canvas(config, canvas, label):
    directory = Path(config["comfy_input_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    name = f"h3_outpaint_{uuid.uuid4().hex[:8]}.png"
    target = directory / name
    canvas.save(target)
    return target.as_posix()


def run_outpaint_pass(config, src_path, target, seed, label=None):
    """把 src_path 扩成 target 比例，返回扩图后的临时图路径（不覆盖原图）。"""
    canvas, (canvas_w, canvas_h) = _gray_canvas(src_path, target)
    canvas_path = _save_canvas(config, canvas, label)
    prompt = str(config.get("keyframe_outpaint_prompt") or DEFAULT_OUTPAINT_PROMPT)
    preset = dict(OUTPAINT_PRESET)
    if config.get("keyframe_outpaint_lora") is not None:
        preset["lora"] = str(config.get("keyframe_outpaint_lora") or "")
    if config.get("keyframe_outpaint_unet"):
        preset["unet"] = str(config["keyframe_outpaint_unet"])
    preset["steps"] = int(config.get("keyframe_outpaint_steps") or preset["steps"])
    graph = build_qwen_keyframe_graph(
        config,
        BACKEND_QWEN21,
        prompt,
        canvas_path,
        seed,
        preset=preset,
        size=(canvas_w, canvas_h),
    )
    prompt_id, entry = submit_graph(config, graph, label or f"Outpaint {seed}")
    images = (entry.get("outputs") or {}).get("32", {}).get("images") or []
    if not images:
        raise RuntimeError("Outpaint pass produced no image")
    return str(resolve_output(config, images[0]))


def align_frame_to_target(config, path, seed=0, label=None):
    """把一帧关键帧对齐到成片分辨率；返回（可能被覆盖过的）路径。

    ``keyframe_align = off`` 时原样返回，保持「关键帧尺寸随便、视频阶段缩放」的旧行为。
    读不了图 / 配置不全时也只打日志并返回原路径——对齐是"锦上添花"，
    不能因为它把整条任务打断。
    """
    mode = keyframe_align_mode(config)
    if mode == "off":
        return path
    try:
        target = target_frame_size(config)
        size = image_size(path)
        if exact_frame(size, target):
            return path
        gap = aspect_gap(size, target)
        # 1% 以内直接 cover+crop 到目标尺寸（等比放大再中心裁，不是硬拉伸），
        # 裁掉的那几像素肉眼看不出来，不值得为它跑一次扩图。
        # 阈值取 1% 是因为目标分辨率不一定是 32 的倍数：选 1080x1920 时
        # 2.1 会按 32 对齐成 1088x1920，差 0.74%，卡在 0.5% 就会每帧白扩一次（+60s）。
        if mode == "outpaint" and gap > 0.01:
            try:
                source = run_outpaint_pass(config, path, target, seed, label)
                return _resize_to(_center_crop_resize(source, target), target)
            except Exception as exc:
                print(
                    f"[H3] 扩图失败，退回中心裁切（{type(exc).__name__}: {exc}）",
                    flush=True,
                )
        return _resize_to(_center_crop_resize(path, target), target)
    except Exception as exc:
        print(
            f"[H3] 关键帧对齐跳过（{type(exc).__name__}: {exc}）：{path}",
            flush=True,
        )
        return path


# Every Qwen anchor carries this tail. Both edit models read the identity
# references as "people in the picture", and without an explicit headcount they
# happily render the same girl two or three times (verified on the very first
# 2.1 canvas test: image_2 + image_3 became two extra classmates). The manual
# 2511 workflow template already says this; the runner now says it too.
DEFAULT_QWEN_KEYFRAME_SUFFIX = (
    "画面中只有同一个人物，不新增人物、不复制人物，背景里的路人和行人保持远景虚化。"
    "真实摄影，自然皮肤纹理，无文字水印。"
)

# 三条全局锁，和 skill 的 Q21_FIRST_TMPL / Q21_LAST_TMPL 用同一套措辞。
# storyboard 里的一句话很难同时压住头发走向、人物大小和色温，模型每次总会
# 在其中一项上漂：头发散开、人物被推近、白背心发黄或整体偏冷。所以单独写成
# 固定尾巴（2026-11-02 定稿配方的一部分）。
DEFAULT_KEYFRAME_LOCKS_PROMPT = (
    "服装一件都不许改：款式、领口形状、肩带宽度、衣长和颜色都与参考图完全一致，"
    "不要换款式、不要改成露脐装或细吊带、不要增减衣物；"
    "头发保持原状：发长、分缝、发丝走向和垂落位置都和参考图一致，"
    "不要让头发飘动、散开、变形或改变遮挡关系；"
    "同一机位同一景别，人物在画面中的大小和位置与参考图保持一致，不靠近也不远离镜头；"
    "背景是干净的浅灰色墙面，柔和的室内自然光从左前方照来，色温中性偏暖、光线均匀没有硬阴影，"
    "白色衣物保持纯白不发黄，肤色自然不偏红不偏绿，整体低对比、不偏冷蓝也不偏橙黄。"
)


def qwen_keyframe_prompt(config, prompt):
    suffix = str(
        config.get("keyframe_qwen_prompt_suffix", DEFAULT_QWEN_KEYFRAME_SUFFIX)
    ).strip()
    if suffix and suffix not in prompt:
        prompt = f"{prompt}\n\n{suffix}"
    if bool(config.get("keyframe_locks", True)):
        locks = str(
            config.get("keyframe_locks_prompt") or DEFAULT_KEYFRAME_LOCKS_PROMPT
        ).strip()
        if locks and locks not in prompt:
            prompt = f"{prompt}\n\n{locks}"
    return prompt


def qwen_keyframe_preset(config, backend):
    if backend not in QWEN_KEYFRAME_PRESETS:
        raise RuntimeError(f"Unknown keyframe backend: {backend}")
    preset = dict(QWEN_KEYFRAME_PRESETS[backend])
    # 换 LoRA 就等于换加速档：蒸馏 LoRA 只在它自己的步数上成立（2511 Lightning
    # 4 步 / 2.1 8 步），所以 steps 跟 LoRA 一起覆盖，避免「4 步 + 8 步 LoRA」
    # 这种既糊又不像的组合悄悄跑完一整条任务。``keyframe_qwen_lora: "none"``
    # 表示不用任何 LoRA、走原生步数（此时 keyframe_qwen_steps 必须一起给，
    # 否则会拿蒸馏档的 8 步去跑原生权重，出图必然又糊又花）。
    lora = config.get("keyframe_qwen_lora")
    if lora is not None:
        text = str(lora).strip()
        preset["lora"] = "" if text.lower() in {"", "none", "no", "null"} else text
    preset["steps"] = int(config.get("keyframe_qwen_steps") or preset["steps"])
    preset["cfg"] = float(
        config.get("keyframe_qwen_cfg")
        if config.get("keyframe_qwen_cfg") is not None
        else preset["cfg"]
    )
    return preset


def build_qwen_keyframe_graph(config, backend, prompt, canvas, seed, preset=None, size=None):
    """Draw one anchor frame with a Qwen edit model, at a fixed resolution.

    ``canvas`` is the frame this one has to grow out of:

    * ``None`` -- the segment has no predecessor. An empty latent at
      ``keyframe_width x keyframe_height`` builds the shot natively (this is the
      "empty latent instead of VAEEncode(image1)" trick from
      ``docs/关键帧生成.md``), with the reference photos feeding identity.
    * a path -- the previous anchor is used as image 1 *and* as the canvas:
      scaled to the fixed size, VAEEncoded, and sampled at that size, so framing,
      camera height and subject scale are locked and only the action changes.

    Only one image comes out of an edit sampler, which is why the caller treats
    the result as either the start *or* the end anchor of a segment.
    """
    if backend not in QWEN_BACKENDS:
        raise RuntimeError(f"Unknown keyframe backend: {backend}")
    # ``preset`` / ``size`` 让扩图复用这一套图：同一个 2.1 编码器 + VAEEncode 路径，
    # 只换 UNET/LoRA 和画布尺寸，避免再维护第二份完全平行的建图代码。
    preset = preset or qwen_keyframe_preset(config, backend)
    if size:
        width, height = size
    elif backend == BACKEND_QWEN21:
        # 跟着图1/画布的比例出图，别把 4:5 的参考图裁成 9:16 的窄条。
        width, height = qwen_keyframe_size(config, canvas)
    else:
        width, height = keyframe_size(config)
    references = [str(item) for item in config.get("reference_images") or []]
    # Measured on 2.1 with the same canvas and seed: canvas alone renders
    # one person; canvas + either one or two identity references renders
    # TWO people -- the model treats "reference photo of the same person"
    # as a second subject to paint, and the no-extra-people prompt suffix
    # cannot stop it. The canvas already carries identity, clothing and
    # framing, so edit passes (derived frames) never take references;
    # only start frames (canvas=None) feed them. This also keeps 2511
    # within its hard 3-slot limit instead of silently dropping inputs.
    #
    # ``keyframe_qwen_canvas_refs`` flips that rule for the identity-swap
    # workflow: a pose/angle plate drawn by another model (2511 can turn the
    # head, 2.1 cannot) is used as the canvas while the original front photo
    # goes in as an identity reference, so 2.1 repaints the face onto the
    # plate. Only turn it on when the canvas shows a *different-looking*
    # person -- feeding the same person twice is what paints a twin.
    if canvas and not str(config.get("keyframe_qwen_canvas_refs") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        references = []
    node = {}

    def add(node_id, class_type, **inputs):
        node[str(node_id)] = {"class_type": class_type, "inputs": inputs}
        return str(node_id)

    image_slots = []
    if canvas:
        image_slots.append(
            add(
                "1",
                "VHS_LoadImagePath",
                image=input_image_path(config, canvas),
                custom_width=0,
                custom_height=0,
            )
        )
    for index, image in enumerate(references):
        image_slots.append(
            add(
                str(2 + index),
                "VHS_LoadImagePath",
                image=input_image_path(config, image),
                custom_width=0,
                custom_height=0,
            )
        )
    if not image_slots:
        raise RuntimeError("Qwen keyframes need at least one reference image")

    if backend == BACKEND_QWEN2511:
        # 20 -> 21 -> 22 -> 23: 2511 int8 + Lightning 4 steps. AuraFlow shift and
        # CFGNorm mirror the manual workflow; cfg is 1.0 so the negative encoder
        # is only there to satisfy KSampler.
        add("20", "UNETLoader", unet_name=preset["unet"], weight_dtype="default")
        add(
            "21",
            "ModelSamplingAuraFlow",
            model=["20", 0],
            shift=float(preset["shift"]),
        )
        add("22", "CFGNorm", model=["21", 0], strength=1.0, pre_cfg=False)
        # 没有 LoRA 时跳过这个节点：原生权重要靠原生步数跑，接了空 LoRA 名
        # 会被 ComfyUI 以 value_not_in_list 拒掉。
        model_src = "22"
        if preset.get("lora"):
            model_src = add(
                "23",
                "LoraLoaderModelOnly",
                model=["22", 0],
                lora_name=preset["lora"],
                strength_model=1.0,
            )
        add(
            "24",
            "CLIPLoader",
            clip_name=preset["clip"],
            type="qwen_image",
            device="default",
        )
        add("25", "VAELoader", vae_name=preset["vae"])
        if canvas:
            scaled = add(
                "26",
                "ImageScale",
                image=["1", 0],
                upscale_method="lanczos",
                width=width,
                height=height,
                crop="center",
            )
            latent = add("27", "VAEEncode", pixels=[scaled, 0], vae=["25", 0])
        else:
            latent = add(
                "27",
                "EmptySD3LatentImage",
                width=width,
                height=height,
                batch_size=1,
            )
        positive = add(
            "28",
            "TextEncodeQwenImageEditPlus",
            clip=["24", 0],
            vae=["25", 0],
            prompt=prompt,
            **{
                f"image{slot + 1}": [node_id, 0]
                for slot, node_id in enumerate(image_slots[:3])
            },
        )
        negative = add(
            "29",
            "TextEncodeQwenImageEditPlus",
            clip=["24", 0],
            vae=["25", 0],
            prompt="",
        )
        model = [model_src, 0]
        vae = ["25", 0]
        sampler_inputs = {
            "positive": [positive, 0],
            "negative": [negative, 0],
            "latent_image": [latent, 0],
        }
    else:
        add(
            "20",
            "CLIPLoader",
            clip_name=preset["clip"],
            type="qwen_image",
            device="default",
        )
        add("21", "VAELoader", vae_name=preset["vae"])
        add("22", "UNETLoader", unet_name=preset["unet"], weight_dtype="default")
        model_src = "22"
        if preset.get("lora"):
            model_src = add(
                "23",
                "LoraLoaderModelOnly",
                model=["22", 0],
                lora_name=preset["lora"],
                strength_model=1.0,
            )
        add("24", "QwenImage21Cache", model=[model_src, 0], device="auto", dtype="default")
        # 2.1 builds its own latent inside the encoder, sized from image_1, so
        # image_1 must already be the exact target size and ``resolution`` has to
        # stay 0 -- any positive value re-rounds image_1 to a ~1MP box (a 9:16
        # canvas becomes 768x1376, ratio 0.558) and the size then drifts with the
        # aspect of whatever the previous frame was. Without a canvas the first
        # reference photo plays image_1, so it gets the same fixed-size crop;
        # the remaining references only need a cheap pixel budget (they are pure
        # identity input, the latent never comes from them).
        encoder_slots = []
        first = add(
            "26",
            "ImageScale",
            image=[image_slots[0], 0],
            upscale_method="lanczos",
            width=width,
            height=height,
            crop="center",
        )
        # ``keyframe_qwen_canvas_blur_mp`` (megapixels, 0 = off) degrades the
        # canvas before it hits the encoder: downscale to that budget, then
        # scale back up. Measured in the identity-swap test (2511 angle plate
        # + front reference): with a crisp canvas 2.1 treats the reference as
        # decoration and repaints the plate's face almost unchanged; with a
        # ~0.12MP mushed canvas it has to re-imagine the details, and the
        # reference actually steers the face (eyes/lips/hair drift toward it)
        # while pose, angle and framing still follow the canvas. Only use it
        # for face-swap passes -- a derived frame that merely continues the
        # action wants the crisp canvas so the previous frame's detail
        # survives.
        blur_mp = float(config.get("keyframe_qwen_canvas_blur_mp") or 0)
        if blur_mp > 0:
            mushed = add(
                "26b",
                "ImageScaleToTotalPixels",
                image=[first, 0],
                upscale_method="lanczos",
                megapixels=blur_mp,
                resolution_steps=32,
            )
            first = add(
                "26c",
                "ImageScale",
                image=[mushed, 0],
                upscale_method="lanczos",
                width=width,
                height=height,
                crop="center",
            )
        encoder_slots.append(first)
        for index, node_id in enumerate(image_slots[1:]):
            if len(encoder_slots) >= 16:
                break
            encoder_slots.append(
                add(
                    str(40 + index),
                    "ImageScaleToTotalPixels",
                    image=[node_id, 0],
                    upscale_method="lanczos",
                    megapixels=0.4,
                    resolution_steps=32,
                )
            )
        encoder_images = {
            f"images.image_{slot + 1}": [node_id, 0]
            for slot, node_id in enumerate(encoder_slots)
        }
        positive = add(
            "28",
            "TextEncodeQwenImage21",
            clip=["20", 0],
            vae=["21", 0],
            prompt=prompt,
            negative_prompt="",
            resolution=0,
            **encoder_images,
        )
        model = ["24", 0]
        vae = ["21", 0]
        sampler_inputs = {
            "positive": [positive, 0],
            "negative": [positive, 1],
            "latent_image": [positive, 2],
        }

    add(
        "30",
        "KSampler",
        model=model,
        seed=int(seed),
        steps=int(preset["steps"]),
        cfg=float(preset["cfg"]),
        sampler_name="euler",
        scheduler="simple",
        denoise=1.0,
        **sampler_inputs,
    )
    add("31", "VAEDecode", samples=["30", 0], vae=vae)
    add(
        "32",
        "SaveImage",
        images=["31", 0],
        filename_prefix=f"{config['output_prefix']}/keyframes/{preset['prefix']}_{seed}",
    )
    return node


def run_qwen_keyframe(config, backend, prompt, canvas, seed, label):
    """Submit one Qwen anchor and return it as a start *or* an end keyframe.

    ``canvas is None`` means this call is producing a segment's first frame;
    otherwise it is producing that segment's last frame from its first frame.
    Returning ``None`` for the unused side keeps the H3-only "reuse the opening
    draft's last frame" shortcut from firing on an edit model, which would
    otherwise hand a segment an end frame identical to its start.
    """
    graph = build_qwen_keyframe_graph(
        config,
        backend,
        qwen_keyframe_prompt(config, prompt),
        canvas,
        seed,
    )
    prompt_id, entry = submit_graph(
        config,
        graph,
        label or f"Qwen keyframe {seed}",
    )
    images = (entry.get("outputs") or {}).get("32", {}).get("images") or []
    if not images:
        raise RuntimeError("Qwen keyframe image output was not reported")
    path = str(resolve_output(config, images[0]))
    path = align_frame_to_target(config, path, seed=seed, label=f"{label or 'Qwen'} align")
    is_end = canvas is not None
    return {
        "prompt_id": prompt_id,
        "first_frame": None if is_end else path,
        "last_frame": path if is_end else None,
    }


def _request_with_retry(method, url, *, retries=3, **kwargs):
    """request() with exponential backoff on network errors and 5xx replies."""
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code >= 500 and attempt < retries - 1:
                raise requests.RequestException(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                delay = 2 ** attempt
                print(
                    f"[H3] {method} {url} failed ({exc}); retrying in {delay}s",
                    flush=True,
                )
                time.sleep(delay)
    raise last_error


def wait_for_prompt(base_url, prompt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _request_with_retry(
            "GET",
            f"{base_url}/history/{prompt_id}",
            timeout=30,
        )
        response.raise_for_status()
        history = response.json()
        entry = history.get(prompt_id)
        if entry and entry.get("status", {}).get("completed"):
            return entry
        if entry and entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError(json.dumps(entry["status"], ensure_ascii=False))
        time.sleep(3)
    raise TimeoutError(f"Prompt {prompt_id} did not finish in {timeout}s")


def resolve_output(config, item):
    root = Path(config["comfy_output_dir"])
    return root / item.get("subfolder", "") / item["filename"]


def submit_graph(config, graph, label):
    client_id = str(uuid.uuid4())
    response = _request_with_retry(
        "POST",
        f"{config['comfy_url']}/prompt",
        json={"prompt": graph, "client_id": client_id},
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{response.status_code}: {response.text}")
    response.raise_for_status()
    prompt_id = response.json()["prompt_id"]
    print(f"[H3] {label}: submitted {prompt_id}", flush=True)
    entry = wait_for_prompt(config["comfy_url"], prompt_id, timeout=7200)
    return prompt_id, entry


def run_segment(
    config,
    segment,
    segment_index,
    first_frame,
    last_frame,
    *,
    seed=None,
    log_label=None,
):
    if seed is None:
        seed = int(config["base_seed"]) + segment_index
    label = log_label or f"Segment {segment_index + 1}"
    graph = build_graph(
        config,
        segment["prompt"],
        first_frame,
        last_frame,
        seed,
        segment["seconds"],
        segment["two_pass"],
    )
    prompt_id, entry = submit_graph(config, graph, label)
    outputs = entry["outputs"]
    images = outputs.get("39", {}).get("images", [])
    videos = outputs.get("42", {}).get("gifs", []) or outputs.get("42", {}).get("videos", [])
    if not images:
        raise RuntimeError("Last-frame image output was not reported")
    if not videos:
        raise RuntimeError("Segment video output was not reported")
    last_frame = resolve_output(config, images[0])
    video = resolve_output(config, videos[0])
    raw_video = video
    fix_frames = int(config.get("two_pass_fix_opening_frames", 0))
    if segment["two_pass"] and first_frame and fix_frames > 0:
        video = repair_two_pass_opening(
            config,
            video,
            first_frame,
            prompt_id,
            segment["seconds"],
            fix_frames,
        )
        print(
            f"[H3] {label}: replaced first {fix_frames} upscaler frames with the anchor",
            flush=True,
        )
    print(f"[H3] {label}: {video}", flush=True)
    return {
        "segment": segment_index + 1,
        "prompt_id": prompt_id,
        "video": str(video),
        "raw_video": str(raw_video),
        "last_frame": str(last_frame),
        "next_frame": str(last_frame),
        "seconds": segment["seconds"],
        "two_pass": segment["two_pass"],
        "prompt_sha1": hashlib.sha1(segment["prompt"].encode("utf-8")).hexdigest(),
    }


def run_single_clip(config, segment, state_path):
    first_frame = config["fixed_first_frame"]
    last_frame = config["fixed_last_frame"]
    record = run_segment(
        config,
        segment,
        0,
        first_frame,
        last_frame,
        seed=int(config["base_seed"]),
        log_label="Single clip",
    )
    record["video"] = str(
        normalize_video_resolution(
            config,
            record["video"],
            record["prompt_id"],
        )
    )
    record["start_frame"] = first_frame
    record["end_frame"] = last_frame
    state = {
        "phase": "single_clip",
        "segments": [record],
    }
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return record


def end_keyframe_prompt(config, prompt, segment_index):
    """Final prompt used for an AI-generated end keyframe.

    Two optional blocks are appended, in this order: identity re-anchor (only
    every ``keyframe_reanchor_every`` segments) and the camera-continuity note.
    The camera note goes last because the sampler weights the tail the most.
    """
    text = prompt
    reanchor_every = int(config.get("keyframe_reanchor_every", 0) or 0)
    if reanchor_every > 0 and (segment_index + 1) % reanchor_every == 0:
        note = str(
            config.get("keyframe_reanchor_prompt", DEFAULT_KEYFRAME_REANCHOR_PROMPT)
        ).strip()
        if note:
            text = f"{text}\n\n{note}"
    if bool(config.get("keyframe_camera_continuity", True)):
        camera = str(
            config.get(
                "keyframe_camera_prompt", DEFAULT_KEYFRAME_CAMERA_PROMPT
            )
        ).strip()
        if camera:
            text = f"{text}\n\n{camera}"
    return text


def video_prompt_spec(config, spec):
    """Return a copy of ``spec`` with the camera note applied to its prompt.

    The keyframe prompt gets the camera note so the *anchor* keeps the shot
    size; the video prompt gets nothing, which is why a 15 s walk towards the
    camera progressively tightens into a waist-up shot even when both anchors
    are full body. Appending the same note to the segment prompt keeps the
    framing stable for the whole clip. Off by default because it changes the
    generation cache and the look of every existing storyboard.
    """
    if not bool(config.get("video_camera_continuity", False)):
        return spec
    note = str(
        config.get("video_camera_prompt")
        or config.get("keyframe_camera_prompt")
        or DEFAULT_KEYFRAME_CAMERA_PROMPT
    ).strip()
    if not note:
        return spec
    prompt = str(spec.get("prompt") or "")
    if note in prompt:
        return spec
    return {**spec, "prompt": f"{prompt}\n\n{note}"}


def run_keyframe(config, segment, seed, first_frame=None, prompt=None, label=None):
    backend = keyframe_backend(config)
    if backend in QWEN_BACKENDS:
        return run_qwen_keyframe(
            config,
            backend,
            prompt or segment["prompt"],
            first_frame,
            seed,
            label,
        )
    seconds = float(config.get("keyframe_draft_seconds", 2.0))
    keyframe_prompt = prompt or segment["prompt"]
    graph = build_keyframe_graph(config, keyframe_prompt, first_frame, seed, seconds)
    prompt_id, entry = submit_graph(
        config,
        graph,
        label or f"Keyframe {seed}",
    )
    outputs = entry["outputs"]
    first_images = outputs.get("38", {}).get("images", [])
    last_images = outputs.get("40", {}).get("images", [])
    if not first_images or not last_images:
        raise RuntimeError("Keyframe image outputs were not reported")
    first_path = str(resolve_output(config, first_images[0]))
    last_path = str(resolve_output(config, last_images[0]))
    return {
        "prompt_id": prompt_id,
        "first_frame": align_frame_to_target(config, first_path, seed=seed, label="H3 keyframe align"),
        "last_frame": align_frame_to_target(config, last_path, seed=seed + 1, label="H3 keyframe align"),
    }


def find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise RuntimeError("ffmpeg was not found; install it or install imageio-ffmpeg") from exc
    return ffmpeg


def find_ffprobe():
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        for name in ("ffprobe.exe", "ffprobe"):
            bundled = Path(ffmpeg).with_name(name)
            if bundled.is_file():
                return str(bundled)
    try:
        import imageio_ffmpeg

        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
        for name in ("ffprobe.exe", "ffprobe"):
            bundled = ffmpeg.with_name(name)
            if bundled.is_file():
                return str(bundled)
    except Exception:
        pass
    raise RuntimeError("ffprobe was not found; install ffmpeg with ffprobe support")


def probe_video_size(video):
    try:
        import cv2

        capture = cv2.VideoCapture(str(video))
        try:
            if capture.isOpened():
                width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if width > 0 and height > 0:
                    return width, height
        finally:
            capture.release()
    except Exception:
        pass

    result = subprocess.run(
        [
            find_ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {video}")
    return int(streams[0]["width"]), int(streams[0]["height"])


def frame_motion(video, sample_width=208):
    """逐帧算两种差异指标，返回长度 ``帧数 - 1`` 的 ``[(mean, p99)]``。

    ``mean`` 分不开「真静止」和「缓慢抬手」：实测 seg1 静止段 mean 0.8、抬手段
    mean 1.1，同一量级。``p99`` 抓的是「少量像素大幅移动」——手/头一动就飙到
    10~45，纯呼吸/压缩噪声只到 2~8。所以判定静止看 p99，不看 mean。
    """
    import cv2

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video}")
    scores = []
    previous = None
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            small = cv2.resize(
                frame,
                (sample_width, max(1, int(height * sample_width / float(width)))),
                interpolation=cv2.INTER_AREA,
            )
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if previous is not None:
                delta = np.abs(gray - previous)
                scores.append(
                    (float(delta.mean()), float(np.percentile(delta, 99)))
                )
            previous = gray
    finally:
        capture.release()
    if not scores:
        raise RuntimeError(f"no frames decoded: {video}")
    return scores


def frame_diffs(video, sample_width=208):
    """逐帧灰度均值差（旧口径，给尾部统计用）。

    返回长度为 ``帧数 - 1`` 的列表：``diffs[i]`` 是第 i 帧到第 i+1 帧的平均差。
    """
    return [mean for mean, _ in frame_motion(video, sample_width=sample_width)]


def static_tail_frames(video, threshold=1.0, sample_width=208):
    """数出视频「尾部连续不动」的帧数。

    H3 有个固定毛病：一段 4 秒的提示词里动作 1.8 秒就演完了，剩下 2 秒人物
    原地定格——首尾帧都对，但观感就是在等。实测（2026-11-02，抬手碰锁骨）：

        seg1 尾部连续静止 53 帧 = 2.21s
        seg2 尾部连续静止 13 帧 = 0.54s

    拼起来就是中间「停两秒再跳一下」。返回值 ``(tail_frames, total_frames)``；
    读不了就抛异常，调用方自己降级。
    """
    diffs = frame_diffs(video, sample_width=sample_width)
    tail = 0
    for value in reversed(diffs):
        if value < threshold:
            tail += 1
        else:
            break
    return tail, len(diffs) + 1


def find_static_runs(diffs, threshold=1.0, min_frames=4):
    """把逐帧差序列里「连续不动」的区间找出来，返回帧号闭区间 ``[(first, last)]``。

    ``diffs[i]`` 是第 i 帧到第 i+1 帧的差；连续 ``k`` 个低差值覆盖 ``k+1`` 帧。
    """
    quiet = [value < threshold for value in diffs]
    runs = []
    index = 0
    while index < len(quiet):
        if not quiet[index]:
            index += 1
            continue
        end = index
        while end < len(quiet) and quiet[end]:
            end += 1
        # diff 的 index..end-1 都低 → 帧 index..end 静止
        if end - index + 1 >= min_frames:
            runs.append((index, end))
        index = end
    return runs


def build_keep_mask(total, runs, head_keep, mid_keep, tail_keep, mid_min_frames):
    """把每段静止区间抽稀成 head/mid/tail_keep 帧，返回长度 ``total`` 的布尔掩码。

    和早先「只留区间前 N 帧」相比改成**均匀取帧**：区间两端各留一帧，跳变摊在
    区间内部，不会正好落在动作起手的一瞬间（那里最容易被看出卡顿）。
    """
    keep = np.ones(total, dtype=bool)
    for first, last in runs:
        count = last - first + 1
        if first == 0:
            allowed = head_keep
        elif last >= total - 1:
            allowed = tail_keep
        elif count >= mid_min_frames:
            allowed = mid_keep
        else:
            allowed = count
        allowed = max(1, min(count, allowed)) if allowed > 0 else count
        picks = np.unique(
            np.linspace(first, last, allowed).round().astype(int)
        )
        keep[first : last + 1] = False
        keep[picks] = True
    return keep


def thin_creep(keep, scores, static_runs, threshold, factor, min_frames, window=6):
    """把「缓动区」按 ``factor`` 抽稀：「在动，但慢到看不出在动」的区间。

    阈值 9 只抓得住真静止（p99 < 9 = 人物一帧都没动）。实测 seg2 的 p99 中位数
    是 11——整段都是缓慢的肢体漂移，判不成静止，可一分钟只挪几像素，观感就是
    定格。这类区间按比例抽帧，把「几乎不动」加速到「正常动」。

    判据用**滑动平均**而不是单帧值：缓动区的 p99 在 8~20 之间抖动，单帧判会有
    一半帧漏掉，平滑 0.25 秒后才连成片。
    """
    if factor < 2 or threshold <= 0:
        return keep
    inside_static = np.zeros(len(keep), dtype=bool)
    for first, last in static_runs:
        inside_static[first : last + 1] = True
    smoothed = scores.astype(np.float32)
    if window > 1:
        kernel = np.ones(window, dtype=np.float32) / window
        smoothed = np.convolve(smoothed, kernel, mode="same")
    quiet = (smoothed < threshold) & ~inside_static[:-1]
    index = 0
    while index < len(quiet):
        if not quiet[index]:
            index += 1
            continue
        end = index
        while end < len(quiet) and quiet[end]:
            end += 1
        # quiet 的 index..end-1 覆盖帧 index..end
        if end - index + 1 >= min_frames:
            for frame in range(index, min(end + 1, len(keep))):
                if (frame - index) % factor:
                    keep[frame] = False
        index = end
    return keep


def mask_to_ranges(keep):
    """布尔掩码 → 要保留的帧区间 ``[(first, last))``。"""
    ranges = []
    start = None
    for index, flag in enumerate(keep):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            ranges.append((start, index))
            start = None
    if start is not None:
        ranges.append((start, len(keep)))
    return ranges


def has_audio_stream(video):
    """用 ``ffmpeg -i`` 的探测输出判断有没有音轨（环境里未必有独立 ffprobe）。"""
    result = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-i", str(video)],
        check=False,
        capture_output=True,
        text=True,
    )
    return "Audio:" in (result.stderr or "")


def render_keep_ranges(video, ranges, fps, out_path):
    """只保留 ``ranges`` 里的帧（音轨按同一时间轴同步裁剪），重编码到 ``out_path``。"""
    ffmpeg = find_ffmpeg()
    audio = has_audio_stream(video)
    filters = []
    labels = []
    for index, (first, last) in enumerate(ranges):
        filters.append(
            f"[0:v]trim=start_frame={first}:end_frame={last},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        if audio:
            filters.append(
                f"[0:a]atrim=start={first / fps:.6f}:end={last / fps:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
            labels.append(f"[v{index}][a{index}]")
        else:
            labels.append(f"[v{index}]")
    filters.append(
        "".join(labels) + f"concat=n={len(ranges)}:v=1:a={1 if audio else 0}[v][a]"
        if audio
        else "".join(labels) + f"concat=n={len(ranges)}:v=1:a=0[v]"
    )
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
    ]
    if audio:
        command.extend(["-map", "[a]"])
    command.extend(
        [
            "-r",
            str(int(fps)),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)


def compact_static(config, playlist, work_dir):
    """拼接前压掉每段的「定格等待」，返回新的 ``(path, seconds)`` 列表。

    H3 的段有个固定毛病：4 秒的提示词，动作 1.8 秒就演完，剩下 2 秒人物原地
    定格。首尾帧、QA 全过，但拼起来就是「停一秒再接下一段」。实测（2026-11-02）：

        seg1 尾部静止 53 帧(2.21s)；成片里还夹着开头 1.12s、中段 0.83s 的静止。

    只剪尾巴只能消掉一部分，所以头部和中段一起压：静止区间抽稀到 head_keep /
    mid_keep / tail_keep 帧，动作区一帧不动。因为静止帧之间几乎完全一样，
    抽稀看不出跳帧，只是节奏不拖了。
    """
    if not bool(config.get("static_compact", True)):
        return playlist
    fps = float(config.get("fps", 24) or 24)
    # 阈值是 p99 口径：< 9 说明整帧没有「局部大幅移动」，即人物真的没动。
    threshold = float(config.get("static_compact_threshold", 9.0))
    head_keep = max(0, int(config.get("static_compact_head_keep", 2)))
    mid_keep = max(1, int(config.get("static_compact_mid_keep", 2)))
    tail_keep = max(0, int(config.get("static_compact_tail_keep", 3)))
    min_frames = max(2, int(round(float(config.get("static_compact_min_seconds", 0.4)) * fps)))
    mid_min_frames = max(2, int(round(float(config.get("static_compact_mid_min_seconds", 0.4)) * fps)))
    min_saving = max(1, int(round(float(config.get("static_compact_min_saving", 0.3)) * fps)))
    # 第二层：缓动区（在动但太慢）按比例抽帧，把「几乎定格」加速。
    creep_threshold = float(config.get("static_compact_creep_threshold", 16.0))
    creep_factor = max(1, int(config.get("static_compact_creep_factor", 2)))
    creep_min_frames = max(
        2, int(round(float(config.get("static_compact_creep_min_seconds", 0.3)) * fps))
    )
    creep_window = max(1, int(round(float(config.get("static_compact_creep_window", 0.25)) * fps)))

    compacted = []
    for index, (path, seconds) in enumerate(playlist, 1):
        try:
            scores = frame_motion(path)
        except Exception as exc:
            print(
                f"[H3] Segment {index}: 静止检测跳过 "
                f"({type(exc).__name__}: {exc})",
                flush=True,
            )
            compacted.append((path, seconds))
            continue
        p99 = np.array([p99 for _, p99 in scores])
        total = len(p99) + 1
        runs = find_static_runs(list(p99), threshold=threshold, min_frames=2)
        head_tail_runs = [
            (first, last)
            for first, last in runs
            if (first == 0 or last >= total - 1) and (last - first + 1) >= min_frames
        ]
        mid_runs = [
            (first, last)
            for first, last in runs
            if 0 < first and last < total - 1 and (last - first + 1) >= mid_min_frames
        ]
        active = sorted(head_tail_runs + mid_runs)
        if not active and creep_factor < 2:
            compacted.append((path, seconds))
            continue
        keep = build_keep_mask(total, active, head_keep, mid_keep, tail_keep, mid_min_frames)
        keep = thin_creep(
            keep,
            p99,
            active,
            threshold=creep_threshold,
            factor=creep_factor,
            min_frames=creep_min_frames,
            window=creep_window,
        )
        ranges = mask_to_ranges(keep)
        kept = sum(b - a for a, b in ranges)
        removed = total - kept
        # 保护：只有「整段几乎不动」（空镜、极慢的慢动作）才跳过——那种素材
        # 压完会剩下几帧，看起来就是坏片。动作段里静止占比 7 成是常态
        # （实测 seg1：96 帧里 71 帧静止），必须照压，否则开头的定格原样留着。
        if removed < min_saving or removed >= total * float(
            config.get("static_compact_max_ratio", 0.9)
        ):
            if removed >= min_saving:
                print(
                    f"[H3] Segment {index}: 静止占 {removed / total:.0%}，"
                    "判定为空镜/慢动作，不压",
                    flush=True,
                )
            compacted.append((path, seconds))
            continue
        target = Path(work_dir) / f"{Path(path).stem}.compact.mp4"
        try:
            render_keep_ranges(path, ranges, fps, target)
        except Exception as exc:
            print(
                f"[H3] Segment {index}: 静止压缩失败 "
                f"({type(exc).__name__}: {exc})，保留原段",
                flush=True,
            )
            compacted.append((path, seconds))
            continue
        new_seconds = kept / fps
        print(
            f"[H3] Segment {index}: 静止压缩 {total} → {kept} 帧"
            f"（{total / fps:.2f}s → {new_seconds:.2f}s，去掉 {len(active)} 处定格）",
            flush=True,
        )
        compacted.append((target.as_posix(), new_seconds))
    return compacted


def pick_scale_flags(config, src_size, dst_size):
    """放大时用 bicubic：lanczos 放大低分辨率扩散帧会产生振铃/硬边（观感=锐化过度）。
    缩小时仍用 lanczos（无过冲问题，锐度更稳）。可用 final_scale_flags 强制指定。"""
    explicit = config.get("final_scale_flags")
    if explicit:
        return str(explicit)
    try:
        src_px = int(src_size[0]) * int(src_size[1])
        dst_px = int(dst_size[0]) * int(dst_size[1])
    except (TypeError, IndexError, ValueError):
        return "bicubic"
    return "bicubic" if dst_px > src_px else "lanczos"


def normalize_video_resolution(config, video, prompt_id):
    video = Path(video)
    width = int(config["final_width"])
    height = int(config["final_height"])
    src_size = probe_video_size(video)
    if src_size == (width, height):
        return video
    flags = pick_scale_flags(config, src_size, (width, height))

    output = (
        Path(config.get("final_output_dir", config["comfy_output_dir"]))
        / config["output_prefix"]
        / f"single_{prompt_id[:8]}_{width}x{height}.mp4"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    # 等比放大到盖住目标框再中心裁切，而不是直接 scale=W:H。
    # 直接 scale 是硬拉伸：608x1056(0.5758) → 832x1472(0.5652) 会把整片横向压扁 2.4%，
    # 人脸会变窄、圆的东西变椭圆。和关键帧那边的 crop 用同一套几何。
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags={flags},"
        f"crop={width}:{height}"
    )
    subprocess.run(
        [
            find_ffmpeg(),
            "-y",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            video_filter,
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    print(
        f"[H3] Normalized single clip to {width}x{height}: {output}",
        flush=True,
    )
    return output


def repair_two_pass_opening(
    config,
    video,
    anchor_image,
    prompt_id,
    seconds,
    fix_frames,
):
    ffmpeg = find_ffmpeg()
    width = int(config.get("final_width", 704))
    height = int(config.get("final_height", 1216))
    output = (
        Path(config.get("final_output_dir", config["comfy_output_dir"]))
        / config["output_prefix"]
        / f"segment_{prompt_id[:8]}_two_pass.mp4"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    blend_frames = int(config.get("two_pass_blend_frames", 4))
    filter_complex = build_two_pass_repair_filter(
        width,
        height,
        fix_frames,
        blend_frames,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-framerate",
            "24",
            "-i",
            input_image_path(config, anchor_image),
            "-i",
            str(video),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-t",
            str(float(seconds)),
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    return output


def repair_blend_weight(fix_frames, blend_frames, frame_index):
    """Weight of the *generated* footage for frame ``frame_index``.

    0.0 = the frame is the static anchor image, 1.0 = pure generated footage.
    The first ``fix_frames`` frames are pinned to the anchor, then the anchor
    fades out linearly over ``blend_frames`` frames. Everything after that is
    generated footage for the rest of the clip -- getting this ramp backwards
    (which is what shipped first) turned every two-pass segment into a still
    image, i.e. a slideshow instead of a video.
    """
    if fix_frames < 0:
        fix_frames = 0
    if blend_frames <= 0:
        return 0.0 if frame_index < fix_frames else 1.0
    ratio = (frame_index - fix_frames) / float(blend_frames)
    return max(0.0, min(1.0, ratio))


def build_two_pass_repair_filter(width, height, fix_frames, blend_frames=0):
    if blend_frames <= 0:
        return (
            f"[0:v]scale={width}:{height},setsar=1,"
            f"trim=end_frame={fix_frames},setpts=PTS-STARTPTS[a];"
            f"[1:v]scale={width}:{height},setsar=1,"
            f"trim=start_frame={fix_frames},setpts=PTS-STARTPTS[b];"
            "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"
        )
    # Linear ramp from the static anchor to the generated footage: the first
    # fix_frames frames stay fully on the anchor, then the anchor fades out
    # over blend_frames frames (weight of B goes 0 -> 1). The quoted expression
    # keeps the commas inside clip() safe from the filtergraph parser.
    weight = f"clip((N-{fix_frames})/{blend_frames},0,1)"
    return (
        f"[0:v]scale={width}:{height},setsar=1,format=yuv420p[a];"
        f"[1:v]scale={width}:{height},setsar=1,format=yuv420p[b];"
        f"[a][b]blend=shortest=1:all_expr='A*(1-{weight})+B*{weight}',"
        "format=yuv420p[v]"
    )


def rebuild_two_pass_postprocess(config, state, state_path, fix_frames):
    """Re-run the two-pass opening repair for every recorded segment.

    The raw model output is kept in ``state["segments"][i]["raw_video"]``, so a
    post-processing bug (like the inverted blend ramp that froze every clip)
    can be fixed without burning GPU time again: rebuild the repaired files
    from the raws and let the normal stitch step produce a new final video.
    """
    if fix_frames <= 0:
        print(
            "[H3] two_pass_fix_opening_frames=0，没有需要重建的开头帧；"
            "将直接用现有分段重新拼接。",
            flush=True,
        )
        return 0
    anchors = {
        int(item.get("segment", 0)): item.get("start_frame")
        for item in state.get("keyframes", [])
    }
    rebuilt = 0
    for segment in sorted(
        state.get("segments", []), key=lambda value: value.get("segment", 0)
    ):
        if not segment.get("two_pass"):
            continue
        raw = Path(segment.get("raw_video") or "")
        if not raw.is_file():
            print(
                f"[H3] 段 {segment.get('segment')}: 缺少 raw_video（{raw}），跳过重建。",
                flush=True,
            )
            continue
        anchor = segment.get("start_keyframe") or anchors.get(
            int(segment.get("segment", 0))
        )
        if not anchor or not Path(anchor).is_file():
            print(
                f"[H3] 段 {segment.get('segment')}: 找不到开头锚点帧，跳过重建。",
                flush=True,
            )
            continue
        prompt_id = str(segment.get("prompt_id") or f"seg{segment.get('segment')}")
        video = repair_two_pass_opening(
            config,
            raw,
            anchor,
            prompt_id,
            float(segment["seconds"]),
            fix_frames,
        )
        segment["video"] = str(video)
        rebuilt += 1
        print(f"[H3] 段 {segment.get('segment')}: 已重建 {video}", flush=True)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return rebuilt


def manual_keyframe(overrides, segment, side, config=None):
    """Return the user-supplied keyframe path for a segment side, if any.

    The review gate lets a human replace an AI keyframe after it has been
    rendered. Those overrides live in ``state["keyframe_overrides"]`` and always
    win over both cached and freshly generated frames.

    给了 ``config`` 就顺手把这张图对齐到成片分辨率：用户可以直接往磁盘上覆盖
    一张任意尺寸的图（截图、PS 改过的、别处生成的），这里统一归一成
    final_width x final_height，省得视频阶段再被拉伸。已经对齐过的会原地跳过。
    """
    if not overrides:
        return None
    entry = overrides.get(str(segment))
    if entry is None:
        entry = overrides.get(segment)
    if not isinstance(entry, dict):
        return None
    path = entry.get(side)
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    resolved = candidate.as_posix()
    if config is not None:
        try:
            resolved = align_frame_to_target(config, resolved, label="manual keyframe align")
        except Exception as exc:
            print(
                f"[H3] 手动帧对齐失败，按原样使用（{type(exc).__name__}: {exc}）",
                flush=True,
            )
    return resolved


def keyframe_attempt(attempts, segment, side):
    """How many times a human has asked for this keyframe to be re-rolled."""
    if not attempts:
        return 0
    entry = attempts.get(str(segment))
    if entry is None:
        entry = attempts.get(segment)
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get(side, 0)))
    except (TypeError, ValueError):
        return 0


def keyframe_source(signature):
    """Human-readable origin of a cached keyframe signature."""
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


def previous_video_tail(state, segment_number):
    """Last frame of the previous segment's rendered video, if it is on disk.

    Used by ``free_end`` chains: a segment without an end anchor hands its real
    last frame to the next segment, so that segment starts from rendered footage
    instead of a short draft that cannot reach the target pose.
    """
    record = next(
        (
            item
            for item in state.get("segments", [])
            if int(item.get("segment", 0)) == int(segment_number) - 1
        ),
        None,
    )
    if not record:
        return None
    for key in ("last_frame", "next_frame"):
        path = record.get(key)
        if path and Path(path).is_file():
            return str(path)
    return None


def missing_keyframes(state, count):
    """Segment numbers whose start/end keyframe is absent from disk."""
    existing = {
        int(item["segment"]): item
        for item in state.get("keyframes", [])
        if "segment" in item
    }
    missing = []
    for index in range(1, int(count) + 1):
        item = existing.get(index)
        if not item:
            missing.append(index)
            continue
        for side in ("start", "end"):
            # ``free`` ends have no anchor at all; ``pending-video`` frames are
            # filled in during the video stage from the previous segment's last
            # rendered frame, so they must not block the videos stage here.
            if item.get(f"{side}_source") in {
                "free",
                "pending-video",
                "from-video",
            }:
                continue
            path = Path(item.get(f"{side}_frame") or "")
            if not str(path) or not path.is_file():
                missing.append(index)
                break
    return sorted(set(missing))


def ensure_keyframe_chain(
    config,
    specs,
    state,
    state_path,
    *,
    max_segments=None,
    force=False,
):
    count = len(specs) if max_segments is None else min(len(specs), max_segments)
    draft_seconds = float(config.get("keyframe_draft_seconds", 2.0))
    overrides = state.get("keyframe_overrides") or {}
    attempts = state.get("keyframe_attempts") or {}
    existing = {
        int(item["segment"]): item
        for item in state.get("keyframes", [])
        if "segment" in item
    }
    ordered = []

    start_frame = config.get("opening_frame") or None
    start_prompt_id = None
    start_signature = f"explicit:{start_frame}" if start_frame else None
    opening_draft_last_frame = None
    opening_draft_prompt_id = None

    for index in range(count):
        spec = specs[index]
        previous = existing.get(index + 1)
        continues_from_previous = (
            index > 0 and should_continue_from_previous(config, spec)
        )

        # A ``free_end`` predecessor has no end anchor, so this segment cannot
        # inherit a frame from it: it starts from the previous *rendered* video's
        # last frame. That frame only exists after the video stage has run, hence
        # the pending state (the panel shows "待定首帧" until then).
        previous_is_free = bool(
            (
                previous
                and (
                    previous.get("end_frame") in (None, "")
                    or previous.get("end_source") == "free"
                )
            )
            or (index > 0 and specs[index - 1].get("free_end"))
        )
        manual_start = manual_keyframe(overrides, index + 1, "start", config)
        if manual_start:
            start_frame = manual_start
            start_prompt_id = None
            start_signature = f"manual:{manual_start}"
            if index == 0:
                opening_draft_last_frame = None
                opening_draft_prompt_id = None
            print(
                f"[H3] Segment {index + 1}: using replaced start keyframe "
                f"{manual_start}",
                flush=True,
            )
        elif continues_from_previous and previous_is_free:
            video_tail = previous_video_tail(state, index + 1)
            if video_tail:
                start_frame = video_tail
                start_prompt_id = None
                start_signature = f"from-video:{Path(video_tail).as_posix()}"
                print(
                    f"[H3] Segment {index + 1}: start frame taken from the "
                    f"previous segment's last rendered frame: {video_tail}",
                    flush=True,
                )
            else:
                start_frame = None
                start_prompt_id = None
                start_signature = f"pending-video:{index + 1}"
                print(
                    f"[H3] Segment {index + 1}: start frame is pending — the "
                    "previous segment ends freely, so it will be taken from that "
                    "render's last frame during the video stage.",
                    flush=True,
                )
        elif index == 0 or not continues_from_previous:
            start_spec = {
                "seconds": draft_seconds,
                "two_pass": False,
                "prompt": spec["start_frame_prompt"],
            }
            start_signature = generation_signature(config, start_spec, None)
            if index == 0 and start_frame is not None:
                start_signature = f"explicit:{start_frame}"
            elif (
                not force
                and previous
                and previous.get("start_sha1") == start_signature
                and previous.get("start_frame")
                and Path(previous["start_frame"]).is_file()
            ):
                start_frame = previous["start_frame"]
                start_prompt_id = previous.get("start_prompt_id")
                opening_draft_last_frame = previous.get(
                    "opening_draft_last_frame"
                )
                opening_draft_prompt_id = previous.get(
                    "opening_draft_prompt_id"
                )
                print(
                    f"[H3] Reusing opening keyframe: {start_frame}",
                    flush=True,
                )
            else:
                record = run_keyframe(
                    config,
                    spec,
                    int(config["base_seed"])
                    + 900000
                    + keyframe_attempt(attempts, index + 1, "start") * 101,
                    first_frame=None,
                    prompt=spec["start_frame_prompt"],
                    label=(
                        "Opening keyframe"
                        if index == 0
                        else f"Independent start keyframe {index + 1}"
                    ),
                )
                start_frame = record["first_frame"]
                start_prompt_id = record["prompt_id"]
                if index == 0:
                    opening_draft_last_frame = record["last_frame"]
                    opening_draft_prompt_id = record["prompt_id"]

        # An empty cached signature means the human asked for a re-roll: the
        # frame must be rendered again instead of being reused or shortcut.
        reroll_start = bool(previous) and previous.get("start_sha1") == ""
        reroll_end = bool(previous) and previous.get("end_sha1") == ""
        hold_composition = should_anchor_last_frame(config, spec)
        manual_end = manual_keyframe(overrides, index + 1, "end", config)
        if manual_end:
            end_frame = manual_end
            end_prompt_id = None
            end_signature = f"manual:{manual_end}"
            print(
                f"[H3] Segment {index + 1}: using replaced end keyframe "
                f"{manual_end}",
                flush=True,
            )
        elif hold_composition:
            end_frame = start_frame
            end_prompt_id = start_prompt_id
            end_signature = f"hold:{start_signature}"
        elif spec.get("free_end"):
            # No end anchor at all: the segment is free to end wherever the
            # action takes it, and its rendered last frame becomes the next
            # segment's start. This is what makes long travels ("walk 10 m to
            # the bench") work -- anchoring them to a 5 s draft only produced
            # cropped, drifting frames.
            end_frame = None
            end_prompt_id = None
            end_signature = f"free:{start_signature}"
            print(
                f"[H3] Segment {index + 1}: free end (no end keyframe); the "
                "rendered video decides where it stops.",
                flush=True,
            )
        elif start_frame is None:
            end_frame = None
            end_prompt_id = None
            end_signature = f"pending-video:end:{index + 1}"
        elif (
            index == 0
            and spec["end_frame_prompt"] == spec["prompt"]
            and opening_draft_last_frame
            and Path(opening_draft_last_frame).is_file()
            and not reroll_end
        ):
            end_frame = opening_draft_last_frame
            end_prompt_id = opening_draft_prompt_id
            end_signature = f"opening-draft:{start_signature}"
            print(
                f"[H3] Reusing opening draft end keyframe: {end_frame}",
                flush=True,
            )
        else:
            end_prompt = end_keyframe_prompt(config, spec["end_frame_prompt"], index)
            if end_prompt != spec["end_frame_prompt"]:
                extras = []
                if int(config.get("keyframe_reanchor_every", 0) or 0) > 0:
                    extras.append("identity re-anchor")
                if bool(config.get("keyframe_camera_continuity", True)):
                    extras.append("camera continuity")
                print(
                    f"[H3] Segment {index + 1}: end keyframe prompt carries "
                    + " + ".join(extras),
                    flush=True,
                )
            end_spec = {
                "seconds": draft_seconds,
                "two_pass": False,
                "prompt": end_prompt,
            }
            end_signature = generation_signature(
                config,
                end_spec,
                start_prompt_id,
            )
            if (
                not force
                and previous
                and previous.get("end_sha1") == end_signature
                and previous.get("end_frame")
                and Path(previous["end_frame"]).is_file()
            ):
                end_frame = previous["end_frame"]
                end_prompt_id = previous.get("end_prompt_id")
                print(
                    f"[H3] Reusing end keyframe {index + 1}: {end_frame}",
                    flush=True,
                )
            else:
                record = run_keyframe(
                    config,
                    spec,
                    int(config["base_seed"])
                    + 700000
                    + index * 2
                    + keyframe_attempt(attempts, index + 1, "end") * 101,
                    first_frame=start_frame,
                    prompt=spec["end_frame_prompt"],
                    label=f"End keyframe {index + 1}",
                )
                end_frame = record["last_frame"]
                end_prompt_id = record["prompt_id"]

        item = {
            "segment": index + 1,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "start_prompt_id": start_prompt_id,
            "end_prompt_id": end_prompt_id,
            "start_sha1": start_signature,
            "end_sha1": end_signature,
            "continue_from_previous": continues_from_previous,
            "opening_draft_last_frame": opening_draft_last_frame,
            "opening_draft_prompt_id": opening_draft_prompt_id,
            "start_source": keyframe_source(start_signature),
            "end_source": keyframe_source(end_signature),
            "free_end": bool(spec.get("free_end")),
        }
        ordered.append(item)
        merged = [
            value
            for value in state.get("keyframes", [])
            if int(value.get("segment", -1)) != index + 1
        ]
        merged.append(item)
        merged.sort(key=lambda value: value["segment"])
        state["keyframes"] = merged
        state["phase"] = "keyframes"
        state["keyframe_total"] = count
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"[H3] Keyframe pair {index + 1}/{count} ready",
            flush=True,
        )

        start_frame = end_frame
        start_prompt_id = end_prompt_id
        start_signature = end_signature

    return ordered


def build_fade_filters(boundaries):
    video_filters = []
    audio_filters = []
    for boundary, duration in boundaries:
        half = max(0.001, float(duration) / 2.0)
        fade_out_start = max(0.0, boundary - half)
        video_filters.extend(
            [
                f"fade=t=out:st={fade_out_start:.6f}:d={half:.6f}:"
                f"enable='between(t,{fade_out_start:.6f},{boundary:.6f})'",
                f"fade=t=in:st={boundary:.6f}:d={half:.6f}:"
                f"enable='between(t,{boundary:.6f},{boundary + half:.6f})'",
            ]
        )
        audio_filters.extend(
            [
                f"afade=t=out:st={fade_out_start:.6f}:d={half:.6f}:"
                f"enable='between(t,{fade_out_start:.6f},{boundary:.6f})'",
                f"afade=t=in:st={boundary:.6f}:d={half:.6f}:"
                f"enable='between(t,{boundary:.6f},{boundary + half:.6f})'",
            ]
        )
    return video_filters, audio_filters


def stitch_segments(
    config,
    playlist,
    work_dir=HERE,
    total_seconds=None,
    fade_boundaries=None,
    audio_fade_boundaries=None,
):
    """Concatenate segment videos into the final long video.

    playlist is an ordered list of ``(path, seconds)`` tuples.
    ``fade_boundaries`` are video fade points (story cuts only); they force a
    full video re-encode. ``audio_fade_boundaries`` are short audio
    micro-fades applied at every boundary to remove clicks; they do not
    change the timeline, so A/V sync never drifts.
    """
    ffmpeg = find_ffmpeg()
    concat_file = Path(work_dir) / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{Path(path).as_posix()}'\n" for path, _ in playlist),
        encoding="utf-8",
    )
    output = Path(config.get("final_output_dir", config["comfy_output_dir"])) / config["output_prefix"] / "long_video.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    if total_seconds is None:
        total_seconds = sum(float(seconds) for _, seconds in playlist)
    width = int(config.get("final_width", 704))
    height = int(config.get("final_height", 1216))
    fade_boundaries = fade_boundaries or []
    audio_fade_boundaries = audio_fade_boundaries or []
    video_fade_filters, _ = build_fade_filters(fade_boundaries)
    _, audio_fade_filters = build_fade_filters(audio_fade_boundaries)

    def encode_full():
        # 同上：等比盖住再裁，避免比例微差被硬拉伸。
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height}"
        )
        if video_fade_filters:
            video_filter += "," + ",".join(video_fade_filters)
        command = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-t",
            str(total_seconds),
            "-vf",
            video_filter,
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
        if audio_fade_filters:
            command.extend(["-af", ",".join(audio_fade_filters)])
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        subprocess.run(
            command,
            check=True,
        )

    try:
        needs_scale = probe_video_size(playlist[0][0]) != (width, height)
    except Exception:
        needs_scale = True
    if video_fade_filters or needs_scale:
        encode_full()
        return output

    # Zero-loss video pass: stream-copy the H.264 track straight through and
    # only re-encode audio when boundary micro-fades are requested.
    command = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-t",
        str(total_seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
    ]
    if audio_fade_filters:
        command.extend(["-af", ",".join(audio_fade_filters)])
        command.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        command.extend(["-c:a", "copy"])
    command.extend(["-movflags", "+faststart", str(output)])
    try:
        subprocess.run(
            command,
            check=True,
        )
    except (subprocess.CalledProcessError, RuntimeError, OSError):
        print(
            "[H3] Stream-copy concat failed; falling back to a full re-encode.",
            flush=True,
        )
        encode_full()
    return output


_FACE_APP = None


def _read_video_frames(video, samples=3):
    """Sample first / middle / last frames of a video as BGR arrays."""
    try:
        import cv2
    except ImportError:
        return None
    capture = cv2.VideoCapture(str(video))
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return None
        indexes = sorted({0, total // 2, total - 1})[:samples]
        frames = {}
        for target in indexes:
            capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = capture.read()
            if not ok:
                return None
            frames[target] = frame
        return frames
    finally:
        capture.release()


def _box_sum(values, k=8):
    integral = np.pad(values, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (
        integral[k:, k:]
        - integral[:-k, k:]
        - integral[k:, :-k]
        + integral[:-k, :-k]
    )


def _cover_crop(image, target_w, target_h):
    """Resize + centre-crop `image` so it covers `target_w x target_h`."""
    import cv2

    height, width = image.shape[:2]
    scale = max(target_w / width, target_h / height)
    new_w = max(target_w, int(round(width * scale)))
    new_h = max(target_h, int(round(height * scale)))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    offset_x = (new_w - target_w) // 2
    offset_y = (new_h - target_h) // 2
    return resized[
        offset_y : offset_y + target_h, offset_x : offset_x + target_w
    ]


def _match_anchor_to_frame(anchor, frame):
    """Align a (usually high-res) anchor keyframe to a decoded video frame.

    Anchor keyframes are rendered at `keyframe_megapixels` and do not always
    share the video's aspect ratio (e.g. 608x1056 keyframe vs 288x544 frame).
    Comparing them directly with SSIM penalises sharpness difference, aspect
    mismatch and sub-pixel composition shifts rather than real content drift.
    We therefore cover-crop the anchor to the frame's aspect, blur both and
    compare them on a coarse 96x96 grid so SSIM tracks coarse content.

    Calibrated on job 20261030_143853_304cf442: same-segment pairs score
    0.60-0.97 while cross-segment pairs stay below 0.16, so the 0.55 default
    threshold separates real drift from benign composition offset.
    """
    import cv2

    height, width = frame.shape[:2]
    matched = _cover_crop(anchor, width, height)
    blur = (5, 5)
    matched = cv2.GaussianBlur(matched, blur, 4.0)
    frame_blurred = cv2.GaussianBlur(frame, blur, 4.0)
    coarse_a = cv2.resize(matched, (96, 96), interpolation=cv2.INTER_AREA)
    coarse_b = cv2.resize(frame_blurred, (96, 96), interpolation=cv2.INTER_AREA)
    return coarse_a, coarse_b


def compute_ssim(image_a, image_b, k=8):
    """Local SSIM on grayscale 256x256 versions of two BGR frames."""
    try:
        import cv2
    except ImportError:
        return None

    def grayscale(image):
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return cv2.resize(image, (256, 256)).astype(np.float64)

    a = grayscale(image_a)
    b = grayscale(image_b)
    mu_a = _box_sum(a, k) / (k * k)
    mu_b = _box_sum(b, k) / (k * k)
    var_a = _box_sum(a * a, k) / (k * k) - mu_a ** 2
    var_b = _box_sum(b * b, k) / (k * k) - mu_b ** 2
    cov_ab = _box_sum(a * b, k) / (k * k) - mu_a * mu_b
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    ssim_map = ((2 * mu_a * mu_b + c1) * (2 * cov_ab + c2)) / (
        (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
    )
    return float(np.clip(ssim_map, -1.0, 1.0).mean())


def _face_similarity(reference_path, frame):
    """Cosine similarity between the largest faces in reference and frame.

    Requires the optional `insightface` package; returns None when it is not
    installed so the rest of the QA report still works.
    """
    global _FACE_APP
    try:
        import cv2
        from insightface.app import FaceAnalysis
    except ImportError:
        return None
    try:
        if _FACE_APP is None:
            _FACE_APP = FaceAnalysis(
                name="buffalo_l", providers=["CPUExecutionProvider"]
            )
            _FACE_APP.prepare(ctx_id=0, det_size=(640, 640))
        reference = cv2.imread(str(reference_path))
        if reference is None:
            return None

        def largest_face(faces):
            if not faces:
                return None
            return max(
                faces,
                key=lambda face: (face.bbox[2] - face.bbox[0])
                * (face.bbox[3] - face.bbox[1]),
            )

        reference_face = largest_face(_FACE_APP.get(reference))
        frame_face = largest_face(_FACE_APP.get(frame))
        if reference_face is None or frame_face is None:
            return None
        return float(
            np.dot(reference_face.normed_embedding, frame_face.normed_embedding)
        )
    except Exception as exc:
        print(f"[H3] Face check skipped: {type(exc).__name__}: {exc}", flush=True)
        return None


def _video_motion_profile(video, samples_per_second=2):
    """Measure how much of a clip actually moves.

    A wrong post-processing ramp once turned every two-pass segment into a
    still image with sound; SSIM against the anchors cannot see that (the
    first/last frames can still be wildly different), so the QA report gets an
    explicit "static_video" flag driven by consecutive-frame differences.
    """
    try:
        import cv2
    except ImportError:
        return None
    capture = cv2.VideoCapture(str(video))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
        step = max(1, int(round(fps / max(samples_per_second, 1))))
        prev = None
        diffs = []
        index = 0
        while True:
            if not capture.grab():
                break
            if index % step == 0:
                ok, frame = capture.retrieve()
                if not ok:
                    break
                small = cv2.resize(frame, (96, 96), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype("float32")
                if prev is not None:
                    diffs.append(float(np.abs(gray - prev).mean()))
                prev = gray
            index += 1
    finally:
        capture.release()
    if not diffs:
        return None
    values = np.asarray(diffs, dtype="float32")
    return {
        "samples": int(values.size),
        "median_diff": round(float(np.median(values)), 3),
        "static_ratio": round(float((values < 1.0).mean()), 3),
    }


def build_qa_report(config, state, state_path):
    """Compare decoded segment frames against their anchor keyframes."""
    report_path = state_path.parent / "qa_report.json"
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "available": False,
        "segments": [],
    }
    entries = []
    for record in sorted(
        state.get("segments", []), key=lambda item: item.get("segment", 0)
    ):
        video = Path(record.get("video", ""))
        entry = {
            "segment": record.get("segment"),
            "video": str(video),
        }
        frames = None
        if video.is_file():
            frames = _read_video_frames(video)
            if frames is not None:
                report["available"] = True
        entries.append((entry, frames))
    report["segments"] = [entry for entry, _ in entries]
    if not report["available"]:
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            "[H3] QA skipped: opencv (cv2) is not available in this environment.",
            flush=True,
        )
        return report

    ssim_min = float(config.get("qa_anchor_ssim_min", 0.55))
    face_check = bool(config.get("qa_face_check", False))
    face_min = float(config.get("qa_face_sim_min", 0.50))
    static_ratio_max = float(config.get("qa_static_ratio_max", 0.90))
    reference_path = config["reference_images"][0]
    flagged = []
    for entry, frames in entries:
        segment = entry.get("segment")
        record = next(
            (
                item
                for item in state.get("segments", [])
                if item.get("segment") == segment
            ),
            {},
        )
        if frames is None:
            entry["flags"] = ["frames_unreadable"]
            flagged.append((segment, entry["flags"]))
            continue
        indexes = sorted(frames)
        start_key = record.get("start_keyframe")
        end_key = record.get("end_keyframe")
        if start_key and Path(start_key).is_file():
            import cv2

            anchor = cv2.imread(str(start_key))
            if anchor is not None:
                aligned, frame = _match_anchor_to_frame(
                    anchor, frames[indexes[0]]
                )
                entry["first_frame_ssim"] = compute_ssim(aligned, frame)
        if end_key and Path(end_key).is_file():
            import cv2

            anchor = cv2.imread(str(end_key))
            if anchor is not None:
                aligned, frame = _match_anchor_to_frame(
                    anchor, frames[indexes[-1]]
                )
                entry["last_frame_ssim"] = compute_ssim(aligned, frame)
        if face_check:
            entry["identity_similarity"] = _face_similarity(
                input_image_path(config, reference_path),
                frames[indexes[len(indexes) // 2]],
            )
        flags = []
        first_ssim = entry.get("first_frame_ssim")
        last_ssim = entry.get("last_frame_ssim")
        if first_ssim is not None and first_ssim < ssim_min:
            flags.append("first_frame_drift")
        if last_ssim is not None and last_ssim < ssim_min:
            flags.append("last_frame_drift")
        identity = entry.get("identity_similarity")
        if identity is not None and identity < face_min:
            flags.append("identity_drift")
        motion = _video_motion_profile(Path(entry.get("video", "")))
        if motion:
            entry["motion"] = motion
            if motion["static_ratio"] >= static_ratio_max:
                flags.append("static_video")
        entry["flags"] = flags
        if flags:
            flagged.append((segment, flags))
    report["thresholds"] = {
        "anchor_ssim_min": ssim_min,
        "face_check": face_check,
        "face_sim_min": face_min,
        "static_ratio_max": static_ratio_max,
    }
    report["flagged"] = [
        {"segment": segment, "flags": flags} for segment, flags in flagged
    ]
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if flagged:
        summary = ", ".join(
            f"segment {segment}: {','.join(flags)}" for segment, flags in flagged
        )
        print(f"[H3] QA flagged {summary}; see {report_path}", flush=True)
    else:
        print(f"[H3] QA passed for all segments; see {report_path}", flush=True)
    return report


def _all_segments_current(config, specs, state):
    """True when every segment in state matches its generation signature."""
    records = {
        int(item["segment"]): item
        for item in state.get("segments", [])
        if "segment" in item
    }
    for index, spec in enumerate(specs):
        record = records.get(index + 1)
        if not record or not Path(record.get("video", "")).is_file():
            return False
        expected = generation_signature(
            config, spec, record.get("source_prompt_id", "")
        )
        if record.get("generation_sha1") != expected:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate chained MiniMax H3 long videos.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to a config.json file.")
    parser.add_argument("--limit", type=int, default=0, help="Only render the first N missing segments.")
    parser.add_argument(
        "--from-segment",
        type=int,
        default=1,
        help="1-based index of the first segment to (re)render.",
    )
    parser.add_argument(
        "--to-segment",
        type=int,
        default=0,
        help="1-based index of the last segment to render; 0 means all.",
    )
    parser.add_argument("--no-stitch", action="store_true", help="Do not concatenate the completed segments.")
    parser.add_argument("--force", action="store_true", help="Regenerate completed segments.")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Override config base_seed for this run (segment i uses seed + i, "
            "keyframe drafts use seed + 700000 + 2*i). Without this flag the "
            "base_seed from config.json is reused, so every run is identical."
        ),
    )
    parser.add_argument(
        "--random-seed",
        action="store_true",
        help="Use a fresh random base seed for this run (same behaviour as the Director UI's randomize_seed).",
    )
    parser.add_argument(
        "--stage",
        choices=["all", "keyframes", "videos", "reencode"],
        default="all",
        help=(
            "all: keyframes then videos; keyframes: render the keyframe chain "
            "and stop for human review; videos: reuse the reviewed keyframes "
            "and only render the segment videos; reencode: rebuild the two-pass "
            "post-processing (opening repair + concat) from the raw model "
            "outputs already on disk, without touching ComfyUI."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Build one API workflow without submitting it.")
    parser.add_argument("--dump-graphs", type=Path, help="Write one-pass and two-pass API graph examples.")
    parser.add_argument("--anchor-image", help="Image to use when dumping a two-pass graph example.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    state_path = config_path.parent / ".state.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config(config)
    if args.seed is not None and args.random_seed:
        parser.error("--seed and --random-seed are mutually exclusive.")
    if args.seed is not None:
        config["base_seed"] = int(args.seed)
        print(f"[H3] Using CLI seed override: base_seed={config['base_seed']}", flush=True)
    elif args.random_seed:
        import secrets

        config["base_seed"] = secrets.randbits(50)
        print(f"[H3] Using random base seed: {config['base_seed']}", flush=True)
    specs = segment_specs(config)
    segment_count = len(specs)
    if args.dump_graphs:
        graph_dir = args.dump_graphs.expanduser().resolve()
        graph_dir.mkdir(parents=True, exist_ok=True)
        anchor = args.anchor_image or config.get("opening_frame") or config["reference_images"][0]
        one_pass = build_graph(
            config,
            specs[0]["prompt"],
            None,
            None,
            int(config["base_seed"]),
            specs[0]["seconds"],
            False,
        )
        two_pass = build_graph(
            config,
            specs[0]["prompt"],
            anchor,
            anchor,
            int(config["base_seed"]),
            specs[0]["seconds"],
            True,
        )
        one_pass_path = graph_dir / "segment_one_pass_api.json"
        two_pass_path = graph_dir / "segment_two_pass_api.json"
        one_pass_path.write_text(
            json.dumps(one_pass, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        two_pass_path.write_text(
            json.dumps(two_pass, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(one_pass_path)
        print(two_pass_path)
        return
    if args.dry_run:
        graph = build_graph(
            config,
            specs[0]["prompt"],
            config.get("fixed_first_frame") if config.get("single_clip") else None,
            config.get("fixed_last_frame") if config.get("single_clip") else None,
            int(config["base_seed"]),
            specs[0]["seconds"],
            specs[0]["two_pass"],
        )
        dry_run_path = config_path.parent / "workflow_api.json"
        dry_run_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
        print(dry_run_path)
        return

    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    if config.get("single_clip"):
        segment = run_single_clip(config, specs[0], state_path)
        print(f"[H3] Single clip: {segment['video']}", flush=True)
        return
    if args.stage == "reencode":
        rebuilt = rebuild_two_pass_postprocess(
            config,
            state,
            state_path,
            int(config.get("two_pass_fix_opening_frames", 0)),
        )
        print(
            f"[H3] 后处理重建完成：{rebuilt} 段，接下来只做拼接（不重新渲染）。",
            flush=True,
        )
        # Everything below reuses the records that are already on disk, so the
        # normal stitch/QA path runs without submitting a single new prompt.
        args.stage = "videos"
    segments = state.setdefault("segments", [])
    render_start = max(1, int(args.from_segment or 1))
    render_end = segment_count
    if args.to_segment:
        render_end = min(max(int(args.to_segment), render_start), segment_count)
    if args.limit:
        render_end = min(render_end, args.limit)

    if args.stage == "videos":
        missing = missing_keyframes(state, render_end)
        if missing:
            print(
                "[H3] 关键帧缺失，无法直接进入视频阶段。缺失段："
                + ", ".join(str(index) for index in missing),
                flush=True,
            )
            print("[H3] 请先运行 --stage keyframes 生成并确认关键帧。", flush=True)
            raise SystemExit(2)

    # 关键帧来源不是 H3 时：先确认这个后端到底是「代码出图」还是「只等人工图」。
    # qwen2511 / qwen21 走 build_qwen_keyframe_graph，和 h3 一样自动出图，
    # 只是慢得多（一张一分钟级），所以仍然停在关键帧阶段等人工确认。
    backend = keyframe_backend(config)
    state["keyframe_backend"] = backend
    if backend == BACKEND_MANUAL and args.stage in {"keyframes", "all"}:
        state["stage"] = args.stage
        state["phase"] = "keyframes"
        state["awaiting_review"] = True
        state["review_segments"] = render_end
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"[H3] 关键帧来源 = {backend}：跳过 AI 出图，{render_end} 段等人工放图。",
            flush=True,
        )
        print(
            "[H3] 出好图后在面板上逐张「替换」（或 push 灌进来），"
            "再点「确认关键帧，生成视频」。",
            flush=True,
        )
        return
    if backend in QWEN_BACKENDS and args.stage in {"keyframes", "all"}:
        if backend == BACKEND_QWEN21:
            size_text = "按图1比例 × 1.05MP（跟着参考图走，不再裁成 9:16）"
        else:
            width, height = keyframe_size(config)
            size_text = f"固定 {width}x{height}"
        print(
            f"[H3] 关键帧来源 = {backend}：用 Qwen 出图，"
            f"{size_text}，共 {render_end} 段。",
            flush=True,
        )

    keyframes = ensure_keyframe_chain(
        config,
        specs,
        state,
        state_path,
        max_segments=render_end,
        force=args.force,
    )
    state["stage"] = args.stage
    if args.stage == "keyframes":
        state["phase"] = "keyframes"
        state["awaiting_review"] = True
        state["review_segments"] = render_end
        state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"[H3] 关键帧阶段完成：{len(keyframes)}/{render_end} 段，等待人工确认。",
            flush=True,
        )
        for item in keyframes:
            print(
                f"[H3]   段 {item['segment']}  "
                f"首帧({item.get('start_source', 'ai')}) {item['start_frame']}",
                flush=True,
            )
            print(
                f"[H3]          "
                f"尾帧({item.get('end_source', 'ai')}) {item['end_frame']}",
                flush=True,
            )
        return
    state["awaiting_review"] = False
    state["phase"] = "videos"
    state["keyframe_total"] = render_end
    transition_mode = str(config.get("transition_mode", "fade")).lower()
    required_transitions = {
        index + 1
        for index, keyframe in enumerate(keyframes)
        if (
            transition_mode == "generated"
            and index > 0
            and not keyframe.get("continue_from_previous", True)
        )
    }
    state["transitions"] = [
        item
        for item in state.get("transitions", [])
        if int(item.get("to_segment", -1)) in required_transitions
    ]
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    playlist = []
    transitions = state.setdefault("transitions", [])
    for index in range(render_start - 1, render_end):
        keyframe = keyframes[index]
        spec = specs[index]
        if (
            transition_mode == "generated"
            and index > 0
            and not keyframe.get("continue_from_previous", True)
            and (keyframes[index - 1].get("end_frame") and keyframe.get("start_frame"))
        ):
            previous_keyframe = keyframes[index - 1]
            transition_spec = {
                "seconds": specs[index]["transition_seconds"],
                "two_pass": bool(config.get("transition_two_pass", False)),
                "prompt": specs[index]["transition_prompt"],
            }
            transition_source = (
                f"{previous_keyframe.get('end_prompt_id')}:"
                f"{keyframe.get('start_prompt_id')}"
            )
            transition_signature = generation_signature(
                config,
                transition_spec,
                transition_source,
            )
            existing_transition = next(
                (
                    item
                    for item in transitions
                    if int(item.get("to_segment", -1)) == index + 1
                ),
                None,
            )
            if (
                existing_transition
                and not args.force
                and existing_transition.get("video")
                and Path(existing_transition["video"]).is_file()
                and existing_transition.get("generation_sha1")
                == transition_signature
                and existing_transition.get("source_prompt_id")
                == transition_source
            ):
                playlist.append(
                    (existing_transition["video"], transition_spec["seconds"])
                )
            else:
                transition_record = run_segment(
                    config,
                    transition_spec,
                    index,
                    previous_keyframe["end_frame"],
                    keyframe["start_frame"],
                    seed=int(config["base_seed"]) + 800000 + index,
                    log_label=f"Transition to segment {index + 1}",
                )
                transition_record["to_segment"] = index + 1
                transition_record["source_prompt_id"] = transition_source
                transition_record["generation_sha1"] = transition_signature
                transitions = [
                    item
                    for item in transitions
                    if int(item.get("to_segment", -1)) != index + 1
                ]
                transitions.append(transition_record)
                transitions.sort(key=lambda item: item["to_segment"])
                state["transitions"] = transitions
                state_path.write_text(
                    json.dumps(state, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                playlist.append(
                    (transition_record["video"], transition_spec["seconds"])
                )

        existing = next((x for x in segments if x.get("segment") == index + 1), None)
        source_key = (
            f"{keyframe.get('start_prompt_id')}:"
            f"{keyframe.get('end_prompt_id')}"
        )
        video_spec = video_prompt_spec(config, specs[index])
        expected_signature = generation_signature(
            config,
            video_spec,
            source_key,
        )
        if (
            existing
            and not args.force
            and Path(existing["video"]).exists()
            and existing.get("generation_sha1") == expected_signature
            and existing.get("source_prompt_id") == source_key
        ):
            playlist.append((existing["video"], spec["seconds"]))
            continue
        # A segment that follows a ``free_end`` one has no stored start frame:
        # take the previous render's last frame (that is exactly what the free
        # end produced) instead of failing or regenerating a draft.
        resolved_start = keyframe.get("start_frame")
        if not resolved_start:
            resolved_start = previous_video_tail(state, index + 1)
            if not resolved_start:
                raise RuntimeError(
                    f"段 {index + 1} 的首帧要等上一段渲染完成后才能确定；"
                    "请按顺序渲染（先用 --from-segment 补上一段），不要跳过。"
                )
            print(
                f"[H3] Segment {index + 1}: start frame resolved from the "
                f"previous render: {resolved_start}",
                flush=True,
            )
        segment = run_segment(
            config,
            video_spec,
            index,
            resolved_start,
            keyframe.get("end_frame"),
        )
        segment["source_prompt_id"] = source_key
        segment["generation_sha1"] = expected_signature
        segment["start_keyframe"] = resolved_start
        segment["end_keyframe"] = keyframe.get("end_frame")
        segments = [x for x in segments if x.get("segment") != index + 1]
        segments.append(segment)
        segments.sort(key=lambda x: x["segment"])
        state["segments"] = segments
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        playlist.append((segment["video"], spec["seconds"]))

    try:
        build_qa_report(config, state, state_path)
    except Exception as exc:
        print(f"[H3] QA report failed: {type(exc).__name__}: {exc}", flush=True)

    if args.no_stitch:
        return
    if not _all_segments_current(config, specs, state):
        print(
            f"[H3] Rendered segments {render_start}-{render_end}; "
            "the final video will be created once every segment matches "
            "its current configuration.",
            flush=True,
        )
        return
    if playlist:
        # 先剪掉每段尾部的定格等待，再按剪完的实际时长算转场/音频边界。
        playlist = compact_static(config, playlist, config_path.parent)
        fade_boundaries = []
        if transition_mode == "fade":
            elapsed = 0.0
            for index, (_, seconds) in enumerate(playlist[:-1]):
                elapsed += float(seconds)
                if index + 1 >= len(specs):
                    break
                next_spec = specs[index + 1]
                if not should_continue_from_previous(config, next_spec):
                    fade_boundaries.append(
                        (elapsed, next_spec["transition_seconds"])
                    )
        audio_fade = float(config.get("audio_boundary_fade", 0.1))
        audio_boundaries = []
        if audio_fade > 0:
            elapsed = 0.0
            for path, seconds in playlist[:-1]:
                elapsed += float(seconds)
                if not any(
                    abs(elapsed - boundary) < 0.01
                    for boundary, _ in fade_boundaries
                ):
                    audio_boundaries.append((elapsed, audio_fade))
        output = stitch_segments(
            config,
            playlist,
            config_path.parent,
            total_seconds=sum(float(seconds) for _, seconds in playlist),
            fade_boundaries=fade_boundaries,
            audio_fade_boundaries=audio_boundaries,
        )
        print(f"[H3] Final video: {output}", flush=True)


if __name__ == "__main__":
    main()
