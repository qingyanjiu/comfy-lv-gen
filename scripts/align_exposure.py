#!/usr/bin/env python
"""帧级曝光归一：把一组关键帧的整体明度对齐到锚图（本片第 1 段首帧）。

只动 LAB 的 L 通道（亮度），不做锐化/降噪/局部对比，色相和饱和度原样保留。
用「均值增益」而非两点映射：增益法保持明暗比例，且这批帧全是偏亮→压暗，不会引入新的高光裁切。

为什么需要：2.1 每帧独立出图，同一提示词也会有 ±20 的亮度漂移；H3 会把关键帧的
亮度原样带进视频，跨段拼接时墙面就会出现一档明暗跳变。

用法：
  PY="D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/.venv/Scripts/python.exe"
  $PY align_exposure.py --anchor ds_s01_first.png --dry ds_s01_last.png ds_s03_last.png
  $PY align_exposure.py --anchor ds_s01_first.png ds_s01_last.png ds_s03_last.png
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P  # noqa: E402


def _resolve(name):
    if os.path.exists(name):
        return os.path.abspath(name)
    p = os.path.join(P.COMFY_INPUT, name)
    if os.path.exists(p):
        return p
    raise SystemExit(f"找不到图: {name}")


def align(anchor, path, dry=False, tol=3.0):
    import cv2
    import numpy as np
    ev, eo = P.exposure_stats(path)
    av, ao = P.exposure_stats(anchor)
    delta = ev - av
    name = os.path.basename(path)
    if abs(delta) <= tol:
        print(f"  {name}: 偏离 {delta:+.1f}，已在阈值 ±{tol:.0f} 内，跳过")
        return False
    if dry:
        print(f"  {name}: 偏离 {delta:+.1f}，V 均值 {ev:.0f} -> {av:.0f}，需校正")
        return True

    img = cv2.imread(path)
    # 闭环收敛在 V 均值上（QA 量的就是 V）：先按 V 误差给一个增益，再回到 L 上执行。
    # V 是 max(R,G,B)、L 是加权亮度，同样的增益在 V 上变化更小，所以乘 1.15 补偿。
    for _ in range(8):
        cur_v = v_mean_from(img)
        err = av - cur_v
        if abs(err) <= 0.5:
            break
        gain = 1.0 + (err / max(cur_v, 1.0)) * 1.15
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        L = lab[..., 0].astype(np.float32)
        lab[..., 0] = np.clip(L * gain, 0, 255).astype(np.uint8)
        img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    bak = path.replace(".png", ".prexpo.png")
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    cv2.imwrite(path, img)
    nv, no = P.exposure_stats(path)
    print(f"  {name}: 偏离 {delta:+.1f} -> {nv - av:+.1f}（过曝 {eo * 100:.1f}% -> {no * 100:.1f}%）"
          f"  备份 {os.path.basename(bak)}")
    return True


def v_mean_from(img):
    import cv2
    return float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("images", nargs="+")
    a = ap.parse_args()
    anchor = _resolve(a.anchor)
    print(f"锚: {os.path.basename(anchor)}")
    n = 0
    for x in a.images:
        n += align(anchor, _resolve(x), dry=a.dry)
    print(f"处理 {n} 张")


if __name__ == "__main__":
    main()
