#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐帧精确复刻（frame-exact restyle）——h3-long-video 的第四条链路。

与另三条的区别（2026-11-09 定稿）
- `frames`（普通生成）：人物参考图 + 无人空镜 → 首尾帧 → H3 生成运动 → merge。
- `source_frames`（首尾帧复刻）：原片 first/last 各重绘 → H3 插值出中间帧。
- `t2i`：废弃实验链路。
- **`frame_exact`（本模块）**：原片**每一帧**各重绘一次，中间不交给任何视频模型。
  构图 / 机位 / 动作 / 光效 / 画幅 / fps / 帧数 / 时长 / 音轨全部继承源片。

因此它必须绕开 skill 的几项硬规则，别把普通链路的闸门套进来：
- 9:16 硬锁（`normalize_916` / `outpaint_916` / 提示词模板）→ 画幅跟源片。
- 5s 段长下限、17n+5 帧阶梯 → 帧数 = 源片该时间段的真实帧数。
- `frames` / `push` / `video` 三阶段 → 本链路直接产出成片，`merge` 只在分刀生成时才用。

**单一实现**：capsule-cinema 的 `LocalFrameExactRestyleTool` 只做参数校验并调用
`run(cfg)`，不要在那个 skill 里再写一份逐帧逻辑。

用法
  pipeline.py frame_exact --storyboard sb.json        # 分镜里写 frame_exact 配置
  python frame_exact.py --params params.json          # 直接调用
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pipeline as P  # noqa: E402
import source_frames as SF  # noqa: E402

FFMPEG = Path("D:/Comfy-Desktop/tools/ffmpeg/bin/ffmpeg.exe")
FFPROBE = Path("D:/Comfy-Desktop/tools/ffmpeg/bin/ffprobe.exe")
if not FFMPEG.is_file():
    FFMPEG = Path(shutil.which("ffmpeg") or "ffmpeg")
    FFPROBE = Path(shutil.which("ffprobe") or "ffprobe")

DEFAULT_BOX = [0.08, 0.04, 0.84, 0.92]

# 2026-11-09 用户反馈：逐帧独立扩散时，把性别判断丢给模型看参考图会让人一会儿偏男
# 一会儿偏女。所以身份锚必须**显式写死性别**，且放在提示词最前面（Qwen 对前段更敏感）。
# 每帧都用同一段身份文字；跨角色的连续段用 characters 按时间切，段内仍然恒定。
IDENTITY_RULE = (
    "画面里的人物身份固定：{identity}。"
    "整段每一帧都必须是同一个人，性别、年龄、发型、发色、服装配色与配饰一律不得改变；"
    "不要把男性画成女性，也不要把女性画成男性，不要增减人物。"
)
DEFAULT_IDENTITY = (
    "保持参考图里这位人物的性别、年龄与造型不变（整段固定，不得改变性别）"
)
DEFAULTS = {
    "prompt": SF.DEFAULT_PROMPT,
    "identity": None,          # 整段统一的人物锚；强烈建议显式写"男性/女性"
    "characters": None,        # [{"start":..,"end":..,"identity":..}] 按帧时间覆盖
    "seed": 20261109,
    "workflow": "",
    "mode": "full",
    "person_box": None,
    "temporal_smooth": 0.0,
    "max_frames": 80,
    "fps": None,
    "keep_audio": True,
}


def _identity_for(t: float, cfg: dict) -> str:
    """按帧时间选身份锚；命中 characters 用它，否则用整段 identity。"""
    for c in cfg.get("characters") or []:
        try:
            if float(c["start"]) <= t < float(c["end"]):
                return str(c["identity"])
        except (KeyError, TypeError, ValueError):
            continue
    return str(cfg.get("identity") or DEFAULT_IDENTITY)


def _prompt_for_frame(t: float, cfg: dict) -> str:
    """身份锚放最前，再接画风提示词。同角色段内每帧文字完全相同。"""
    return IDENTITY_RULE.format(identity=_identity_for(t, cfg)) + cfg["prompt"]


