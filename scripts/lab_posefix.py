#!/usr/bin/env python
"""A/B 实验：首帧转身——纯文字硬掰（现状）vs 多角度 LoRA 镜头旋转。

基线是 `pipeline.py` 现在做的事：拿一张正面首帧，用 POSE_FIX_TMPL 一段文字硬掰成侧身。
实验组把同一个提示词送进 `lab/Qwen2511-PoseFix-MultiAngle.json`（基座 + 多角度 LoRA），
另外加一句英文镜头旋转指令。两组除了这一个变量，输入图、seed、参考图全部相同。

用法：
  PY="D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/.venv/Scripts/python.exe"
  $PY scripts/lab_posefix.py --storyboard assets/三镜测试.json --seg 2 \
      --first h3lv_s04_first.png --cam left --angle 45

参数：
  --mode both|template|lora   跑哪一组（默认 both）
  --cam left|right|auto       镜头绕人物转的方向；auto 取目标朝向的反方向
  --angle 45                  旋转角度
  --seed                      固定种子，两组相同才能对比
  --tag                       输出文件名前缀，默认 lab
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P      # noqa: E402
import direction as D     # noqa: E402

WF_BASE = os.path.join(P.WF_DIR, "Qwen2511-Reference-Edit.json")
WF_LORA = os.path.join(P.WF_DIR, "lab", "Qwen2511-PoseFix-MultiAngle.json")


def resolve_image(name):
    if os.path.exists(name):
        return os.path.abspath(name)
    p = os.path.join(P.COMFY_INPUT, name)
    if os.path.exists(p):
        return p
    P.die(f"找不到图：{name}")


def run_once(wf, prompt, first_abs, out_prefix, seed):
    """跑一张。注入点和 pipeline.py 的转身修正保持一致：10/28/32/30。"""
    t0 = time.time()
    outs = P.comfy_run(wf, {
        10: {0: os.path.basename(first_abs)},
        28: {0: prompt},
        32: {0: out_prefix},
        30: {0: seed},
    })
    src = os.path.join(P.COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
    dst = os.path.join(P.COMFY_INPUT, out_prefix + ".png")
    shutil.copy(src, dst)
    return dst, round(time.time() - t0, 1)


def judge(path, target):
    try:
        y = D.face_yaw_signed(path)
    except Exception as e:
        return None, None, f"检测失败 {e}"
    ok = D.yaw_matches(y, target) if target else None
    return y, ok, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--seg", type=int, required=True)
    ap.add_argument("--first", required=True, help="要转身的首帧（文件名或绝对路径）")
    ap.add_argument("--mode", choices=["both", "template", "lora"], default="both")
    ap.add_argument("--cam", choices=["left", "right", "auto"], default="auto")
    ap.add_argument("--angle", type=float, default=45)
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--tag", default="lab")
    a = ap.parse_args()

    sb = json.load(open(a.storyboard, encoding="utf-8"))
    s = sb["segments"][a.seg - 1]
    dv, _ok = D.get_dir(s)
    target = dv.get("body")
    first = resolve_image(a.first)

    y0, _f, _e = judge(first, None)
    faces = None
    try:
        faces = D.count_faces(first)
    except Exception:
        pass
    print(f"输入 {os.path.basename(first)}  yaw {y0:+.2f}" if y0 is not None else
          f"输入 {os.path.basename(first)}  未检测到人脸")
    print(f"目标 body={target}（{D.CN.get(target, '?')}）  人脸数 {faces}")
    if target in (D.LEFT, D.RIGHT):
        print(f"达标要求 yaw {'<' if target == D.LEFT else '>'} {(-0.25) if target == D.LEFT else 0.25:+.2f}")

    base = P.POSE_FIX_TMPL.format(**P.seg_vars(sb, s))
    cam = a.cam
    if cam == "auto":
        # 镜头往人身体的反方向绕，才会看到目标那一侧
        cam = "left" if target == D.RIGHT else "right"
    lora_prompt = base + f"\nRotate the camera {a.angle:g} degrees to the {cam}."

    print(f"\n镜头旋转：{cam} {a.angle:g}°（auto 时取目标朝向的反方向）")
    print("模板提示词 %d 字；实验组 = 模板 + 一句英文旋转指令\n" % len(base))

    rows = []
    plan = [("template", WF_BASE, base), ("lora", WF_LORA, lora_prompt)]
    for name, wf, prompt in plan:
        if a.mode != "both" and a.mode != name:
            continue
        if not os.path.exists(wf):
            print(f"[{name}] 工作流缺失 {wf}")
            continue
        print(f"[{name}] 跑 {os.path.basename(wf)} ...", flush=True)
        out, sec = run_once(wf, prompt, first, f"{a.tag}_{a.seg:02d}_{name}", a.seed)
        y, ok, err = judge(out, target)
        rows.append((name, y, ok, sec, out, err))
        print(f"[{name}] 完成 {sec}s → yaw "
              + (f"{y:+.2f}  {'达标' if ok else '未达标'}" if y is not None else err), flush=True)

    print("\n结果")
    print(f"{'组':<10}{'yaw':>8}{'Δyaw':>8}{'判定':>8}{'耗时':>8}  文件")
    for name, y, ok, sec, out, err in rows:
        tag = "—" if y is None else ("PASS" if ok else "FAIL")
        delta = "—" if (y is None or y0 is None) else f"{y - y0:+.2f}"
        print(f"{name:<10}{(f'{y:+.2f}' if y is not None else '—'):>8}{delta:>8}"
              f"{tag:>8}{str(sec) + 's':>8}  {os.path.basename(out)}")
    if len(rows) == 2 and all(r[1] is not None for r in rows):
        t, l = rows[0][1], rows[1][1]
        print(f"\nΔyaw 对比：模板 {t - y0:+.2f} / LoRA {l - y0:+.2f}")
    print("\n看图确认：两只鞋尖是否和目标一致、有没有换人、服装有没有被改。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
