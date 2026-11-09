#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复刻链路：首/尾帧各自从「原片对应帧」独立重绘，一致性由同源素材保证。

为什么要单独一条命令（2026-11-09 定稿）
- `frames`（普通生成，无原片）靠「人物参考图 + 无人空镜」双图链路锁身份与背景；
  尾帧从本段首帧派生，这是用户 2026-11-05 定稿的正路，不要改。
- `frames` 一旦套到复刻上就会出问题：拿首帧 A 当输入去推尾帧，模型会把构图焊死在 A，
  拿不到原片尾帧的机位/景别；而且 2.1 在这种用法下容易触发「高清复刻」式的过锐化。
- 复刻有外部真值（原片同一个镜头的两帧），所以最优解是**两帧各自从对应的原片帧重绘**：
  同模型 + 同提示词 + 同 seed + 同预处理尺寸，身份/服装/背景天然同源。
  **绝不把 A 喂给尾帧当图1或图2**。

分镜写法（全部字段都是可选的，只有 dir 必填）
  "source_frames": {
    "dir": "D:/.../shots",                    # 原片抽帧目录（必填）
    "pattern": "shot_{i:03d}_{which}.jpg",    # 默认这个；可用 {i} {idx} {which}
    "prompt": "把这张画面重绘成…；严格保留构图/机位/人物姿势/背景结构/光效",
    "workflow": "Qwen21-SingleRef916-L4.json",# 默认单图 L4；路径相对 pipeline.WF_DIR
    "seed_base": 20261108,                   # 不写则由分镜路径派生（自动避开坏种子）
    "out_dir": "D:/.../work/k2_refs",        # 可选：额外产出一份到项目目录
    "out_name": "s{idx:02d}_{which}.png",    # 可选：out_dir 里的命名
    "nodes": {"10": [0, "image"], "40": [0, "prompt"],
              "32": [0, "prefix"], "30": [0, "seed"]}  # 可选：换工作流时改节点映射
  }

用法（统一走 pipeline 入口，别再另写驱动）
  pipeline.py source_frames --storyboard sb.json [--segs 4,8|all] [--force]
  python source_frames.py --storyboard sb.json [--segs 4,8] [--force]

设计约定
- state.first/last 一律记 COMFY_INPUT 下的规范文件名，保证 push / video 的
  `os.path.basename()` 能直接用；out_dir 只是给人看的副本。
- 已存在的 out_dir 成对帧会自动 adopt 进 state（不重绘），所以旧产物不会作废。
- 断点按「配置指纹」判断：prompt / 工作流 / seed / 尺寸一变就自动重绘，
  不会出现「改了提示词却还在跳过」的隐形不一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import consistency  # noqa: E402
import pipeline as P  # noqa: E402

DEFAULT_PROMPT = (
    "参考这张画面重绘成真人实拍剧照：画面里是一位日本年轻人物（性别与年龄按参考图该角色），"
    "日本人长相与脸型、日系自然妆容，白皙细腻的皮肤，皮肤做过精修磨皮——看不到毛孔与瑕疵，"
    "柔和均匀、带柔焦雾面质感，像日系写真海报。真实发丝、真实布料与金属质感，"
    "人物造型按参考图改成真人版：发色、发型、服装配色、配饰与武器都照参考图。"
    "严格保留参考图的构图、镜头角度、景别、人物在画面里的位置与大小、人物姿势与动作、"
    "武器位置与朝向、背景结构与光影方向；"
    "参考图里的元素光效必须原样保留——地面上的荧光光圈、发光粒子、技能特效的颜色与形状都不要丢，"
    "只把画风从 CG 变成真人实拍，特效做成真实的光。"
    "画面里不出现文字，不出现动画描边。"
)
DEFAULT_WF = "Qwen21-SingleRef916-L4.json"
DEFAULT_PATTERN = "shot_{i:03d}_{which}.jpg"
DEFAULT_OUT_NAME = "s{idx:02d}_{which}.png"
DEFAULT_NODES = {                    # 节点号 -> [widget 序号, 取值来源]
    "10": [0, "image"],
    "40": [0, "prompt"],
    "32": [0, "prefix"],
    "30": [0, "seed"],
}
RETRY = 4                           # 桌面版偶发 "Cancelling running prompt" / 502


def _stage(p: Path) -> str:
    """LoadImage 只认 COMFY_INPUT 下的文件名。"""
    dst = Path(P.COMFY_INPUT) / p.name
    if not dst.is_file() or dst.stat().st_mtime < p.stat().st_mtime:
        shutil.copy(p, dst)
    return p.name