def _probe(src: Path) -> dict:
    r = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries",
         "stream=width,height,r_frame_rate,codec_type:format=duration",
         "-of", "json", str(src)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffprobe failed: {(r.stderr or '')[-200:]}")
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s.get("codec_type") == "video")
    num, _, den = (v.get("r_frame_rate") or "30/1").partition("/")
    return {"width": int(v["width"]), "height": int(v["height"]),
            "fps": float(num) / float(den or 1),
            "duration": float(d.get("format", {}).get("duration") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in d["streams"])}


def _extract(src: Path, start: float, dur: float, outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    for p in outdir.glob("f_*.png"):
        p.unlink()
    r = subprocess.run(
        [str(FFMPEG), "-y", "-v", "error", "-i", str(src),
         "-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-fps_mode", "passthrough",
         str(outdir / "f_%04d.png")], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"frame extraction failed: {(r.stderr or '')[-250:]}")
    frames = sorted(outdir.glob("f_*.png"))
    if not frames:
        raise RuntimeError("frame extraction produced no frames")
    return frames


def _restyle(frames: list[Path], prompts: list[str], seed: int, wf: str,
             size: tuple[int, int], tag: str) -> list[np.ndarray]:
    """每帧一次 ComfyUI 任务；模型 / seed / 预处理完全一致，身份锚按帧给定但段内恒定。"""
    from PIL import Image

    out: list[np.ndarray] = []
    w, h = size
    for i, f in enumerate(frames):
        img_name = SF._stage(f)
        got = SF._run_one(wf, {}, img_name, prompts[i], seed, f"fex_{tag}_{i:04d}")
        im = Image.open(got).convert("RGB").resize((w, h), Image.LANCZOS)
        out.append(np.asarray(im, dtype=np.uint8)[:, :, ::-1].copy())
        print(f"[frame_exact] {i + 1}/{len(frames)} restyled", flush=True)
    return out


def _bg_lock(out: list[np.ndarray], src: list[np.ndarray], box: list[float]) -> None:
    h, w = out[0].shape[:2]
    x, y, bw, bh = box
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = min(w, int((x + bw) * w)), min(h, int((y + bh) * h))
    mask = np.zeros((h, w), np.float32)
    mask[y0:y1, x0:x1] = 1.0
    mask = cv2.GaussianBlur(mask, (0, 0), max(6.0, w * 0.012))[:, :, None]
    for i in range(len(out)):
        out[i] = (out[i].astype(np.float32) * mask +
                  src[i].astype(np.float32) * (1 - mask)).clip(0, 255).astype(np.uint8)


def _smooth(seq: list[np.ndarray], amount: float) -> None:
    if amount <= 0:
        return
    orig = [f.copy() for f in seq]
    for i in range(len(seq)):
        a = orig[max(0, i - 1)].astype(np.float32)
        b = orig[i].astype(np.float32)
        c = orig[min(len(seq) - 1, i + 1)].astype(np.float32)
        seq[i] = ((1 - 2 * amount) * b + amount * a + amount * c).clip(0, 255).astype(np.uint8)


def _align(a: np.ndarray, b: np.ndarray) -> dict:
    """源帧 vs 重绘帧的结构对齐。"""
    ga = cv2.cvtColor(cv2.resize(a, (320, 180)), cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(cv2.resize(b, (320, 180)), cv2.COLOR_BGR2GRAY)
    va, vb = ga.astype(np.float32).ravel(), gb.astype(np.float32).ravel()
    gray = float(np.corrcoef(va, vb)[0, 1]) if va.std() > 1e-6 and vb.std() > 1e-6 else 0.0
    ea = cv2.Canny(ga, 60, 140).astype(np.float32).ravel()
    eb = cv2.Canny(gb, 60, 140).astype(np.float32).ravel()
    edge = float(np.corrcoef(ea, eb)[0, 1]) if ea.std() > 1e-6 and eb.std() > 1e-6 else 0.0
    # 低频结构：滤掉 CG->真人 的纹理/描边差异，只留构图与明暗布局。
    la = cv2.GaussianBlur(cv2.resize(ga, (80, 45)), (0, 0), 1.2).astype(np.float32).ravel()
    lb = cv2.GaussianBlur(cv2.resize(gb, (80, 45)), (0, 0), 1.2).astype(np.float32).ravel()
    low = float(np.corrcoef(la, lb)[0, 1]) if la.std() > 1e-6 and lb.std() > 1e-6 else 0.0
    return {"gray_corr": round(max(0.0, gray), 4),
            "lowfreq_corr": round(max(0.0, low), 4),
            "edge_corr": round(max(0.0, edge), 4)}


def _qa(src: list[np.ndarray], out: list[np.ndarray], meta: dict) -> dict:
    per = [_align(a, b) for a, b in zip(src, out)]
    lows = [p["lowfreq_corr"] for p in per]

    def mad(seq):
        return float(np.mean([np.abs(seq[i].astype(np.float32) -
                                    seq[i - 1].astype(np.float32)).mean()
                              for i in range(1, len(seq))])) if len(seq) > 1 else 0.0

    src_mad, out_mad = mad(src), mad(out)
    return {
        "frames": len(out), "fps": meta["fps"], "size": [meta["width"], meta["height"]],
        "align_gray_mean": round(float(np.mean([p["gray_corr"] for p in per])), 4),
        "align_lowfreq_mean": round(float(np.mean(lows)), 4),
        "align_lowfreq_min": round(float(np.min(lows)), 4),
        "align_edge_mean": round(float(np.mean([p["edge_corr"] for p in per])), 4),
        "flicker_source_mad": round(src_mad, 3),
        "flicker_output_mad": round(out_mad, 3),
        "flicker_ratio": round(out_mad / src_mad, 3) if src_mad > 1e-6 else None,
        "per_frame": per,
    }


def run(cfg: dict) -> dict:
    """唯一实现入口。capsule 适配器与 pipeline stage 都调这里。"""
    c = {**DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}
    src = Path(str(c.get("source_video") or "")).expanduser().resolve()
    if not src.is_file():
        return {"success": False, "error": f"source_video not found: {src}"}
    start, end = float(c.get("start") or 0), float(c.get("end") or 0)
    if end <= start:
        return {"success": False, "error": "end must be greater than start"}
    if c["mode"] not in ("full", "bg_lock"):
        return {"success": False, "error": "mode must be full or bg_lock"}
    if not 0 <= float(c["temporal_smooth"]) <= 0.3:
        return {"success": False, "error": "temporal_smooth must be within 0..0.3"}
    out = Path(str(c.get("output_path") or "")).expanduser().resolve()
    if not str(out).lower().endswith((".mp4", ".mov", ".mkv")):
        return {"success": False, "error": "output_path must be a video file"}

    run_dir = out.parent.parent if out.parent.name == "release" else out.parent
    (run_dir / "release").mkdir(parents=True, exist_ok=True)
    (run_dir / "qa").mkdir(parents=True, exist_ok=True)
    work_src = run_dir / "work/src_frames"
    work_out = run_dir / "work/out_frames"

    meta = _probe(src)
    rate = float(c["fps"] or meta["fps"])
    dur = end - start
    est = int(round(dur * rate))
    if est > int(c["max_frames"]):
        return {"success": False,
                "error": f"shot needs ~{est} frames > max_frames={c['max_frames']}; "
                         "raise max_frames explicitly for long shots"}
    wf = os.path.join(P.WF_DIR, c["workflow"] or SF.DEFAULT_WF)
    if not os.path.isfile(wf):
        return {"success": False, "error": f"workflow not found: {wf}"}

    tag = f"s{int(start * 1000):05d}"
    frames = _extract(src, start, dur, work_src)
    prompts = [_prompt_for_frame(start + i / rate, c) for i in range(len(frames))]
    identities = [_identity_for(start + i / rate, c) for i in range(len(frames))]
    print(f"[frame_exact] 身份锚：{sorted(set(identities))}", flush=True)
    src_imgs = [cv2.imread(str(p)) for p in frames]
    size = (meta["width"], meta["height"])
    out_imgs = _restyle(frames, prompts, int(c["seed"]), wf, size, tag)
    if c["mode"] == "bg_lock":
        _bg_lock(out_imgs, src_imgs, c["person_box"] or DEFAULT_BOX)
    _smooth(out_imgs, float(c["temporal_smooth"]))

    work_out.mkdir(parents=True, exist_ok=True)
    for p in work_out.glob("o_*.png"):
        p.unlink()
    for i, im in enumerate(out_imgs):
        cv2.imwrite(str(work_out / f"o_{i:04d}.png"), im)

    report = _qa(src_imgs, out_imgs, meta)
    report.update({"route": "source_led.frame_exact", "mode": c["mode"],
                   "seed": int(c["seed"]), "workflow": os.path.basename(wf),
                   "source": str(src), "start": start, "end": end,
                   "temporal_smooth": float(c["temporal_smooth"]),
                   "identity": c.get("identity"),
                   "characters": c.get("characters"),
                   "identity_by_frame": identities})
    (run_dir / "qa/frame_exact_qa.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [str(FFMPEG), "-y", "-v", "error", "-framerate", f"{rate:.6f}",
           "-i", str(work_out / "o_%04d.png")]
    if c["keep_audio"] and meta["has_audio"]:
        cmd += ["-ss", f"{start:.4f}", "-t", f"{dur:.4f}", "-i", str(src),
                "-map", "0:v", "-map", "1:a?", "-c:a", "aac"]
    else:
        cmd += ["-map", "0:v"]
    # 帧数必须精确等于源帧数：-shortest 会按音轨长度砍掉最后一帧（实测 49→48）。
    # 用 -frames:v 锁视频帧数，音轨长度由第二个输入的 -t 决定。
    cmd += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-frames:v", str(len(out_imgs)), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not out.is_file():
        return {"success": False, "error": f"ffmpeg mux failed: {(r.stderr or '')[-300:]}"}

    (run_dir / "artifact_manifest.json").write_text(json.dumps({"artifacts": [
        {"path": str(out), "category": "final_video", "title": "Frame-exact restyle"},
        {"path": str(run_dir / "qa/frame_exact_qa.json"), "category": "qa_report",
         "title": "Structural alignment and flicker QA"},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "qa/run_notes.json").write_text(json.dumps({
        "route": "source_led.frame_exact",
        "promise": "every output frame derives from the matching source frame",
        "frames": report["frames"],
        "identity": c.get("identity"),
        "characters": c.get("characters"),
        "align_lowfreq_mean": report["align_lowfreq_mean"],
        "align_gray_mean": report["align_gray_mean"],
        "align_edge_mean": report["align_edge_mean"],
        "flicker_ratio": report["flicker_ratio"],
        "known_limits": [
            "Per-frame independent diffusion can shift fine detail between frames; "
            "temporal_smooth and bg_lock are the two mitigations.",
            "Restyle output is resized back to source resolution, so alignment is "
            "structural, not pixel-identical.",
            "Source subtitles / UI are removed by the restyle; burn them back in post "
            "or use mode=bg_lock.",
            "Not a reference_remake: source timing and composition are intentionally kept.",
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"success": True, "video": str(out),
            "qa": str(run_dir / "qa/frame_exact_qa.json"),
            "manifest": str(run_dir / "artifact_manifest.json"),
            **{k: report[k] for k in ("frames", "align_gray_mean", "align_lowfreq_mean",
                                      "align_edge_mean", "flicker_ratio",
                                      "flicker_output_mad")}}


def check_warnings(sb: dict) -> list[str]:
    """给 cmd_check 用：frame_exact 分镜的契约提示。"""
    cfg = sb.get("frame_exact")
    if not isinstance(cfg, dict):
        return []
    w = ["[逐帧精确复刻] 检测到 frame_exact：画幅/fps/帧数/时长全部继承源片，"
         "不要套 9:16 归一化或 17n+5 帧阶梯，也不要跑 frames / push / video"]
    if not cfg.get("source_video"):
        w.append("[逐帧精确复刻] 缺 frame_exact.source_video")
    if not (cfg.get("start") is not None and cfg.get("end") is not None):
        w.append("[逐帧精确复刻] 缺 frame_exact.start / end")
    if not (cfg.get("identity") or cfg.get("characters")):
        w.append("[逐帧精确复刻] 没写 identity / characters：性别判断会落到「按参考图猜」，"
                 "逐帧独立扩散时人物容易一会儿偏男一会儿偏女；"
                 "必须显式写「一位 XX 岁日本男性/女性 + 发型发色服装」")
    return w


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--params", required=True, help="JSON 参数文件")
    a = ap.parse_args()
    cfg = json.load(open(a.params, encoding="utf-8"))
    raise SystemExit(0 if run(cfg).get("success") else 1)
