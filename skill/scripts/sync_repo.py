#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消除「两份源码」：把活跃 skill 的脚本、SKILL.md 与运行资产同步到 ComfyUI 节点仓库。

背景（2026-11-09）
- 活跃源码：`.workbuddy/skills/h3-long-video/`（capsule 适配器与所有 drive 脚本都指它）
- 节点仓库：`ComfyUI-Installs/h3-long-video-repo/skill/`（被 custom_nodes 软链引用，
  里面的 align_exposure.py / anchor_probe.py / gen*.py 会 `import pipeline as P`）
- 两边一度各有一个对方没有的修复：活跃版有 `comfy_free` + 背景 QA，
  repo 版有链式段首帧不重生成的 guard。分叉就是这么来的。

2026-11-17 补 assets 白名单
----------------------------
在此之前只同步 `scripts/*.py` 和 `SKILL.md`，**assets/ 从未入库** —— 结果两条链路
真正在跑的 base 工作流（`H3-SelfLift_单图参考.base.json` /
`H3-SelfLift_黑鹤二采.base.json`）、唯一一份全局约束 `selflift.global.json`、
复刻台提示词模板 `remake_prompt.txt` 全都只存在于本机 `.workbuddy` 下，
换台机器 clone 仓库跑不起来，也不知道该用什么工作流。

只同步「运行必需」的资产（见 ASSET_GLOBS），不同步产物与备份：
mp4 / prompts.html / *.state.json / *.bak* 各机器不同，且体积大。

注意：**这里同步的是入库副本，不改运行时读取路径**。run_selflift.py 的
DEFAULT_WORKFLOW / GLOBAL_CFG_PATH 仍指向活跃目录；`load_global_cfg()` 有回退
逻辑，活跃目录缺失时才读本脚本旁的 assets。所以「改了仓库里的工作流」不会
影响正在跑的片子，改配置仍然只改活跃目录。

用法
  python sync_repo.py --check    # 只读，列出内容不一致的文件（忽略 CRLF/LF 差异）
  python sync_repo.py --apply    # 把活跃版写过去（repo 侧统一 LF），repo 独有文件不动

约定：改代码只改活跃版；改完跑一次 --apply，再跑 --check 应为 ALL SYNCED。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SRC = Path("D:/Comfy-Desktop/.workbuddy/skills/h3-long-video")
DST = Path("D:/Comfy-Desktop/ComfyUI-Installs/h3-long-video-repo/skill")
SELF = "sync_repo.py"

# 运行必需的资产：工作流模板、全局约束、提示词模板、分镜样例。
# 用白名单而非「整个 assets/ 同步」—— 那个目录里混着 mp4 / prompts.html /
# *.state.json / *.bak*，都是本机产物，同步过去只会污染仓库。
ASSET_GLOBS = (
    "*.base.json",              # 两条链路的 base 工作流（真正被 cfg["workflow"] 打开）
    "selflift.global.json",     # 唯一一份全局约束（head/tail/workflow/参数）
    "remake_prompt.txt",        # 复刻台默认提示词模板
    "segments.selflift.example*.json",   # 分镜样例（含 2 人 / 3 参考图变体）
    "storyboard.example.json",
)


def _norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def _targets() -> list[tuple[Path, Path]]:
    # 注意：SELF 也一起同步。早期把它排除了，导致仓库里没有同步器 —— 别人 clone
    # 下来想「改完活跃目录再同步」时找不到工具，只能手工拷贝，于是又开始分叉。
    out = [(p, DST / "scripts" / p.name)
           for p in sorted((SRC / "scripts").glob("*.py"))]
    sk = SRC / "SKILL.md"
    if sk.is_file():
        out.append((sk, DST / "SKILL.md"))
    # 资产：白名单匹配，落在 skill/assets/ 下
    seen: set[str] = set()
    for pat in ASSET_GLOBS:
        for p in sorted((SRC / "assets").glob(pat)):
            if p.is_file() and p.name not in seen:
                seen.add(p.name)
                out.append((p, DST / "assets" / p.name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not SRC.is_dir() or not DST.is_dir():
        raise SystemExit(f"目录不存在：SRC={SRC} DST={DST}")

    drift, missing, synced, applied = [], [], [], []
    for src, dst in _targets():
        sb = _norm(src.read_bytes())
        db = _norm(dst.read_bytes()) if dst.is_file() else None
        if db is None:
            missing.append((src, dst))
            if a.apply:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(sb)
                applied.append(dst)
        elif db != sb:
            drift.append((src, dst))
            if a.apply:
                dst.write_bytes(sb)
                applied.append(dst)
        else:
            synced.append(dst)

    for src, dst in drift:
        print(f"[DRIFT ] {src.name}  ->  {dst}")
    for src, dst in missing:
        print(f"[MISSING] {src.name}  ->  {dst}")
    if a.apply:
        for p in applied:
            print(f"[APPLIED] {p}")
        print(f"已同步 {len(applied)} 个文件；repo 独有文件未动。再跑 --check 确认。")
        return 0
    if drift or missing:
        print(f"共 {len(drift)} 个内容分叉 / {len(missing)} 个缺失。"
              "改代码只改活跃版，然后 python sync_repo.py --apply")
        return 1
    print(f"ALL SYNCED：{len(synced)} 个文件与 repo 一致（忽略行尾）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
