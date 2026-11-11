# 已弃用（Deprecated）— 旧 H3 双图关键帧 / 黑鹤二采 链路

这些工作流是 2026-11-11 **之前**用的旧方法：

- `Qwen21-*` / `Qwen2511-*` 系列：Qwen 双图关键帧 → Director 二采
  （即「黑鹤大佬」改来的 minimax h3 二采链路，用户在 UI 里对应的文件是
  `minimax h3二采工作流（改自黑鹤大佬）.json`）
- `H3-Single-Clip-FirstLastRefs.json`：单镜头首尾帧链路（模型手册 §7）

## 已被取代

自 2026-11-11 起，长视频默认走 **`H3-SelfLift_单图参考` 尾帧接首帧**
（模型手册 §13 + Director 节点第 5 个 tab「SelfLift 逐段」）。新方法更快、
段间观感更自然，旧链路只在「逐像素锁背景 / 竖屏短片 / 必须精确摆关键姿态」时才用。

## 保留说明

- 这些文件仅作历史参考，**不再维护**。
- 如需使用，复制到上一级 `example_workflows/` 即可（注意 Director 节点仍是同一个，
  旧链路走 tab 1–4，新链路走第 5 个 tab）。
- `build_director_workflow.py` 现在会把 `H3-Single-Clip-FirstLastRefs.json`
  **生成到本目录**（`deprecated/`），所以重跑生成器不会把它们放回根目录。