def _seed(cfg: dict, sb: dict, sb_path: str, i: int) -> int:
    base = int(cfg.get("seed_base") or sb.get("keyframe_seed") or 0)
    if not base:
        h = hashlib.md5(os.path.abspath(sb_path).encode("utf-8")).hexdigest()
        base = int(h[:8], 16) % 2_000_000_000 + 1
    if base == 100000:                       # 铁律：坏种子
        base += 1
    return base + i - 1                      # 每段固定；首/尾帧必须相同


def _fingerprint(cfg: dict, prompt: str, wf: str) -> str:
    """配置指纹：prompt / 工作流 / 节点映射 / 命名一变就自动重绘。"""
    raw = json.dumps({"dir": cfg.get("dir"), "pattern": cfg.get("pattern"),
                      "prompt": prompt, "wf": wf, "nodes": cfg.get("nodes")},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _cfg_tag(cfg: dict, prompt: str, wf: str, seed: int) -> str:
    return f"{_fingerprint(cfg, prompt, wf)}:{seed}"


def _parse_segs(spec, total: int) -> set[int] | None:
    if spec is None:
        return None
    if isinstance(spec, int):
        return {spec}
    spec = str(spec).strip()
    if not spec or spec.lower() == "all":
        return None
    return {int(x) for x in spec.split(",") if x.strip()}


def _node_values(cfg: dict, img: str, prompt: str, seed: int, prefix: str) -> dict:
    vals = {"image": img, "prompt": prompt, "seed": seed, "prefix": prefix}
    spec = cfg.get("nodes") or DEFAULT_NODES
    out = {}
    for node, rule in spec.items():
        if not (isinstance(rule, (list, tuple)) and len(rule) == 2 and rule[1] in vals):
            raise SystemExit(f"source_frames.nodes[{node}] 必须是 [widget序号, image|prompt|seed|prefix]")
        out[int(node)] = {int(rule[0]): vals[rule[1]]}
    return out


def _run_one(wf: str, cfg: dict, img: str, prompt: str, seed: int, prefix: str) -> Path:
    outs = P.comfy_run(wf, _node_values(cfg, img, prompt, seed, prefix), timeout=1800)
    spec = cfg.get("nodes") or DEFAULT_NODES
    save_node = next((int(k) for k, v in spec.items() if v[1] == "prefix"), 32)
    items = (outs.get(str(save_node)) or outs.get(save_node) or {}).get("images") or []
    if not items and save_node != 32:
        items = (outs.get("32") or {}).get("images") or []
    if not items:
        raise RuntimeError(f"工作流无输出：{prefix}")
    got = Path(P.COMFY_OUTPUT) / items[-1].get("subfolder", "") / items[-1]["filename"]
    dst = Path(P.COMFY_INPUT) / f"{prefix}.png"
    shutil.copy(got, dst)
    return dst


def _emit(sb_path: str, sb: dict, i: int, st: dict, rec: dict, pair: dict,
          cfg: dict, seed: int, fp: str, pfx: str, write_copy: bool = True) -> None:
    """落盘统一出口：state 真值 + 可选副本 + 背景 QA，只此一处。"""
    for which in ("first", "last"):
        name = (cfg.get("pattern") or DEFAULT_PATTERN).format(
            i=i, idx=i, which=which)
        p = Path(cfg["dir"]) / name
        if p.is_file():
            _stage(p)                            # 输入也留一份规范副本
    rec["first"] = str(pair["first"])
    rec["last"] = str(pair["last"])
    rec["source_seed"] = seed
    rec["source_chain"] = True               # 来自原片同源帧，不是 A 派生
    rec["source_cfg"] = fp
    rec["source_frames"] = {"workflow": os.path.basename(cfg.get("workflow") or DEFAULT_WF),
                            "pattern": cfg.get("pattern") or DEFAULT_PATTERN}

    out_dir = cfg.get("out_dir")
    if out_dir and write_copy:
        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        oname = cfg.get("out_name") or DEFAULT_OUT_NAME
        for which in ("first", "last"):
            copy = od / oname.format(i=i, idx=i, which=which)
            shutil.copy(pair[which], copy)
            rec[f"{which}_copy"] = str(copy)
    elif out_dir:
        oname = cfg.get("out_name") or DEFAULT_OUT_NAME
        for which in ("first", "last"):
            rec[f"{which}_copy"] = str(Path(out_dir) / oname.format(i=i, idx=i, which=which))

    s = (sb.get("segments") or [])[i - 1] if i <= len(sb.get("segments") or []) else {}
    _soft = bool(s.get("lighting_fx")) or bool(s.get("camera_move"))
    try:
        rec["bg_qa"] = consistency.report(
            rec["first"], rec["last"], soft=_soft, label=f"[段{i}] ")
    except Exception as exc:                 # noqa: BLE001
        print(f"[段{i}] 背景质检跳过（{exc}）", flush=True)
    P.save_state(sb_path, st)


def _adopt_existing(sb_path: str, sb: dict, st: dict, cfg: dict, pfx: str,
                    tag_fn) -> int:
    """把 out_dir 里历史驱动产出的成对帧登记进 state，避免重绘 + 统一真值。"""
    out_dir = cfg.get("out_dir")
    if not out_dir:
        return 0
    od = Path(out_dir)
    oname = cfg.get("out_name") or DEFAULT_OUT_NAME
    n = 0
    for i, s in enumerate(sb.get("segments") or [], 1):
        rec = st["segments"].setdefault(str(i), {})
        if rec.get("source_chain") and rec.get("first") and rec.get("last"):
            continue
        paths = {w: od / oname.format(i=i, idx=i, which=w)
                 for w in ("first", "last")}
        if not all(p.is_file() for p in paths.values()):
            continue
        pair = {}
        for which, p in paths.items():
            dst = Path(P.COMFY_INPUT) / f"{pfx}_src_s{i:02d}_{which}.png"
            shutil.copy(p, dst)
            pair[which] = dst
        _emit(sb_path, sb, i, st, rec, pair, cfg,
              _seed(cfg, sb, sb_path, i), tag_fn(i), pfx, write_copy=False)
        rec["source_origin"] = "legacy-adopt"   # 来源可追溯，但配置指纹按当前算
        P.save_state(sb_path, st)
        print(f"[段{i}] 沿用已有首尾帧并登记 state（legacy-adopt）", flush=True)
        n += 1
    return n


def run(sb_path: str, sb: dict, segs: str | None = None, force: bool = False) -> int:
    cfg = sb.get("source_frames")
    if not isinstance(cfg, dict) or not cfg.get("dir"):
        raise SystemExit("分镜缺 source_frames.dir（原片抽帧目录）")
    src = Path(cfg["dir"])
    if not src.is_dir():
        raise SystemExit(f"原片抽帧目录不存在：{src}")
    prompt = (cfg.get("prompt") or DEFAULT_PROMPT).strip()
    wf = os.path.join(P.WF_DIR, cfg.get("workflow") or DEFAULT_WF)
    if not os.path.isfile(wf):
        raise SystemExit(f"重绘工作流不存在：{wf}")

    st = P.load_state(sb_path, sb)
    pfx = sb.get("prefix") or P.PFX
    seg_list = sb.get("segments") or []
    only = _parse_segs(segs, len(seg_list))
    tag_fn = lambda i: _cfg_tag(cfg, prompt, wf, _seed(cfg, sb, sb_path, i))
    if not force:
        _adopt_existing(sb_path, sb, st, cfg, pfx, tag_fn)

    done = 0
    for i, s in enumerate(seg_list, 1):
        if only and i not in only:
            continue
        rec = st["segments"].setdefault(str(i), {})
        seed = _seed(cfg, sb, sb_path, i)
        fp = tag_fn(i)
        if not force and rec.get("source_cfg") == fp and \
                rec.get("first") and rec.get("last") and \
                os.path.exists(rec["first"]) and os.path.exists(rec["last"]):
            print(f"[段{i}] 跳过（配置未变且首尾帧齐全）", flush=True)
            continue

        pattern = cfg.get("pattern") or DEFAULT_PATTERN
        inputs = {}
        for which in ("first", "last"):
            p = src / pattern.format(i=i, idx=i, which=which)
            if not p.is_file():
                print(f"[段{i}] 缺原帧 {p.name}，跳过", flush=True)
                break
            inputs[which] = p
        if len(inputs) < 2:
            continue

        pair = {}
        for attempt in range(1, RETRY + 1):
            try:
                pair = {}
                for which in ("first", "last"):
                    print(f"[段{i}] {which} 重绘中 seed={seed} src={inputs[which].name}",
                          flush=True)
                    pair[which] = _run_one(
                        wf, cfg, _stage(inputs[which]), prompt, seed,
                        f"{pfx}_src_s{i:02d}_{which}")
                break
            except (Exception, SystemExit) as exc:   # noqa: BLE001
                print(f"[段{i}] 第 {attempt}/{RETRY} 次失败：{type(exc).__name__}: {exc}",
                      flush=True)
                if attempt == RETRY:
                    print(f"[段{i}] 放弃（{RETRY} 次均失败），已出的段不受影响", flush=True)
                    break
                time.sleep(8)
                try:
                    P.wait_service()
                except Exception:
                    pass
        if len(pair) < 2:
            continue
        _emit(sb_path, sb, i, st, rec, pair, cfg, seed, fp, pfx)
        done += 1
    print(f"\n完成 {done} 段复刻首尾帧（state 已登记）。下一步：push --storyboard <分镜> → resume",
          flush=True)
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--segs")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    raise SystemExit(run(a.storyboard,
                         json.load(open(a.storyboard, encoding="utf-8")), a.segs, a.force))
