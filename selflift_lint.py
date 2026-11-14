#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分段提示词体检（只读）。

按 docs/segment编写规范.md 的硬指标检查每段 body / cast，输出 PASS / WARN / FAIL。
不改任何文件，跑在跑片过程中也安全。

用法：
    python selflift_lint.py --cfg output/xxx_1115_002114/segments.json
    python selflift_lint.py --cfg ... --json      # 机器可读
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SHOT_RE = re.compile(r"第\s*[一二三四五六七八九十\d]+\s*镜")
AT_RE = re.compile(r"At\s*\d{2}:\d{2}(?::\d{2}|\.\d+)?", re.IGNORECASE)
BLOCKS = ("[机位]", "[光线]", "[场景]")

# 远景不该写的面部细节（像素不够，写了也画不出）。注意「全景」不在此列：
# 全景人物占画面 60–85%，能看清轮廓，只允许写表情大方向，不算违规。
FACE_WORDS = ("眉头", "眉间", "唇角", "嘴角", "眼睑", "瞳孔", "眨眼", "下颌", "脸颊")
FAR_WORDS = ("大远景", "远景")

# 有这些词 = 画面里确实发生了可观察的变化
MOTION_WORDS = (
    "走", "迈", "跑", "转", "抬", "落", "挥", "推", "拉", "俯", "仰", "倾",
    "前移", "后退", "靠近", "后退", "停", "起身", "坐下", "伸手", "握", "举",
    "飘", "摆", "翻卷", "扬", "踏步", "调整", "晃", "移动", "递", "接",
)
# 一句"没变化"的写法
STILL_HINTS = ("保持不动", "静止", "不动", "固定不变", "始终不变")

CAM_WORDS = (
    "固定", "推进", "推近", "拉远", "拉出", "跟拍", "跟随", "平移", "横移",
    "摇摄", "摇", "升降", "滑轨", "环绕", "弧线", "手持", "变焦", "移焦",
    "dolly", "pan", "truck", "follow", "crane", "zoom", "rack",
)


def _issue(level, rule, msg):
    return {"level": level, "rule": rule, "msg": msg}


