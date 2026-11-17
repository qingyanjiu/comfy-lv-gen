#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video_remake.py — 视频复刻流水线（黑鹤二采链路）

输入一个视频，按固定秒数切成若干段；每段用：
  - 该段首帧  → 图片参考（ref_images 身份锚）+ first_frame_override（frame0 硬锚）
  - 该段切片  → ref_video（动作节奏/镜头运动的软参考）
  - remake_prompt.txt → 提示词（人物身份只认图、动作只认视频、画风只认提示词）
逐段用黑鹤链路复刻，最后 merge 拼成完整视频。

用法：
  python video_remake.py "D:/path/to/source.mp4" [--seg 5] [--prompt D:/.../custom.txt]
                          [--out D:/.../output] [--dry-run] [--exact-seg] [--fps-mode]
  --dry-run   只切段+抽帧+建 cfg，不调用 ComfyUI 生成（先看切段对不对）
  --exact-seg 固定秒数切段、尾部不足一段丢弃；默认均匀重切不丢内容
              （11s 默认→2段×5.5s 不丢；加此开关→2段×5s 丢末尾1s）
  --fps-mode  仅日志标记：切段始终按真实时间切，输出统一按 24fps/H3帧数(17n+5)生成，
              参考视频由 to_api 强制 force_rate=24（源视频30/60fps 都按24fps喂，不会慢放）

机制依据（run_selflift.py）：
  - build_wf: REF_VIDEO_PATH = seg.get("ref_video") or cfg.get("ref_video")
  - to_api_blackcrane: ref_videos.ref_video_0 ← VHS_LoadVideo(video=basename, force_rate=24)
  - 黑鹤首帧由 MiniMaxH3AddGuide 在 frame0 硬锚（seg["first_frame_override"]）
  - seg["ref_video"] 必须是已在 ComfyUI input 里的文件名
