#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一致性工具：把「背景一致」从提示词祈祷变成可量化的闸门。

设计依据（2026-11-09 定稿）
- 背景一致 ≠ 人物一致，两条独立链路，别指望一句提示词同时解决。
- 普通生成（无原片）里，背景必须从「每镜重新生成的产物」变成「全片唯一的资产」：
  一张无人空镜板，各镜只做 crop / pan / zoom / 视差派生，不重新抽卡。
- 提示词是语义级的（"一个石砌房间"），语义相同 ≠ 门窗位置 / 光位 / 地平线相同；
  所以一致性靠复用 + 约束 + 质检，不靠措辞。
- 复刻（有原片）里一致性来自同源素材：首/尾帧各自从对应的原片帧独立重绘，
  同模型 + 同提示词 + 同 seed + 同预处理；**不要拿首帧 A 当输入去推尾帧**。

本模块只做「量」，不阻断出片：沿用 skill 既有约定——提示级问题只存档 + 打印，
由人工确认点 / 换种子闸门兜底。

用法
  python consistency.py --first s01_first.png --last s01_last.png
  python consistency.py --first s01_first.png --last s01_last.png --asset bg.png
  python consistency.py --selftest
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# 阈值：都是「提示级」，命中只告警
BG_SSIM_MIN = 0.72        # 背景区结构相似度低于此 = 布局被重画
BG_CHANGED_MAX = 0.35     # 背景区像素大幅变化占比高于此 = 背景被改动
HIST_CORR_MIN = 0.75      # 背景区色调相关性低于此 = 光位/调色漂了
LAYOUT_CORR_MIN = 0.70    # 背景边缘布局相关性低于此 = 门窗/家具位置被重排
BORDER = 0.18             # 取外圈 18% 当背景区（人物通常在中间）


def _im(path: str):
    import cv2

    im = cv2.imread(str(path))
    if im is None:
        raise FileNotFoundError(f"读不到图：{path}")
    return im


def _border_mask(shape, ratio=BORDER):
    import cv2
    import numpy as np

    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    dx, dy = int(w * ratio), int(h * ratio)
    m[:dy, :] = 1
    m[h - dy:, :] = 1
    m[:, :dx] = 1
    m[:, w - dx:] = 1
    return m.astype(bool)


def _block_ssim(a, b, block=8):
    """轻量 SSIM：分块算 (2μaμb+C1)(2σab+C2)/... 再平均，不依赖 skimage。"""
    import numpy as np

    a = a.astype(np.float64)
    b = b.astype(np.float64)
    h, w = a.shape
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    vals = []
    for y in range(0, h - block + 1, block):
        for x in range(0, w - block + 1, block):
            pa, pb = a[y:y + block, x:x + block], b[y:y + block, x:x + block]
            ma, mb = pa.mean(), pb.mean()
            va, vb = pa.var(), pb.var()
            cov = ((pa - ma) * (pb - mb)).mean()
            vals.append(((2 * ma * mb + c1) * (2 * cov + c2)) /
                        ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2)))
    return float(np.mean(vals)) if vals else 0.0


def bg_pair_metrics(first: str, last: str) -> dict:
    """首/尾帧背景区对比：结构、色调、改动面积。"""
    import cv2
    import numpy as np

    a, b = _im(first), _im(last)
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    mask = _border_mask(a.shape)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # 只在外圈背景区算 SSIM / 差分
    ga_m, gb_m = ga.copy(), gb.copy()
    ga_m[~mask] = ga.mean()
    gb_m[~mask] = gb.mean()
    ssim = _block_ssim(ga_m, gb_m)
    changed = float((np.abs(ga[mask] - gb[mask]) > 30).mean())

    # 用背景区像素单独算直方图（cv2 需要 uint8 全图 mask）
    m8 = (mask.astype(np.uint8) * 255)
    ha = cv2.calcHist([a], [0, 1], m8, [32, 32], [0, 256, 0, 256])
    hb = cv2.calcHist([b], [0, 1], m8, [32, 32], [0, 256, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    hist = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))

    # 布局：背景区边缘图的相关性，专抓「颜色没变但门窗/家具挪位」的空间重抽卡
    ea = cv2.Canny(ga.astype(np.uint8), 60, 140).astype(np.float32) * mask
    eb = cv2.Canny(gb.astype(np.uint8), 60, 140).astype(np.float32) * mask
    va, vb = ea[mask], eb[mask]
    if va.std() > 1e-6 and vb.std() > 1e-6:
        layout = float(np.corrcoef(va, vb)[0, 1])
    else:
        layout = 1.0 if float(np.abs(va - vb).mean()) < 1 else 0.0
    layout = max(0.0, min(1.0, layout))

    flags = []
    if ssim < BG_SSIM_MIN:
        flags.append(f"背景结构漂移 SSIM={ssim:.3f}<{BG_SSIM_MIN}")
    if changed > BG_CHANGED_MAX:
        flags.append(f"背景大面积改动 {changed:.1%}>{BG_CHANGED_MAX:.0%}")
    if hist < HIST_CORR_MIN:
        flags.append(f"背景色调漂移 corr={hist:.3f}<{HIST_CORR_MIN}")
    if layout < LAYOUT_CORR_MIN:
        flags.append(f"背景布局重排 edge_corr={layout:.3f}<{LAYOUT_CORR_MIN}")
    return {
        "bg_ssim": round(ssim, 4),
        "bg_changed": round(changed, 4),
        "bg_hist_corr": round(hist, 4),
        "bg_layout_corr": round(layout, 4),
        "flags": flags,
        "ok": not flags,
    }