def lint_segment(seg, film, default_seconds=8.0):
    body = (seg.get("optimized_body") or seg.get("body") or "").strip()
    cast = (seg.get("cast") or "").strip()
    sec = float(seg.get("seconds") or film.get("seconds") or default_seconds)
    out = []

    if not body:
        return [_issue("FAIL", "empty", "body 为空")], {"chars": 0, "beats": 0}

    # 动作块 = 第一个 [机位] 之前的所有内容
    head_cut = body.find("[机位]")
    action = (body[:head_cut] if head_cut > 0 else body).strip()
    action = SHOT_RE.sub(" ", action).strip()

    shots = len(SHOT_RE.findall(body))
    ats = len(AT_RE.findall(body))
    # 节拍 = 分镜标签 或 At 时间点，谁多算谁
    beats = max(shots, ats)

    # --- R1 篇幅 ---
    need_chars = max(200, int(round(sec * 30)))
    if len(body) < need_chars * 0.7:
        out.append(_issue("FAIL", "length",
                          f"正文 {len(body)} 字，{sec:.0f}s 段至少 {need_chars} 字（现在偏薄，H3 会自己补，容易跑偏）"))
    elif len(body) < need_chars:
        out.append(_issue("WARN", "length",
                          f"正文 {len(body)} 字，建议 ≥{need_chars} 字"))

    # --- R2 节拍数 ---
    need_beats = max(1, int(round(sec / 5)))
    if beats < need_beats:
        out.append(_issue("FAIL", "beats",
                          f"{sec:.0f}s 段只有 {beats} 个节拍，至少 {need_beats} 个"
                          f"（用「第一镜/第二镜」拆开，或用 At 00:0x 标出变化点）；"
                          f"只有一个节拍时画面会整段几乎不动"))

    # --- R3 时间轴 ---
    if sec >= 6 and ats == 0 and shots <= 1:
        out.append(_issue("WARN", "timeline",
                          "没有 At 00:SS.mmm 关键帧，H3 对整段时长没有落点概念，建议至少标 1 个"))

    # --- R4 四块齐全 ---
    miss = [b for b in BLOCKS if b not in body]
    if miss:
        out.append(_issue("FAIL", "blocks", "缺少块：" + " ".join(miss)))

    # --- R5 动作块体量 ---
    need_action = max(60, int(round(sec * 10)))
    if len(action) < need_action:
        out.append(_issue("WARN", "action",
                          f"动作块 {len(action)} 字（{sec:.0f}s 建议 ≥{need_action}），容易只剩一句概括"))

    # --- R6 远景写脸 ---
    if any(w in body[:head_cut if head_cut > 0 else len(body)] for w in FAR_WORDS):
        hit = [w for w in FACE_WORDS if w in body]
        if hit:
            out.append(_issue("FAIL", "far_face",
                              f"远景镜却写了面部细节（{'/'.join(hit)}），该景别脸只有 10–20px，写了是白块"))

    # --- R7 错字 / 粘连 ---
    stray = re.findall(r"[\u4e00-\u9fa5][A-Za-z]{1,2}[\u4e00-\u9fa5]", body)
    if stray:
        out.append(_issue("FAIL", "typo", "中文里夹了孤立字母：" + "、".join(stray[:3])))
    if re.search(r"[\u4e00-\u9fa5]{2}\s{2,}[\u4e00-\u9fa5]", body):
        out.append(_issue("WARN", "spacing", "中文之间有连续空格"))

    # --- R8 机位是否给了可执行信息 ---
    cam_block = ""
    if "[机位]" in body:
        cam_block = body.split("[机位]", 1)[1].split("[光线]", 1)[0]
    if cam_block and not any(w in cam_block for w in CAM_WORDS):
        out.append(_issue("WARN", "camera", "[机位] 没写运镜类型（固定/推进/跟拍/横移…）"))

    # --- R9 有没有可观察的动作 ---
    if not any(w in action for w in MOTION_WORDS):
        out.append(_issue("FAIL", "motion", "动作块里找不到可观察的位移/姿态变化，成片会整段静止"))
    elif sum(1 for w in STILL_HINTS if w in action) and len(action) < need_action:
        out.append(_issue("WARN", "motion", "偏静态的写法 + 动作块偏短，建议补微动态（旗帜/尘土/马匹踏步）"))

    # --- R10 cast ---
    refs = seg.get("ref_images") or film.get("ref_images") or []
    if not cast and len(refs) >= 2:
        out.append(_issue("WARN", "cast",
                          "本片挂了多张人物参考，但本段没写 cast（本段谁出场/在哪一侧/什么状态）"))

    stats = {"chars": len(body), "action": len(action), "beats": beats,
             "shots": shots, "ats": ats}
    return out, stats


def lint_film(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    rows = []
    for seg in cfg.get("segments") or []:
        issues, stats = lint_segment(seg, cfg)
        rows.append({"id": seg.get("id"), "title": seg.get("title"),
                     "issues": issues, "stats": stats})
    return cfg, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="分段提示词体检（只读）")
    ap.add_argument("--cfg", required=True, help="segments.json 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)

    cfg, rows = lint_film(a.cfg)
    if a.json:
        print(json.dumps({"cfg": a.cfg, "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"体检：{a.cfg}")
    print(f"每段秒数 {cfg.get('seconds')} | 段数 {len(rows)}\n")
    total_f = total_w = 0
    for r in rows:
        s = r["stats"]
        f = sum(1 for i in r["issues"] if i["level"] == "FAIL")
        w = sum(1 for i in r["issues"] if i["level"] == "WARN")
        total_f += f
        total_w += w
        print(f"[{r['id']}] {r.get('title') or ''}  "
              f"{s.get('chars', 0)}字 / 节拍{s.get('beats', 0)} / 镜{s.get('shots', 0)} / At{s.get('ats', 0)}  "
              f"→ {f} FAIL, {w} WARN")
        for i in r["issues"]:
            print(f"    {i['level']:<4} {i['rule']:<9} {i['msg']}")
    print(f"\n合计 {total_f} FAIL / {total_w} WARN")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
