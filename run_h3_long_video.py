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
GRAPH_VERSION = 3


def h3_frame_count(seconds):
    requested = max(5, math.ceil(float(seconds) * 24))
    while requested % 17 != 5:
        requested += 1
    return requested


def output_frames(seconds):
    return max(1, round(float(seconds) * 24))


def segment_specs(config):
    segments = config.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("config.json must contain a non-empty segments array")
    specs = []
    for index, segment in enumerate(segments):
        prompt = str(segment.get("prompt", "")).strip()
        seconds = float(segment.get("seconds", 0))
        if not prompt:
            raise RuntimeError(f"Segment {index + 1} has an empty prompt")
        if seconds <= 0:
            raise RuntimeError(f"Segment {index + 1} has an invalid duration")
        specs.append(
            {
                "seconds": seconds,
                "two_pass": bool(segment.get("two_pass", config.get("two_pass_default", False))),
                "prompt": prompt,
            }
        )
    return specs


def validate_config(config):
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


def find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise RuntimeError("ffmpeg was not found; install it or install imageio-ffmpeg") from exc
    return ffmpeg


def extract_first_frame(video, output):
    ffmpeg = find_ffmpeg()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video),
            "-vf",
            "select=eq(n\\,0)",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(output),
        ],
        check=True,
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
            (
                f"[0:v]scale={width}:{height},setsar=1,"
                f"trim=end_frame={fix_frames},setpts=PTS-STARTPTS[a];"
                f"[1:v]trim=start_frame={fix_frames},setpts=PTS-STARTPTS[b];"
                "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]"
            ),
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


def ensure_opening_frame(config, specs, state, state_path):
    draft_seconds = float(config.get("opening_draft_seconds", 2.0))
    draft_spec = {
        "seconds": draft_seconds,
        "two_pass": False,
        "prompt": specs[0]["prompt"],
    }
    signature = generation_signature(config, draft_spec, None)
    existing = state.get("opening", {})
    existing_frame = existing.get("frame")
    if (
        existing.get("draft_sha1") == signature
        and existing_frame
        and Path(existing_frame).is_file()
    ):
        print(f"[H3] Reusing generated opening frame: {existing_frame}", flush=True)
        return existing_frame

    record = run_segment(
        config,
        draft_spec,
        0,
        None,
        None,
        seed=int(config["base_seed"]) + 900000,
        log_label="Generated opening",
    )
    output = (
        Path(config.get("final_output_dir", config["comfy_output_dir"]))
        / config["output_prefix"]
        / f"opening_{signature[:12]}.png"
    )
    extract_first_frame(record["video"], output)
    state["opening"] = {
        "prompt_id": record["prompt_id"],
        "video": record["video"],
        "frame": str(output),
        "draft_sha1": signature,
    }
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[H3] Generated full-body opening frame: {output}", flush=True)
    return str(output)


def stitch_segments(config, videos, work_dir=HERE):
    ffmpeg = find_ffmpeg()
    concat_file = Path(work_dir) / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{Path(path).as_posix()}'\n" for path in videos),
        encoding="utf-8",
    )
    output = Path(config.get("final_output_dir", config["comfy_output_dir"])) / config["output_prefix"] / "long_video.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    total_seconds = sum(float(item["seconds"]) for item in config["segments"])
    width = int(config.get("final_width", 704))
    height = int(config.get("final_height", 1216))
    subprocess.run(
        [
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
            f"scale={width}:{height}:flags=lanczos",
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
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
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

    first_frame = config.get("opening_frame") or None
    previous_prompt_id = None
    rendered_this_run = 0
    valid_video_paths = []
    for index in range(segment_count):
        existing = next((x for x in segments if x.get("segment") == index + 1), None)
        expected_signature = generation_signature(config, specs[index], previous_prompt_id)
        if (
            existing
            and not args.force
            and Path(existing["video"]).exists()
            and existing.get("generation_sha1") == expected_signature
            and existing.get("source_prompt_id") == previous_prompt_id
        ):
            first_frame = existing["next_frame"]
            previous_prompt_id = existing["prompt_id"]
            valid_video_paths.append(existing["video"])
            continue
        if args.limit and rendered_this_run >= args.limit:
            break
        if (
            index == 0
            and first_frame is None
            and config.get("stabilize_full_body")
        ):
            first_frame = ensure_opening_frame(config, specs, state, state_path)
        last_frame = first_frame if config.get("stabilize_full_body") and first_frame else None
        segment = run_segment(config, specs[index], index, first_frame, last_frame)
        segment["source_prompt_id"] = previous_prompt_id
        segment["generation_sha1"] = expected_signature
        segments = [x for x in segments if x.get("segment") != index + 1]
        segments.append(segment)
        segments.sort(key=lambda x: x["segment"])
        state["segments"] = segments
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        first_frame = segment["next_frame"]
        previous_prompt_id = segment["prompt_id"]
        valid_video_paths.append(segment["video"])
        rendered_this_run += 1

    if args.no_stitch:
        return
    if len(valid_video_paths) != segment_count:
        print(
            f"[H3] {len(valid_video_paths)}/{segment_count} segments are ready; "
            "the final video will be created after the remaining chain is rendered.",
            flush=True,
        )
        return
    if valid_video_paths:
        output = stitch_segments(config, valid_video_paths, config_path.parent)
        print(f"[H3] Final video: {output}", flush=True)


if __name__ == "__main__":
    main()
