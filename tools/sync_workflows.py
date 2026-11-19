#!/usr/bin/env python
"""把 ComfyUI 界面里的 H3 工作流同步进本仓库 `workflows/`。

为什么需要这个
--------------
`skill/assets/*.base.json` 是**运行时模板**（`run_selflift.build_wf()` 读进来改
参数再提交），由 `skill/scripts/sync_repo.py` 从活跃 skill 目录同步。

但界面里还有一批**能直接拖进 ComfyUI 的工作流**（`user/default/workflows/`），
它们是另一条线：人在画布上调试、存盘，跟 base 模板不同步。2026-11-17 查出来
这 16 个工作流一份都没进仓库 —— 仓库里只有 base 模板，谁 clone 下来都没法在
界面上复现链路。这个脚本就是补那条线。

它做什么
--------
界面目录 → 仓库 `workflows/`，单向覆盖。只搬 `WORKFLOW_NAMES` 白名单里的文件
（上游生图/编辑工作流不属于本工程代码，不搬）。搬运是**字节级原样**：不脱敏、
不改路径、不重排版，因为工作流里的本机绝对路径对本机是能直接跑的。

改名规则见 `RENAMES`：浏览器下载残留的 `(Copy) (2)` 这类后缀在仓库里换成节点数，
让文件名能说明白它是什么。

用法
----
    python tools/sync_workflows.py --check     # 只报告，有差异返回 1
    python tools/sync_workflows.py --apply     # 覆盖写入仓库

`--check` 也会报「仓库有、界面没有」的孤儿，同 sync_repo.py 的口径：
`--apply` 不自动删，删是破坏性操作。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DST = REPO / "workflows"

# ComfyUI 界面工作流目录。可用环境变量覆盖（换机器 / 换安装路径时用）。
SRC = Path(os.environ.get(
    "COMFY_WORKFLOWS_DIR",
    r"D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/user/default/workflows",
))

# 入库白名单：界面文件名 → 仓库文件名。只收 H3 链路的，上游生图/编辑流不收。
RENAMES = {
    "H3-Long-Video-Director.json": "H3-Long-Video-Director.json",
    "H3-SelfLift_单图参考.json": "H3-SelfLift_单图参考.json",
    "H3-SelfLift_逐段导演.json": "H3-SelfLift_逐段导演.json",
    "H3-SelfLift_图片视频参考.json": "H3-SelfLift_图片视频参考.json",
    "H3-SelfLift_参考视频换动漫风.json": "H3-SelfLift_参考视频换动漫风.json",
    "H3-SelfLift_参考视频换动漫风_修正版.json": "H3-SelfLift_参考视频换动漫风_修正版.json",
    "minimax h3二采工作流（改自黑鹤大佬） (Copy) (2).json":
        "minimax-h3二采工作流-改自黑鹤大佬-66节点.json",
    "H3黑鹤-首帧与背景板直连.json":
        "H3黑鹤-首帧与背景板直连.json",
}


def _norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not SRC.is_dir():
        print(f"[SKIP] 界面工作流目录不存在：{SRC}")
        print("       换机器时用 COMFY_WORKFLOWS_DIR 指过去，或忽略本检查。")
        return 0
    DST.mkdir(exist_ok=True)

    drift, missing, synced, orphans, applied = [], [], [], [], []

    for src_name, dst_name in RENAMES.items():
        s = SRC / src_name
        d = DST / dst_name
        if not s.is_file():
            missing.append(dst_name)
            continue
        sb = _norm(s.read_bytes())
        db = _norm(d.read_bytes()) if d.is_file() else None
        if db is None:
            missing.append(dst_name)
            if a.apply:
                d.write_bytes(s.read_bytes())
                applied.append(dst_name)
        elif sb != db:
            drift.append(dst_name)
            if a.apply:
                d.write_bytes(s.read_bytes())
                applied.append(dst_name)
        else:
            synced.append(dst_name)

    # 孤儿：仓库 workflows/ 里有、但不在白名单（或界面已删）
    live = set(RENAMES.values())
    if DST.is_dir():
        orphans = [p.name for p in sorted(DST.glob("*.json")) if p.name not in live]

    for n in synced:
        print(f"[SYNCED ] {n}")
    for n in drift:
        print(f"[DRIFT  ] {n}  界面版与仓库不同")
    for n in missing:
        print(f"[MISSING] {n}  界面目录没有（或仓库没有）")
    for n in orphans:
        print(f"[ORPHAN ] {n}  仓库有、不在白名单 —— 人工确认后 git rm")

    if a.apply:
        print(f"\n已写入 {len(applied)} 个文件到 {DST}")
        return 0

    bad = len(drift) + len(missing) + len(orphans)
    if bad:
        print(f"\n不一致：{len(drift)} 分叉 / {len(missing)} 缺失 / {len(orphans)} 孤儿")
        print("  同步：python tools/sync_workflows.py --apply")
        print("  孤儿要人工 git rm（本脚本不删）")
        return 1
    print(f"\nALL SYNCED：{len(synced)} 个工作流与界面版一致（忽略行尾）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
