"""锚图冒烟验收：拿候选锚当"图2"，连跑 3 个种子，全绿才收。

为什么需要这个：2.1 编辑模式会把图2（场景锚）当底版逐像素复刻——锚上的微纹理、
畸形肢体、凭空道具会被搬进每一帧并放大。2026-11-04 实测：同一段分镜，旧锚触发
"复刻模式"（斑点覆盖全身）的概率 8/15，换干净锚后六帧零重试。
而**静态指标无法预判**（旧锚中频带 5.86 比新锚 6.44 还低），只能实跑。

跑法：SceneRef916（图1=人物参考、图2=候选锚），提示词用分镜第 1 段的尾帧提示词
（编辑模式最容易复刻的那条路径），只换种子。

判据（重要：不是"全绿才收"，因为触发是「锚 × 种子」的交互，不存在绝对免疫的锚）：
  - 每个种子报 脸部 Laplacian（256 尺度）与 全图高频占比；脸<380 且 高频<15% 记 PASS
  - **0/3 坏 → 优**；**1/3 坏 → 可用，但必须在分镜里 `keyframe_seed` 锁一个 PASS 的种子**
  - **≥2/3 坏 → 这张锚该换**
  实测参照：旧锚 100000 坏 / 100001 好；新锚 100000 好 / 100001 坏 ——
  同一个种子换锚会翻面，说明锚只是"偏置"，真正的兜底是种子闸门（生成时自动重试）。

用法：
  $PY anchor_probe.py --storyboard sb.json                      # 默认测分镜当前锚
  $PY anchor_probe.py --storyboard sb.json --anchor cand.png    # 测候选锚
  $PY anchor_probe.py --storyboard sb.json --seeds 100000,100007
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline as P  # noqa: E402

FACE_MAX = 380
HF_MAX = 0.15


def probe(sb_path, anchor=None, seeds=(100000, 100001, 100002)):
    sb = json.load(open(sb_path, encoding="utf-8"))
    s = sb["segments"][0]
    pr, _ = P.render_prompts(sb, s)
    prompt = pr["last"].replace(P.SCENE_FROM_IMG, P.SCENE_FROM_TXT)

    pfx = sb.get("prefix") or P.PFX
    if anchor is None:
        scene_ref = sb.get("scene_ref")
        anchor = (os.path.join(P.COMFY_INPUT, scene_ref) if scene_ref
                  else os.path.join(P.COMFY_INPUT, f"{pfx}_anchor.png"))
    elif not os.path.isabs(anchor):
        anchor = os.path.join(P.COMFY_INPUT, anchor)
    if not os.path.exists(anchor):
        print(f"锚不存在: {anchor}")
        return 1

    ref = P.ref_name(sb)
    rows = []
    for sd in seeds:
        outs = P.comfy_run(
            os.path.join(P.WF_DIR, "Qwen21-SceneRef916.json"),
            {10: {0: os.path.basename(ref)}, 11: {0: os.path.basename(anchor)},
             40: {0: prompt}, 32: {0: f"probe_{sd}"}, 30: {0: sd}},
        )
        path = os.path.join(P.COMFY_OUTPUT, outs["32"]["images"][-1]["filename"])
        lap, hf = P.face_sharpness(path), P.hf_ratio(path)
        good = (lap is None or lap < FACE_MAX) and (hf is None or hf < HF_MAX)
        rows.append((sd, lap, hf, good, path))

    print(f"被测锚: {os.path.basename(anchor)}")
    print(f"{'种子':>8s} {'脸Lap':>8s} {'全图高频':>9s}  判定")
    for sd, lap, hf, good, path in rows:
        lap_s = "-" if lap is None else f"{lap:.1f}"
        print(f"{sd:8d} {lap_s:>8s} {(hf or 0) * 100:8.1f}%  {'PASS' if good else 'FAIL'}  "
              f"{os.path.basename(path)}")
    print()

    bad = sum(1 for r in rows if not r[3])
    good_seeds = [str(sd) for sd, _l, _h, g, _p in rows if g]
    if bad == 0:
        print(f"锚优 ✅（{len(rows)} 个种子全 PASS）")
        return 0
    if bad * 3 <= len(rows):          # 坏 ≤ 1/3
        print(f"锚可用（{bad}/{len(rows)} 坏）—— 但必须锁种子："
              f"分镜里写 \"keyframe_seed\": {good_seeds[0]}（PASS 的：{', '.join(good_seeds)}）；"
              f"生成时仍有闸门兜底")
        return 0
    print(f"锚不可用 ❌（{bad}/{len(rows)} 坏）—— 换锚：重出（提示词带「边缘完整收尾/"
          f"无多余肢体/无蒸汽浮尘」并目视确认解剖正常），别用 inpaint 补救、别靠软化锚")
    return 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--anchor", default=None, help="候选锚图（绝对路径或 ComfyUI input 下的文件名）")
    ap.add_argument("--seeds", default="100000,100001,100002")
    a = ap.parse_args()
    seeds = tuple(int(x) for x in a.seeds.split(",") if x.strip())
    sys.exit(probe(a.storyboard, a.anchor, seeds))
