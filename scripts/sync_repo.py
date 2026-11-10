#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消除「两份源码」：把活跃 skill 的脚本与 SKILL.md 同步到 ComfyUI 节点仓库。

背景（2026-11-09）
- 活跃源码：`.workbuddy/skills/h3-long-video/`（capsule 适配器与所有 drive 脚本都指它）
- 节点仓库：`ComfyUI-Installs/h3-long-video-repo/skill/`（被 custom_nodes 软链引用，
  里面的 align_exposure.py / anchor_probe.py / gen*.py 会 `import pipeline as P`）
- 两边一度各有一个对方没有的修复：活跃版有 `comfy_free` + 背景 QA，
  repo 版有链式段首帧不重生成的 guard。分叉就是这么来的。

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


def _norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def _targets() -> list[tuple[Path, Path]]:
    out = [(p, DST / "scripts" / p.name)
           for p in sorted((SRC / "scripts").glob("*.py")) if p.name != SELF]
    sk = SRC / "SKILL.md"
    if sk.is_file():
        out.append((sk, DST / "SKILL.md"))
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
