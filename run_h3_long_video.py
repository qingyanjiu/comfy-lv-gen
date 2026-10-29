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
from pathlib import Path

import requests


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
GRAPH_VERSION = 5


def h3_frame_count(seconds):
    requested = max(5, math.ceil(float(seconds) * 24))
    while requested % 17 != 5:
        requested += 1
    return requested


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
        specs.append(
            {
                "seconds": seconds,
                "two_pass": bool(segment.get("two_pass", config.get("two_pass_default", False))),
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


def generation_signature(config, segment, source_prompt_id):
    ignored = {
        "comfy_url",
        "comfy_input_dir",
        "chain_input_dir",
        "comfy_output_dir",
        "final_output_dir",
        "output_prefix",
        "segments",
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
    generated_frames = h3_frame_count(seconds)
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
            "5",
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
        h3_inputs["last_frame"] = ["5", 0]
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
    generated_frames = h3_frame_count(seconds)
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


def wait_for_prompt(base_url, prompt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = requests.get(f"{base_url}/history/{prompt_id}", timeout=30)
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
    response = requests.post(
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


def run_keyframe(config, segment, seed, first_frame=None, prompt=None, label=None):
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
    return {
        "prompt_id": prompt_id,
        "first_frame": str(resolve_output(config, first_images[0])),
        "last_frame": str(resolve_output(config, last_images[0])),
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
    filter_complex = build_two_pass_repair_filter(width, height, fix_frames)
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


def build_two_pass_repair_filter(width, height, fix_frames):
    return (
        f"[0:v]scale={width}:{height},setsar=1,"
        f"trim=end_frame={fix_frames},setpts=PTS-STARTPTS[a];"
        f"[1:v]scale={width}:{height},setsar=1,"
        f"trim=start_frame={fix_frames},setpts=PTS-STARTPTS[b];"
        "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"
    )


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

        if index == 0 or not continues_from_previous:
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
                    int(config["base_seed"]) + 900000,
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

        hold_composition = should_anchor_last_frame(config, spec)
        if hold_composition:
            end_frame = start_frame
            end_prompt_id = start_prompt_id
            end_signature = f"hold:{start_signature}"
        elif (
            index == 0
            and spec["end_frame_prompt"] == spec["prompt"]
            and opening_draft_last_frame
            and Path(opening_draft_last_frame).is_file()
        ):
            end_frame = opening_draft_last_frame
            end_prompt_id = opening_draft_prompt_id
            end_signature = f"opening-draft:{start_signature}"
            print(
                f"[H3] Reusing opening draft end keyframe: {end_frame}",
                flush=True,
            )
        else:
            end_spec = {
                "seconds": draft_seconds,
                "two_pass": False,
                "prompt": spec["end_frame_prompt"],
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
                    int(config["base_seed"]) + 700000 + index * 2,
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
    videos,
    work_dir=HERE,
    total_seconds=None,
    fade_boundaries=None,
):
    ffmpeg = find_ffmpeg()
    concat_file = Path(work_dir) / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{Path(path).as_posix()}'\n" for path in videos),
        encoding="utf-8",
    )
    output = Path(config.get("final_output_dir", config["comfy_output_dir"])) / config["output_prefix"] / "long_video.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    if total_seconds is None:
        total_seconds = sum(float(item["seconds"]) for item in config["segments"])
    width = int(config.get("final_width", 704))
    height = int(config.get("final_height", 1216))
    fade_boundaries = fade_boundaries or []
    video_fade_filters, audio_fade_filters = build_fade_filters(fade_boundaries)
    video_filter = f"scale={width}:{height}:flags=lanczos"
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
    return output


def main():
    parser = argparse.ArgumentParser(description="Generate chained MiniMax H3 long videos.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to a config.json file.")
    parser.add_argument("--limit", type=int, default=0, help="Only render the first N missing segments.")
    parser.add_argument("--no-stitch", action="store_true", help="Do not concatenate the completed segments.")
    parser.add_argument("--force", action="store_true", help="Regenerate completed segments.")
    parser.add_argument("--dry-run", action="store_true", help="Build one API workflow without submitting it.")
    parser.add_argument("--dump-graphs", type=Path, help="Write one-pass and two-pass API graph examples.")
    parser.add_argument("--anchor-image", help="Image to use when dumping a two-pass graph example.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    state_path = config_path.parent / ".state.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config(config)
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
            None,
            None,
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
    segments = state.setdefault("segments", [])
    render_count = segment_count
    if args.limit:
        render_count = min(render_count, args.limit)

    keyframes = ensure_keyframe_chain(
        config,
        specs,
        state,
        state_path,
        max_segments=render_count,
        force=args.force,
    )
    state["phase"] = "videos"
    state["keyframe_total"] = render_count
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

    playlist_paths = []
    transitions = state.setdefault("transitions", [])
    completed_segments = 0
    for index, keyframe in enumerate(keyframes):
        if (
            transition_mode == "generated"
            and index > 0
            and not keyframe.get("continue_from_previous", True)
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
                playlist_paths.append(existing_transition["video"])
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
                playlist_paths.append(transition_record["video"])

        existing = next((x for x in segments if x.get("segment") == index + 1), None)
        source_key = (
            f"{keyframe.get('start_prompt_id')}:"
            f"{keyframe.get('end_prompt_id')}"
        )
        expected_signature = generation_signature(
            config,
            specs[index],
            source_key,
        )
        if (
            existing
            and not args.force
            and Path(existing["video"]).exists()
            and existing.get("generation_sha1") == expected_signature
            and existing.get("source_prompt_id") == source_key
        ):
            playlist_paths.append(existing["video"])
            completed_segments += 1
            continue
        segment = run_segment(
            config,
            specs[index],
            index,
            keyframe["start_frame"],
            keyframe["end_frame"],
        )
        segment["source_prompt_id"] = source_key
        segment["generation_sha1"] = expected_signature
        segment["start_keyframe"] = keyframe["start_frame"]
        segment["end_keyframe"] = keyframe["end_frame"]
        segments = [x for x in segments if x.get("segment") != index + 1]
        segments.append(segment)
        segments.sort(key=lambda x: x["segment"])
        state["segments"] = segments
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        playlist_paths.append(segment["video"])
        completed_segments += 1

    if args.no_stitch:
        return
    if completed_segments != segment_count:
        print(
            f"[H3] {completed_segments}/{segment_count} segments are ready; "
            "the final video will be created after the remaining chain is rendered.",
            flush=True,
        )
        return
    if playlist_paths:
        total_seconds = sum(spec["seconds"] for spec in specs)
        fade_boundaries = []
        if transition_mode == "generated":
            total_seconds += sum(
                spec["transition_seconds"]
                for index, spec in enumerate(specs)
                if index > 0 and not should_continue_from_previous(config, spec)
            )
        elif transition_mode == "fade":
            elapsed = 0.0
            for index, spec in enumerate(specs[:-1]):
                elapsed += spec["seconds"]
                next_spec = specs[index + 1]
                if not should_continue_from_previous(config, next_spec):
                    fade_boundaries.append(
                        (elapsed, next_spec["transition_seconds"])
                    )
        output = stitch_segments(
            config,
            playlist_paths,
            config_path.parent,
            total_seconds=total_seconds,
            fade_boundaries=fade_boundaries,
        )
        print(f"[H3] Final video: {output}", flush=True)


if __name__ == "__main__":
    main()