def asset_tone_drift(frame: str, asset: str) -> dict:
    """帧的背景色调 vs 空镜资产（只比颜色，不比几何——构图本来就不同）。"""
    import cv2

    a, b = _im(frame), _im(asset)
    m8 = (_border_mask(a.shape).astype(np.uint8) * 255)
    ha = cv2.calcHist([a], [0, 1], m8, [32, 32], [0, 256, 0, 256])
    hb = cv2.calcHist([b], [0, 1], None, [32, 32], [0, 256, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    corr = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
    return {"asset_hist_corr": round(corr, 4),
            "flags": [f"背景色调偏离空镜资产 corr={corr:.3f}"] if corr < HIST_CORR_MIN else []}


def gate(first: str, last: str, asset: str | None = None) -> dict:
    """给 pipeline 用的单一入口：合成分镜背景一致性报告。"""
    out = bg_pair_metrics(first, last)
    if asset and Path(asset).is_file():
        d = asset_tone_drift(first, asset)
        out.update(d)
        out["flags"] = out["flags"] + d["flags"]
        out["ok"] = not out["flags"]
    return out


def report(first: str, last: str, soft: bool = False, asset: str | None = None,
           label: str = "") -> dict:
    """**唯一报告入口**：算指标 + 按统一格式打印一行，返回 dict。

    `frames` 与 `source_frames` 两条链路都必须调它，别各自拼字符串——
    否则阈值 / 标签 / 字段一处改了另一处没改，日志与 state 就会不一致。
    soft=True（光效题材 `lighting_fx` 或机位推拉 `camera_move`）时，
    未过闸门只标 `[存档]` 不刷 `[提示]`，沿用 skill 既有「提示级不挡出片」约定。
    """
    g = gate(first, last, asset)
    tag = "[存档]" if (soft and not g["ok"]) else ("OK" if g["ok"] else "[提示]")
    extra = "" if g["ok"] else " " + "；".join(g["flags"])
    print(f"{label}背景一致性 SSIM={g['bg_ssim']:.3f} "
          f"layout={g['bg_layout_corr']:.3f} {tag}{extra}", flush=True)
    return g


def contract_warnings(sb: dict) -> list[str]:
    """分镜层的一致性契约检查（cmd_check 调用，只出提示不报错）。"""
    warns = []
    bg = (sb.get("bg_ref") or "").strip()
    if bg:
        if not (sb.get("space_lock") or "").strip():
            warns.append("[一致性契约] 写了 bg_ref 但没写 space_lock（固定方位词：门在左/窗在右/主光左上），"
                         "多机位时背景左右关系容易翻转")
        warns.append("[背景资产] bg_ref 必须全片唯一且无人；不要逐镜重生成背景，"
                     "也不要把生成结果回喂当空镜（瑕疵会逐代放大）")
    src = sb.get("source_frames")
    if src:
        warns.append("[复刻链路] 检测到 source_frames：一致性由原片同源帧保证，"
                     "请跑 `source_frames` 而不是 `frames`；不要用首帧 A 当输入去推尾帧")
    return warns


def _selftest() -> int:
    import cv2
    import numpy as np

    base = np.zeros((256, 256, 3), np.uint8)
    base[:] = (120, 130, 140)
    cv2.rectangle(base, (20, 20), (90, 200), (60, 70, 80), -1)   # 左侧"门"
    same = base.copy()
    cv2.circle(same, (128, 128), 30, (200, 180, 160), -1)         # 中间人物变化
    drift = base.copy()
    # 模拟「模型每镜重新抽空间布局」：门从左侧挪到右侧，整体色调不变
    cv2.rectangle(drift, (20, 20), (90, 200), (120, 130, 140), -1)
    cv2.rectangle(drift, (170, 20), (240, 200), (60, 70, 80), -1)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p1, p2, p3 = (str(Path(td) / f"{n}.png") for n in ("a", "b", "c"))
        for p, im in ((p1, base), (p2, same), (p3, drift)):
            cv2.imwrite(p, im)
        ok = gate(p1, p2)
        bad = gate(p1, p3)
        print(json.dumps({"same": ok, "drift": bad}, ensure_ascii=False, indent=2))
        assert ok["ok"], "同一背景 + 中间人物变化不应该报警"
        assert bad["flags"], "背景被改动应该报警"
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--first")
    ap.add_argument("--last")
    ap.add_argument("--asset")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    if not (a.first and a.last):
        ap.error("需要 --first / --last，或用 --selftest")
    print(json.dumps(gate(a.first, a.last, a.asset), ensure_ascii=False, indent=2))