"""
import os, sys, json, argparse, shutil, subprocess, time, glob

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
REPO = "D:/Comfy-Desktop/ComfyUI-Installs/h3-long-video-repo"
RUNNER = os.path.join(HERE, "run_selflift.py")
DEFAULT_PROMPT = os.path.join(SKILL, "assets", "remake_prompt.txt")
BLACKCRANE_BASE = os.path.join(SKILL, "assets", "H3-SelfLift_黑鹤二采.base.json")
COMFY_INPUT = "D:/Comfy-Desktop/ComfyUI-Shared/input"
FFMPEG = "D:/Comfy-Desktop/tools/ffmpeg/bin/ffmpeg"
FFPROBE = "D:/Comfy-Desktop/tools/ffmpeg/bin/ffprobe"
PY = sys.executable
DEF_SEG = 5
FPS = 24


def log(*a):
    print("[remake]", *a, flush=True)


def run(cmd, capture=False):
    if capture:
        return subprocess.run(cmd, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.strip()
    return subprocess.run(cmd, check=True)


def probe(path):
    out = run([FFPROBE, "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,width,height,r_frame_rate",
               "-of", "json", path], capture=True)
    d = json.loads(out)
    dur = float(d["format"]["duration"])
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    w, h = v["width"], v["height"]
    num, den = v.get("r_frame_rate", "24/1").split("/")
    fps = float(num) / float(den) if float(den) else 24.0
    return dur, w, h, fps


def split_video(src, work, seg_sec, fps_mode, exact=False):
    """切段；返回 ([(片路径, 首帧png路径), ...], (dur,w,h,srcfps), 实际每段秒数)。

    两种模式：
    - 默认（均匀重切）：n=round(dur/seg_sec)，每段 dur/n 秒，不丢内容。
      例 11s/5s → 2段×5.5s。段数靠四舍五入到最近整数，段长在 seg_sec 附近浮动。
    - exact（--exact-seg）：n=floor(dur/seg_sec)，每段严格 seg_sec 秒，尾部不足一段丢弃。
      例 11s/5s → 2段×5s，末尾1s丢弃。
    cfg.seconds 是全局的、所有段统一秒数，所以这两种模式都保证每段等长。
    """
    segdir = os.path.join(work, "source_segments")
    refdir = os.path.join(work, "refs")
    os.makedirs(segdir, exist_ok=True)
    os.makedirs(refdir, exist_ok=True)
    dur, w, h, srcfps = probe(src)

    if exact:
        n = max(1, int(dur // seg_sec))
        actual = float(seg_sec)
        tail = dur - n * actual
        mode_note = f"固定{seg_sec}s" + (f"（末尾 {tail:.2f}s 不足一段，已丢弃）" if tail > 0.15 else "")
    else:
        n = max(1, int(round(dur / seg_sec)))
        actual = dur / n
        mode_note = f"均匀重切（目标{seg_sec}s，实际每段 {actual:.2f}s，不丢内容）"

    # H3 生成帧数按 17n+5 对齐，实际成片每段时长 = length/fps，可能略长于切片时长
    gen_frames = 17 * round(actual * FPS / 17) + 5
    gen_sec = gen_frames / FPS
    log(f"源视频 {dur:.2f}s / {w}x{h} / {srcfps:.2f}fps → 切 {n} 段 × {actual:.2f}s（{mode_note}）")
    if abs(gen_sec - actual) > 0.05:
        log(f"  注：H3 帧数对齐 17n+5 → 成片每段 {gen_frames}帧={gen_sec:.3f}s"
            f"（比切片 {actual:.2f}s {'长' if gen_sec>actual else '短'} {abs(gen_sec-actual):.3f}s，模型约束所致）")

    pairs = []
    for i in range(n):
        t0 = i * actual
        # 最后一段用 -sseof 之外的方式：按实际剩余时长，避免四舍五入溢出源时长
        this_dur = min(actual, dur - t0)
        clip = os.path.join(segdir, f"clip_{i+1:02d}.mp4")
        first = os.path.join(refdir, f"first_{i+1:02d}.png")
        # 精确切段：重编码保证每段从关键帧开始、时长准确
        run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t0:.3f}",
             "-i", src, "-t", f"{this_dur:.3f}",
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-c:v", "libx264", "-crf", "18", "-preset", "fast",
             "-an", "-movflags", "+faststart", clip])
        run([FFMPEG, "-y", "-loglevel", "error", "-i", clip,
             "-frames:v", "1", first])
        pairs.append((clip, first))
        log(f"  段{i+1}: {os.path.basename(clip)} + {os.path.basename(first)}")
    return pairs, (dur, w, h, srcfps), actual


def build_cfg(work, prefix, title, pairs, prompt, seg_sec, srcfps, fps_mode):
    """构造黑鹤链路的 segments.json，每段挂首帧+切片参考。"""
    segs = []
    for i, (clip, first) in enumerate(pairs):
        vname = f"{prefix}_clip_{i+1:02d}.mp4"
        iname = f"{prefix}_first_{i+1:02d}.png"
        shutil.copy(clip, os.path.join(COMFY_INPUT, vname))
        shutil.copy(first, os.path.join(COMFY_INPUT, iname))
        seg = {
            "id": f"r{i+1:02d}",
            "title": f"复刻 段{i+1}",
            "seed": 300000 + i * 7919 + int(time.time()) % 100000,
            "body": prompt,
            "cast": "",
            "ref_images": [{"path": first, "comfy_name": iname,
                            "label": f"人物参考（段{i+1}首帧）"}],
            "first_frame_override": iname,
            "ref_video": vname,
            "ref_video_seconds": seg_sec,
            "link": {"mode": "transition", "transition": "cut"},
        }
        segs.append(seg)
    cfg = {
        "title": title,
        "prefix": prefix,
        "film_id": prefix.split("_", 1)[-1] if "_" in prefix else prefix,
        "final_name": f"{title}_复刻成片.mp4",
        "workflow": BLACKCRANE_BASE,
        "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "fps": FPS,
        "seconds": float(seg_sec),
        "length": int(17 * round(seg_sec * FPS / 17) + 5),  # 与 run_selflift 帧数规则一致（17n+5）
        "megapixels": 0.3,
        "aspect_ratio": "16:9 (Widescreen)",
        "quality": "high",
        "audio": "off",
        "chain_first_frame": False,
        "join_mode": "cut",
        "head": "",
        "tail": "",
        "segments": segs,
    }
    path = os.path.join(work, "segments.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="源视频路径")
    ap.add_argument("--seg", type=float, default=DEF_SEG, help="每段秒数（默认5）")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="提示词文本文件")
    ap.add_argument("--out", default=None, help="输出根目录（默认 output/）")
    ap.add_argument("--dry-run", action="store_true", help="只切段+建cfg，不生成")
    ap.add_argument("--exact-seg", action="store_true",
                    help="固定秒数切段、尾部不足一段丢弃（默认均匀重切不丢内容）")
    ap.add_argument("--fps-mode", action="store_true", help="按源fps换算（默认24）")
    ap.add_argument("--title", default="视频复刻", help="片名")
    args = ap.parse_args()

    src = os.path.abspath(args.video)
    if not os.path.isfile(src):
        log("✗ 源视频不存在：", src); sys.exit(1)
    if not os.path.isfile(args.prompt):
        log("✗ 提示词文件不存在：", args.prompt); sys.exit(1)
    with open(args.prompt, encoding="utf-8") as f:
        prompt = f.read().strip()
    if "<Picture {pic:1}>" not in prompt:
        log("⚠ 提示词里没找到 <Picture {pic:1}>，请确认用的是 remake_prompt.txt 模板")

    root = args.out or "D:/Comfy-Desktop/output"
    stamp = time.strftime("%m%d_%H%M%S")
    prefix = f"rmk_{stamp}"
    work = os.path.join(root, f"{args.title}_{stamp}")
    os.makedirs(work, exist_ok=True)
    log("工作目录：", work)

    pairs, info, actual_sec = split_video(src, work, args.seg, args.fps_mode,
                                          exact=args.exact_seg)
    cfg_path = build_cfg(work, prefix, args.title, pairs, prompt,
                         actual_sec, info[3], args.fps_mode)
    log("配置已写：", cfg_path, f"（{len(pairs)} 段）")

    if args.dry_run:
        log("=== dry-run 完成，未调用 ComfyUI。检查 source_segments/ 与 segments.json 后可正式跑 ===")
        return

    env = os.environ.copy()
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    for i in range(len(pairs)):
        log(f"===== 复刻 段 {i+1}/{len(pairs)} =====")
        run([PY, RUNNER, "seg", str(i + 1), "--cfg", cfg_path], env=env)
    log("===== 全部段完成，开始拼接 =====")
    run([PY, RUNNER, "merge", "--cfg", cfg_path], env=env)
    log("===== 完成 =====")
    final = os.path.join(work, "final", f"{args.title}_复刻成片.mp4")
    log("成片：", final if os.path.exists(final) else "见 final/ 目录")


if __name__ == "__main__":
    main()
