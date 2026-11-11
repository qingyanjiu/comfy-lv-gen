---
name: h3-long-video
description: 端到端长视频流水线。**2026-11-11 起长视频默认走 `H3-SelfLift_单图参考` ref2va 直出 + 分段接续（尾帧接首帧 + 全片唯一无人空镜板），见「分段接续链路」**，比首尾帧链路更快、段间观感也更自然（2026-11-11 用户判定）；旧的 Qwen2.1 首尾帧 → Director 链路只在需要逐像素锁背景时用。当用户要做多段/长视频、"帮我把这段做成视频""生成分镜再出片"时使用。触发词：分镜、长视频、多段、成片、流水线、批量出片、改提示词、分段接续。
---

# H3 长视频流水线

## 它解决什么

长视频的瓶颈不是"能不能生成 60 秒"，而是**每段的通过率**。
固定流程图能把顺序排好，但没法判断"这段能不能用"，所以这条流水线的核心是：
**每一道工序后都过一遍量化质检，卡在三道人工确认点上。**

三层结构：

```
人      ：定分镜 · 看首尾帧 · 验收成片
agent   ：跑工序 · 读质检 · 按失败码修参数重跑
工作流   ：Qwen 首尾帧 → H3 视频 → ffmpeg 合并
```

## 链路分流：开工第一步先判断（2026-11-11 修订，新增长视频默认链路）

**2026-11-11 用户定：长视频 / 多段成片一律走 `H3-SelfLift_单图参考`（ref2va 直出）**，
理由是**更快、而且段间观感明显更自然**（07:06 看完叠化成片后的判定）：
0.5MP 实测 3.5–3.8 分钟/段，旧链路 0.98MP 是 8 分钟；
「尾帧接首帧」比「先生成首尾帧再分别生成视频」更接近一镜到底，机制与实测见下一节。
下表最后一行是这条**默认链路**；前四行是「有原片复刻」与「旧的双图关键帧链路」，
只在对应场景才用。完整规则见新增章节「分段接续链路」。

| 用户给了什么 | 第一步 | 之后 | 一致性靠什么 | 规则章节 |
|---|---|---|---|---|
| **原片视频 + 要求逐帧对齐** | 直接按时间段抽**每一帧** | **`frame_exact`：每帧各自重绘，直接合成成片** | 源帧本身就是真值 | 「逐帧精确复刻」 |
| **原片视频 / 原片逐镜抽帧** | 抽每刀 first/last | **`source_frames`：两帧各自从对应原片帧重绘** | 原片同源帧（外部真值） | 「一致性来源」 |
| 人物图（+ 无人场景空镜） | — | `frames` 双图编辑 | 用户给的 `image_1` + 空镜资产 | 「两张参考图」 |
| **只有文字，没有任何图** | **`refs`：Krea2 造人物图 + 无人空镜** | **`frames` 双图编辑** | `refs` 造出来的人物图当 `image_1` | 「文生视频」 |
| **长视频 / 多段成片（默认）** | 人物图 + 一张无人空镜板 | **`run_selflift.py`：SelfLift ref2va 逐段直出 + 尾帧接首帧** | 同一人物图 + 全片唯一空镜板 + `first_frame` | 「分段接续链路」 |

**逐段可干预入口**：Director 节点第 5 个 tab「SelfLift 逐段」，见本章「逐段人在回路」；
一段段跑、看每段真实首/尾帧、换 seed 重出、指定首帧图、改提示词。

**⚠ 复刻链路最容易犯的错**：把首帧 A 当输入去推尾帧。实测 2.1 与 2511 都会把
构图焊死在 A 上（拿不到原片尾帧的拉远/换机位），2.1 还容易触发「高清复刻」过锐。
正解是**两帧各从对应原片帧独立重绘**，同模型 + 同提示词 + 同 seed + 同预处理，
身份/服装/背景天然同源。`source_frames` 命令就是这么做的，**不喂 A**。

**上面两条双图关键帧链路（旧默认）**从第二步起仍是**同一套**：Qwen2.1 双图编辑、首帧图2=空镜、
尾帧图2=本段首帧、`chain` 段首尾帧 byte-for-byte 相连、9:16 硬锁、
禁坏种子 100000、`push → resume` 出片。**不存在「纯文本专用出帧链路」**。
**这条链路现在只在需要逐像素锁死背景时用**；普通长视频不要绕回它。

**旧链路工作流 / 文档已归档弃用（2026-11-11 清理）**：Qwen2.1 / Qwen2.5 双图关键帧系列、
`H3-Single-Clip-FirstLastRefs.json`、以及对应的旧文档，已全部移到
`h3-long-video-repo/example_workflows/deprecated/` 与 `docs/deprecated/` 并标记弃用；
`.bak` 本地备份按 `.gitignore`（`*.bak` / `*.bak-*` / `*.capsule-bak`）规则已删除。
用户 UI 里的旧工作流（含「`minimax h3二采工作流（改自黑鹤大佬）.json`」）也一并移到
`user/default/workflows/deprecated/`。
**新的默认入口工作流**：`user/default/workflows/H3-SelfLift_逐段导演.json`
（Director 节点已显式设 `h3_tab=selflift`，一打开就停在「SelfLift 逐段」tab，不会再像旧文件那样停在 keyframes）。
**`frame_exact` 是例外**：它不生成运动，画幅/fps/帧数/时长全部继承源片，
必须绕开 9:16 归一化、5s 下限、17n+5 帧阶梯和 `frames / push / video` 三阶段。
- 唯一差别只是参考图**谁提供**：用户给，或者 `refs` 造。
- **`t2i` 直出全部关键帧是当天理解反了的弯路，已废弃**：命令保留作实验对照，
  `check` 会对 `keyframe_backend: "krea2_t2i"` 告警；正片不要用。
- `check` 分流：分镜写了 `ref_person_prompt` + `ref_bg_prompt` 但还没有
  `reference_images` → 提示先跑 `refs`，不报「缺身份锚」。

## 分段接续链路：SelfLift 单图参考直出（2026-11-11 定版，长视频默认）

### 它解决什么

多段各自独立生成时，黑场之后再出现的是**重新抽过**的天台：栏杆样式、楼群布局、
机位景别、光线档位全变，人物姿态也重新起手。观众看到的是"换了个地方"，不是"过了一段时间"。

三件套把接缝两侧的全图帧差 MAE 从 **51–80 压到 2.1–2.7**（0 = 同一帧），
也就是段与段之间几乎是同一帧；天台结构全片统一。

**过场方式（2026-11-11 06:42 用户改主意）**：第一版按需求插了 0.8 秒纯黑硬切，
用户反馈「黑屏不好」，**改回项目原来的接续方式 = 0.25 秒短叠化（交叉溶解 / dissolve）**，
也就是 `merge --join dissolve`（cfg 里 `join_mode=dissolve` 已是默认）。
黑场只作为可选历史规格保留（`--join black`），另有无过场硬切 `--join cut`。
首尾帧已接续到 MAE 2.x，叠化只把这 2.x 的极小差异抹平，观感是连续的一镜到底。

### 为什么尾帧接首帧更自然（2026-11-11 07:06 用户判定，选链路判据）

用户看完 0.25s 短叠化成片后判定：**首尾帧相连的接续方式，观感明显比旧的
「先生成首尾帧 → 再分别生成视频」自然**。这条与「更快」并列，是长视频默认走本节的理由，
不是可选项；选链路时别只比速度。

| | 旧：双图关键帧 → Director | 新：尾帧接首帧（本节） |
|---|---|---|
| 段起点 | Qwen2.1 从静态空镜板**重绘**的关键帧 | **上一段视频真实渲染的最后一帧** |
| 段间「相连」 | `chain` 同一张图 byte-for-byte 复制 + 硬切 | `first_frame` 近似复现（MAE 2.x）+ 0.25s 叠化 |
| 段内实质 | 两张静态端点之间补运动，背景是静帧推拉、无真实视差 | 视频模型从上一段末帧**继续演**，姿态/机位/光线/景深连续 |
| 人眼观感 | 「照片→动画→照片」，端点完全静止再突然启动，有起手感 | 连续运动接连续运动，接近一镜到底 |

**关键：两种「相连」不是一回事。** 旧链路保证的是**两张图相等**，新链路保证的是
**同一条运动曲线的延续**。前者像素相等但不保证运动连续，后者像素只近似却把运动演下来了，
所以观感反而更好。**别把 MAE=0 当质量目标**——Director 的 byte-identical chain 本质就是
「静止端点硬切」，不如 MAE 2.x + 叠化顺。

实测：接缝两侧帧差 51–80 → 2.2–2.7；锈门、灰白栏杆、楼群全片统一；
5 段共用一张人物图 + 一张全片唯一无人空镜板。

**代价（必须一起说）**：`first_frame` 只锁第 0 帧，段内光线仍会自演亮度循环
（逐秒 `L` 是 5 个锯齿，见「验收命令」）；空镜板是语义级、机位变化透视不严格对齐；
段间**必须串行**（下段依赖上段尾帧），改前段提示词就得 `all --force` 整条重跑。

**仍回旧链路的场合**：要逐像素锁死背景、有原片真值要复刻、竖屏 9:16 短片、
必须精确摆出某个关键姿态。普通长视频别绕回去。

### 唯一实现与文件归属

| 内容 | 路径 |
|---|---|
| 驱动（唯一实现） | `.workbuddy/skills/h3-long-video/scripts/run_selflift.py` |
| 工作流副本（已设 0.5MP / nvfp4 CLIP） | `assets/H3-SelfLift_单图参考.base.json` |
| 分镜模板 | `assets/segments.selflift.example.json` |
| 兼容薄壳（别在这改逻辑） | `tools/h3_selflift/run_selflift.py` |

改完活跃版必须跑 `python scripts/sync_repo.py --apply`，再 `--check` 应为 `ALL SYNCED`。
`--cfg <分镜json>` 开新片：分镜在哪，`segments/ refs/ final/ qa/` 就写到哪。

### 工作流与参数档位

`H3-SelfLift_单图参考.json` = MiniMax H3 **ref2va 直出**（视频模型自己演动作和运镜），
+ SelfLift 两段式升采样 + 音视频同出。**不过首尾帧、不过 Director、`chain` 规则一律不适用**。

- 主模型 `minimax_h3_hybrid_fl2va_ref2va_b25-49-int8`；CLIP 见文末环境变化
- Turbo 4step LoRA（1.0）；euler / simple，6 步 + SigmaRefiner 1 步，cfg 1.0，
  `transition_step=4`、`lowres_scale=0.5`
- `length` = **24fps + 17n+5 网格**；`duration=8` → 192 帧 = 8.00 秒
- **分辨率默认 0.5 MP**（16:9 → 960×544）；VHS `frame_rate` 必须 24（工作流里存的 16 是旧值）
- `task_type=auto`：给 `first_frame` 后自动走 FL2VA / Hybrid，实跑通过
- 8GB 实测 0.5MP 约 **3.5–3.8 分钟/段**（0.98MP 是 8 分钟）

### 三件套：节点怎么接

| 手段 | 节点 | 做法 |
|---|---|---|
| **尾帧接首帧** | `MiniMaxH3AudioConditioningT8.first_frame` ← LoadImage **151** | 每段跑完用 `-sseof -0.042` 抽最后一帧，下一段拿它当 `first_frame`；段 1 不接 |
| **全片唯一空镜板** | `ref_images.ref_image_1` ← LoadImage **152** | Krea2 t2i 从零造一张**完全无人**的场景板，5 段共用；提示词写 `<Picture 2>` |
| **段首尾静息** | 分镜 `tail` | 段首/段尾各约 0.5 秒人物静止、镜头稳定，接缝像一次自然呼吸而不是急刹 |

人物图仍是 `ref_images.ref_image_0`（LoadImage 150，提示词 `<Picture 1>`）。
`strict_prompt_tags=true` 时标签数要和参考图数对得上，两张就写 `<Picture 1>` + `<Picture 2>`。

### 分镜 cfg 字段（`--cfg` 读的就是它）

| 字段 | 说明 |
|---|---|
| `prefix` / `title` / `final_name` | 帧与成片命名；`final_name` 可自定义 |
| `workflow` | 指向 `assets/H3-SelfLift_单图参考.base.json` |
| `ref_image` / `comfy_ref_name` | 人物参考图本地路径 / 拷进 `COMFY_INPUT` 后的文件名 |
| `bg_plate` / `bg_plate_path` / `bg_plate_seed` | 空镜板文件名 / 本地路径 / 独立 seed |
| `chain_first_frame` | `true` = 自动把上段尾帧接到下段首帧 |
| `clip_name` | 覆盖工作流里的 CLIP 文件名（模型换过就改这里） |
| `fps` / `seconds` / `length` | 24 / 8.0 / 192 |
| **`join_mode` / `join_dissolve_seconds`** | **默认 `dissolve` / 0.25**（项目原来的短叠化）；可选 `black`（0.8s 纯黑）、`cut`（无过场硬切） |
| `join_black_seconds` | 0.8，仅 `join_mode=black` 时生效 |
| `megapixels` | **不写就是 0.5**（`DEFAULT_MEGAPIXELS`） |
| `head` / `tail` / `segments[].body` | 全片身份与画风锚 / 衔接锚 / 逐段动作与机位 |

### 命令

```bash
PY="D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/.venv/Scripts/python.exe"
RUN="D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts/run_selflift.py"

$PY $RUN plate --cfg <新片>/segments.json          # 造全片唯一无人空镜板
$PY $RUN graph  --cfg <新片>/segments.json          # 校验 API 图（不提交）
$PY $RUN seg 1  --cfg <新片>/segments.json          # 只跑第 1 段
$PY $RUN all    --cfg <新片>/segments.json          # 顺序跑 5 段，自动抽尾帧接续
$PY $RUN all    --cfg <新片>/segments.json --force   # 全片重跑（改了提示词必须重跑整条链）
$PY $RUN merge  --cfg <新片>/segments.json          # 0.25s 短叠化接续 + 拼接 + 静音版（默认）
$PY $RUN merge  --cfg <新片>/segments.json --join cut    # 无过场硬切
$PY $RUN merge  --cfg <新片>/segments.json --join black  # 切回 0.8s 纯黑硬切（历史规格）
$PY $RUN probe  --cfg <新片>/segments.json          # 逐段 ffprobe
```

**改了任何一段的提示词，必须从段 1 起整条重跑**：尾帧是链式的，
只重跑后段会让 `first_frame` 与新的前段尾帧对不上。

### 五条铁律

1. **`first_frame` 只锁第 0 帧，管不住整段。** 文本里只要留着
   「橙红夕阳 → 暗紫 → 深蓝」这种完整弧线或强色名锚，模型就会在段内把日落重演一遍，
   黑场后第 1 秒又跳回明亮橙红（2026-11-11 第一轮实测）。
   正解：**光线分摊到 5 段累积完成，每段只写「承接首帧、推进一小档、
   不得倒退回更亮的时刻」**，段内变化小到几乎察觉不到。
2. **空镜板必须完全无人、从零生成**，不能抠图、不能透明底、不能喂回生成结果；
   瑕疵会被逐段复刻放大。`plate` 命令已按这条写死提示词。
3. **UI 工作流转 API 有两个坑**：`VHS_VideoCombine.widgets_values` 是 **dict**，
   `build_prompt` 只认 list，要按 object_info 的 required 顺序摊平；
   `MiniMaxH3AudioConditioningT8` 把 width/height/length 转成连线输入后
   **widgets_values 里仍留占位值**，`build_prompt` 会整体错位
   （`task_type` 变空串、`audio_mode` 变 1344）→ 必须手写该节点 inputs。
   `to_api()` 已处理，别改回通用转换。
4. **短叠化用 ffmpeg `xfade=transition=fade:duration=0.25`，音频用 `acrossfade`**，
   两者必须**分开跑**（同一 filtergraph 会互相拖死、成片被截成一段）。
   offset 按 `k×(段长−0.25)` 算（8s 段 → 7.75 / 15.50 / 23.25 / 31.00）。
5. **默认不插黑场**：叠化版 39.00s（5×8 − 4×0.25），硬切版 40.00s；
   `blackdetect` 应查不到任何黑段。只有 `--join black` 才用 19 帧 = 0.7917s 纯黑
   （此时拼接必须 `-c:v copy`，二次有损编码会把纯黑变成 `(0,1,0)`）。

### 实测（2026-11-11 黄昏天台 5×8s，原版 vs 接续版）

| 指标 | 原版（5 段独立） | 接续版 |
|---|---|---|
| 接缝两侧 MAE | 51.0 / 52.7 / 57.0 / 79.8 | **2.6 / 2.7 / 2.7 / 2.2** |
| 场景几何 | 栏杆样式、楼群逐段重抽 | 锈门 / 灰白栏杆 / 楼群全片统一 |
| 光线 | 段间按档位跳 | 段间突变消失；**段内亮度仍锯齿，非严格单调**（见下） |
| 分辨率 / 单段耗时 | 1344×768 / ≈8 min | 960×544 / 3.5–3.8 min |

**最终交付是短叠化版**：`黄昏天台_成片_接续版_短叠化.mp4`（936 帧 / 39.00s，无黑场），
另有无过场硬切版（960 帧 / 40.00s）与黑场历史版（1036 帧 / 43.17s）留作对照。
换过场只改 `merge`，**不用重跑 5 段**。

**光线的准确结论（2026-11-11 复核修正，别再说「单调递进」）**：
改成 5 段累积光线锚后，「过场后第 1 秒突然跳回明亮橙红」这个**段间突变**确实没了，
但把最终合并成片逐秒抽帧算平均亮度 `L = (R+G+B)/3` 后是明显锯齿：

| 段 | 秒 | L | R−B（暖色度） |
|---|---|---|---|
| s1 | 0→7 | 72→33 | +13 → −16 |
| s2 | 8→15 | 31→**80** | −16 → **+15** |
| s3 | 16→23 | 80→35 | +15 → −17 |
| s4 | 24→31 | 40→67 | −14 → −22 |
| s5 | 32→38 | 66→30 | −24 → −8 |

橙红夕阳在 s2 / s3 又被重演了一次。**H3 会在每段内部自行走完一次亮度循环，
文本「不得倒退回更亮的时刻」压不住**。要真的单调只有两条路：
① 每段光线都写同一档、不在段内做时间推进，后期全片套一条统一色彩渐变（grading）；
② 换带时间条件控制的首尾帧链路。
**验收必须量数值曲线，不能只靠逐秒拼图目测**——目测曾把这条锯齿误判成单调。
证据：`output/rooftop-dusk-v2/qa/成片_逐秒全片.png`、`qa/成片_四接缝验收.png`。
片例与证据：`output/rooftop-dusk-v2/生成说明.md`、`qa/seam_final.png`、
`qa/_seam_dissolve.png`、`qa/_timeline_dissolve.png`、`qa/_timeline_before_light_fix.png`。手册见 **§13**。

### 验收命令

```bash
# 默认叠化/硬切版：blackdetect 应查不到任何黑段（无输出）
ffmpeg -v info -i final.mp4 -vf "blackdetect=d=0.2:pix_th=0.05" -an -f null - 2>&1 | grep black_start
# 逐秒全片拼图（看光线有没有回跳、场景有没有换、有没有黑格）
ffmpeg -y -i final.mp4 -vf "fps=1,scale=268:-1,tile=7x6:padding=2:color=gray" -frames:v 1 qa/_timeline.png
# 接缝密集抽帧（看 0.25s 叠化是否平滑）
ffmpeg -y -ss 7.65 -t 0.5 -i final.mp4 -vf "fps=12,scale=440:-1,tile=3x2" -frames:v 1 qa/_seam_dissolve.png
# 接缝 MAE：抽每段首尾帧，算相邻段尾→首的全图 RGB 平均绝对差
# 逐秒亮度 / 暖色度曲线：唯一可靠的光线验收，别只目测拼图
ffmpeg -y -v error -i final.mp4 -vf "fps=1,scale=268:-1" -frames:v 39 qa/_sec/%02d.png
# 再对每张算 L=(R+G+B)/3 与 R-B，打印曲线；出现 5 个峰谷 = 段内重演光线
```
接缝 MAE 应 < 5；> 20 说明 `first_frame` 没接上或段被单独重跑过。
**接缝 MAE 只证明构图接上，不证明光线接上**；逐秒 `L` 曲线呈 5 个峰谷即段内重演光线，不算单调。
叠化版总时长应 = 5×段长 − 4×0.25 = 39.00s（比硬切版少 1 秒，这也证明 xfade 真的生效）。

### 已知边界

- `first_frame` 是近似复现、不是 byte-identical（MAE 2.x 而非 0）。
- 空镜板是**语义级**参考：机位变化时透视不会严格对齐，仍不是同一套 3D 空间。
  要逐像素锁死背景，得走「人物图 + 唯一空镜板」双图关键帧链路（旧默认）。
- 段间过场（叠化 / 硬切 / 黑场）只能掩盖**段与段之间**的机位跳变，
  掩盖不了段内光线回跳；而且实测提示词也压不住，只能靠「段内不做时间推进 + 后期全片 grading」。
- 接缝 MAE 与逐秒亮度曲线是**两个独立指标**：前者验构图，后者验光线。
- 短叠化会让成片比"5×段长"少 `(段数−1)×0.25` 秒；要严格凑 43.2 秒得另配段长。

### 环境变化（2026-11-10 23:24 起）

`qwen3vl_32b_minimax_h3_int8_convrot.safetensors` 已从 `models/text_encoders/` 消失，
现存同系列 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`。
**ComfyUI 重启刷新模型列表后才会报 `value_not_in_list`**（跑着跑着段 3 突然失败就是这个）。
驱动副本与 cfg 的 `clip_name` 已换成 nvfp4；用户的原工作流
`user/default/workflows/H3-SelfLift_单图参考.json` 没动，把 int8 放回去即可恢复。

## 逐段人在回路：Director 面板第 5 个 tab（2026-11-11 用户定）

用户 07:09：「以后长视频就用这个方式来做，那 direct 工作流就得做修改了，
先一段段跑，然后我可以修改每段的首尾帧，然后改提示词」。
确认过的两个选择：**重出 + 换首帧图**（不是回到旧「先生成首尾帧」）、
**交互做在 ComfyUI Director 面板**（不是本地 HTML 页）。

### 改造落点（**没有动控件顺序**）

| 文件 | 作用 |
|---|---|
| `h3-long-video-repo/selflift_panel.py` | 新增 8 个 HTTP 路由 + 异步任务状态机；只做编排，出片仍调 `run_selflift.py` |
| `h3-long-video-repo/director.py` | 只加 3 行胶水 `register_routes(...)`；旧 18 个路由与 `execute` 完全不动 |
| `web/director.js` | 新增第 5 个 tab「SelfLift 逐段」，**默认打开**；旧 4 个 tab 原样保留 |
| `scripts/run_selflift.py` | 新增 `status` / `set` 命令、抽真实首帧、`first_frame_override`、`stale` 判定 |
| `tests/test_selflift_panel.py` | 静态回归：路由数、路径白名单、tab、控件数 22 不变 |

**关键：这次没加任何 widget**。Director 节点仍是 22 项 `widgets_values`，
所以 `example_workflows/H3-Long-Video-Director.json`、`build_director_workflow.py`、
`test_graph.py` 的顺序断言都不用改（回归测试里有 `test_widget_count_unchanged` 兜底）。
以后若真要加控件，仍必须追加到**末尾**并同步那 4 处。

### 面板能做什么

1. 打开 `H3-SelfLift_逐段导演.json`（已默认停在 SelfLift 逐段 tab）→ 填分镜 json 绝对路径（或「新建片」从 skill 模板开目录）→ 点「打开 / 刷新」
2. 每段一张卡：**真实首帧 / 尾帧**缩略图、seed、提示词 textarea、状态徽章
3. 「生成本段」/「按此 seed 重出」/「换随机 seed 重出」→ 只跑这一段，不连跑 5 段
4. 「设为本段首帧图」上传任意图片 → 写段级 `first_frame_override`，
   **优先于上段尾帧**（`run_seg` 先判 override 才判 chain）
5. 「恢复自动承接」清掉 override，回到尾帧接首帧
6. 「保存提示词」直接写回 `segments[].body`（`head` / `tail` 全片共用锚不动）
7. 全段就绪后「拼接成片」，`dissolve`（默认）/ `cut` / `black` 可选

### 后端路由（`/h3-long-video/selflift/*`）

| 路由 | 作用 |
|---|---|
| `GET status?cfg=` | 逐段状态：首/尾帧与视频 URL、seed、body、done/stale、成片列表、当前任务日志 |
| `POST set` | 改 seed / body / first_frame（`auto` 清覆盖），打 `updated_at` |
| `POST run` | 异步跑单段（3~4 分钟，线程 + 轮询，不卡 HTTP）；带 seed 先写回再出片 |
| `POST merge` | 异步拼接 |
| `POST init` | 从 `assets/segments.selflift.example.json` 复制出新片目录 |
| `POST upload` | 图片存进 ComfyUI input，写 `first_frame_override` |
| `POST frame` | 改尾帧：`next_first` 替换尾帧供下段接续 / `last_lock` 锁定本段尾帧 |
| `GET media?path=` | 片目录 / input 下的图片与视频，白名单防路径穿越 |

### 尾帧怎么改（2026-11-11 07:51 用户指出首帧能改、尾帧不能改，已补齐）

`MiniMaxH3AudioConditioningT8` 的 optional 输入里**本来就有 `last_frame`**，
之前只是驱动没接。面板现在每张卡的尾帧下方有「↓ 下载」+ 三个按钮，两种语义要分清：

| 按钮 | 落到哪里 | 本段视频会变吗 | 代价 |
|---|---|---|---|
| **替换尾帧→下段首帧**（`next_first`） | 覆盖 `refs/<prefix>_sNN_last.png` + 写下一段 `first_frame_override` | **不变**，只换喂给下游的那一帧 | 覆盖尾帧会让后续段自动 `stale`，需按序重跑；末段禁用 |
| **锁定本段尾帧**（`last_lock`） | 写本段 `last_frame_override`，驱动接 238 节点的 `last_frame` | **要重出本段才生效** | `task_type=auto` 会切 FL2VA/Hybrid，端点近似复现、运镜自由度下降 |
| 解除尾帧锁 | `set --last-frame auto` | — | 清掉 `last_frame_override` |

- `last_frame_override` **默认关闭、实验性**：连线与 API 图已验证（回归测试
  `test_last_frame_reaches_api_graph`），但**生成行为还没实跑过**。开了以后段内运动受两端约束，
  可能退回你不喜欢的「端点形变」观感；先拿一段试，别整片开。
- 两种改法都不改本段已经渲染出的最后一帧；要本段画面真的不同，只能换 seed / 改提示词重出。
- **手动编辑过的帧（换头、指令编辑）喂下游前必须做 CIE Lab 色彩对齐**，
  否则色偏会被逐段复刻放大。

### 铁律（这次新增）

- **串行依赖**：段 N 的 `first_frame` 来自段 N−1 尾帧。改了第 N 段的提示词 / seed / 尾帧，
  第 N…末段自动标 `stale`，必须**按序重跑**；只重出中间一段不算接上。
- `stale` 两种触发：段级 `updated_at` 比视频新，或上段尾帧比本段视频新；命中一次就向后传染。
- **生成实现仍唯一**：面板不复制出片逻辑，全部 subprocess 调 `run_selflift.py seg N --cfg ...`。
- **改 `director.py` / `web/director.js` 要重启桌面版**才生效；
  绝不用 Stop-Process 杀后端、绝不另起临时 ComfyUI。
- 面板默认 cfg 只是片例占位，别拿它当默认路径；新片用「新建片」。

**⚠ 长视频不要点节点画布上的「运行」按钮**：那个仍走旧 Director 链路
（`execute` → `run_h3_long_video.py` → 首尾帧 → push/resume），本次没改它。
新链路完全在「SelfLift 逐段」tab 里，不经过节点 execute；旧 4 个 tab 只在
逐像素锁背景 / 竖屏短片时才用。

### 边界

- `first_frame_override` 只锁第 0 帧，段内光线仍会自演亮度循环（见「验收命令」）。
- 空镜板是语义级参考，机位变化时透视不严格对齐。
- 上传的图不做 Lab 色彩对齐；若来自过编辑模型，喂之前自己先对齐。

### 已知坑：Director 面板加载即整段崩溃（director.js insertBefore 顺序，2026-11-11 修）

- **症状**：加载 `H3-SelfLift 逐段导演.json`（或任何带 `H3LongVideoDirector` 节点的工作流）后，
  自定义面板——含第 5 个「SelfLift 逐段」tab——**整个挂不上**，节点只剩标准参数控件；
  浏览器 Console 能看到 `Failed to execute 'insertBefore' ... is not a child of this node` 的 DOMException。
- **根因**：`web/director.js` 的 `installPanel` 里
  `keyframeTab.pane.insertBefore(backendNote, editor)` 在 `editor` **还没成为 `pane` 子节点**时
  就把它当参照节点，抛异常使整个 `installPanel` 中断，后续的 `addDOMWidget(面板)` 永不执行。
- **触发条件**：只要 `H3LongVideoDirector` 节点被创建（`nodeCreated` 钩子触发）就必崩——
  **所有用到该节点的工作流都会中招**，不是偶发，也不是只影响 SelfLift tab。
- **修复**：改成先 `insertBefore(editor, keyframes)` 把 editor 挂进 pane，再在 editor 前插 backendNote
  （语义不变）。修复 commit `7535972`（已 push `main`）。
- **生效方式**：前端 JS 热更新，**用户只需在 ComfyUI 浏览器里刷新页面**即可，不用重启后端。
- **排查手段**：若面板又挂不上，用本机无头 Chrome + CDP（`--no-proxy-server --proxy-bypass-list="<-loopback>"`
  才能连 127.0.0.1:8188）驱动 ComfyUI 截图；若 `document` 里搜不到 `[class*="h3-director"]` 即面板未挂载。

## 一致性来源：素材 / 资产 / 契约（2026-11-09 新增）

**核心判断：有没有外部真值，决定一致性从哪来。** 把复刻的做法套到普通生成上会翻车，
反过来也一样。

```
开工：手里有什么？
│
├─ 有原片视频（复刻）
│    ├─ 允许 H3 补中间帧 → 抽每刀 first/last → source_frames：两帧各从对应原帧独立重绘
│    │    一致 = 素材本来就同源（同模型 / 同提示词 / 同 seed / 同预处理）
│    │    绝不用首帧 A 当输入去推尾帧
│    └─ 一帧都不许变 → frame_exact：每一帧各自重绘，不经过视频模型
│         一致 = 输出帧与源帧一一对应；构图/动作/画幅/fps/时长全部继承
│
└─ 没有原片（普通生成）
     造标准资产：人物参考图 + 一张无人空镜板（全片唯一）
       ├─ 首帧：图1=人物、图2=空镜
       ├─ 尾帧：图2=本段首帧（沿用 11-05 定稿链路）
       ├─ 段间：chain 尾帧=下段首帧（byte-for-byte）
       └─ 一致 = 资产复用 + 固定契约 + 量化质检
```

### 五层一致性要分开治

| 层 | 锁什么 | 手段 |
|---|---|---|
| 身份 | 脸、体型、发型、服装 | 人物参考图 / FaceID / 角色 LoRA；**身份只生成一次，之后复用** |
| 场景 | 布局、道具位置、光位、左右关系 | **背景资产化**，不逐镜重生成；`space_lock` 固定方位词 + 180° 轴线 |
| 画风 | 渲染质感、调色、颗粒 | 固定提示词锚块、全局 LUT / 颗粒、同模型同档位 |
| 动作 | 段间方向、速度、视线 | `chain`、首尾帧插值、短叠化 |
| 时间 | 段内不闪烁、不融脸 | 视频模型时序层 + `frame_bad()` 画质闸门 |

**背景一致 ≠ 人物一致**：两条独立链路、两套手段，别指望一句提示词同时解决。

### 背景资产化（硬规则）

- 一张**无人、干净、一次定稿**的空镜板 = 全片唯一背景源。各镜从它派生
  （crop / pan / zoom / 视差 / 轻微布光），**绝不逐镜重新生成背景**。
- 提示词是语义级的（"一个石砌房间"），语义相同 ≠ 门窗位置、光位、地平线相同；
  每镜重生成一次背景 = 每镜重抽一次空间布局。
- **不把生成结果回喂当空镜**（瑕疵逐代放大）；空镜必须独立生成。
- 换机位先备同空间的 2–4 个机位板，或用深度 / 法线 / 线稿条件锁几何；
  分镜写 `space_lock`（门在左、窗在右、主光左上）与 `camera_move`，
  防止左右关系翻转。
- 最稳的实操：**一镜到底再剪**——先出一段背景连续的长视频，再按节奏切多刀。

### 量化闸门（提示级，不阻断出片）

`scripts/consistency.py` 在首/尾帧背景区（外圈 18%）量四项：

| 指标 | 阈值 | 抓什么 |
|---|---|---|
| `bg_ssim` | < 0.72 | 背景结构被重画 |
| `bg_changed` | > 35% | 背景大面积改动 |
| `bg_hist_corr` | < 0.75 | 光位 / 调色漂了 |
| `bg_layout_corr` | < 0.70 | **颜色没变但门窗家具挪位**（空间重抽卡） |

`frames` / `source_frames` 出完尾帧自动跑一次，结果写进 `state` 的 `bg_qa`；
两条链路**共用 `consistency.report()` 这一个出口**（阈值、标签、打印格式只有一份实现，
不要各自拼字符串），光效题材（`lighting_fx`）或机位推拉（`camera_move`）只标 `[存档]`，
其余标 `[提示]`。
和既有 `frame_bad()` 一样：**提示级问题不挡出片**，由人工确认点和换种子闸门兜底。
`check` 会检查一致性契约（缺 `space_lock`、给了 `bg_ref` 的资产纪律、
检测到 `source_frames` 时提醒别跑 `frames`）。

### 复刻链路怎么用

```bash
# 分镜里写 source_frames（见「参数配置」），然后：
pipeline.py source_frames --storyboard sb.json                 # 全部段（自动断点）
pipeline.py source_frames --storyboard sb.json --segs 4,8      # 只跑指定段
pipeline.py source_frames --storyboard sb.json --segs 4 --force# 强制重绘
# 结果写进 state 的 first/last，后续照常：
pipeline.py push --storyboard sb.json && pipeline.py resume
```

`source_frames` 自带：4 次失败重试 + `wait_service()`、同段首/尾同 seed、
配置指纹断点（prompt / 工作流 / seed 一变自动重绘，不会「改了提示词还在跳过」）、
把历史驱动留在 `out_dir` 的成对帧自动 adopt 进 state（旧产物不作废）、
每段跑完统一背景 QA。**不要在 capsule 项目里另写一套重绘驱动**，用
`drive_restyle.py` 这类薄壳把分镜翻译成 pipeline 格式再转发即可。

### 逐帧精确复刻（`frame_exact`，2026-11-09 新增）

**什么时候用**：用户要「这一刀的动作/机位/光效一帧都不许变，只把人物或画风换掉」。
它和 `source_frames` 的区别是中间帧：`source_frames` 只重绘首/尾两帧，中间交给 H3
插值；`frame_exact` 把**每一帧**都从对应源帧重绘，不调用任何视频模型。

```bash
# 分镜里写 frame_exact 配置（见「参数配置」），然后：
pipeline.py check --storyboard sb.json          # 专用校验，不套普通生成规则
pipeline.py frame_exact --storyboard sb.json   # 直接产出成片
# 或独立调用：
python scripts/frame_exact.py --params params.json
```

分镜字段：

```json
"frame_exact": {
  "source_video": "C:/.../原片.mp4",
  "start": 1.433, "end": 5.433,
  "output_path": "D:/.../output/release/video.mp4",
  "seed": 20261109,
  "identity": "一位 26 岁日本男性，深红长发，黑金长大衣",
  "characters": [],
  "mode": "full",
  "temporal_smooth": 0.12,
  "max_frames": 130,
  "keep_audio": true
}
```

`mode=bg_lock` 时人物框外锁源像素；`characters` 是连续段跨角色时按
`{start, end, identity}` 切，段内文字恒定；`max_frames` 默认 80。

**⚠ 身份锚必须显式写死性别**（2026-11-09 用户反馈）：逐帧是独立扩散请求，
没有跨帧记忆；把「性别按参考图猜」写进提示词，模型会每帧重猜一次，红发男性
一会儿偏男一会儿偏女。`identity` 会被包成 `IDENTITY_RULE` 放在提示词最前面，
同角色段内每帧文字完全相同；跨角色用 `characters` 按帧时间切。
每帧用的锚记在 `qa/frame_exact_qa.json` 的 `identity_by_frame`，没写会告警。

**必须绕开的硬规则**（照搬普通链路会毁掉「精确」）：
9:16 归一化 / 扩图、5s 段长下限、17n+5 帧阶梯、`open_action`/`motion` 必填、
`reference_images` 必填、`frames → push → video` 三阶段。画幅、fps、帧数、
时长、音轨一律继承源片；分刀生成时才用 `merge` 拼时间轴。

产物：`release/video.mp4`、`qa/frame_exact_qa.json`（逐帧结构对齐 + 抖动）、
`qa/run_notes.json`、`artifact_manifest.json`。QA 主指标是 `align_lowfreq_mean`
（低频明暗布局相关）；`align_edge` 在 CG→真人这类域转移下实测只有 0.03–0.05，
**只作参考不作验收线**；`flicker_ratio ≈ 1` 表示抖动与源片相当。

**单一实现**：全部逻辑在 `scripts/frame_exact.py::run(cfg)`。
capsule-cinema 的 `LocalFrameExactRestyleTool` 只是参数校验薄壳，调的是同一个函数；
不要在两个 skill 里各写一份逐帧逻辑。`frame_exact.py` 复用 `source_frames._run_one`
与 `DEFAULT_PROMPT`，保证画风/工作流/节点映射与复刻链路一致。

限制：源字幕/UI 会被重绘抹掉（后期烧回或用 `bg_lock`）；对齐是结构级不是像素级；
同 seed 不锁跨帧身份；实测 **18s/帧**，全片 23s/697 帧约 **3.5 小时**。

**观感上限（2026-11-09 用户反馈「效果不怎么样」，如实记录）**：逐帧独立扩散
没有时序模型，脸、材质、光效在帧间各自微变；4 步 Lightning 蒸馏配置画质上限低，
叠「重磨皮」又抹细节；`temporal_smooth` 压抖动会引入运动模糊。49 帧实测
`align_lowfreq=0.716 / min=0.447`、`align_gray=0.523`、`align_edge=0.031`、
`flicker=0.864` —— **QA 达标只说明构图没跑偏，不代表好看**，别当最终成片交付。
主体成片走 `source_frames` 两帧重绘 + H3 首尾帧插值更稳；逐帧更适合短刀的
结构级对齐验证与画风试验。详见 capsule-cinema `references/frame-exact-restyle.md`。

**别默认换成「锚帧窗口链」**（2026-11-09 一次对照实测）：每 22 帧重绘首尾锚、
中间交给 H3 `first_last_frame`，在迪卢克段上 `align_lowfreq` 从 0.716 掉到
**0.325**、接缝跳变 1.7×，动作塌缩成站姿横剑；锚帧重绘本身就会把蹲姿 / 举剑
「摆正」，H3 再丢一层。省时（4m07s vs 14m27s）买不到可用画质。
详见 capsule-cinema `references/frame-exact-restyle.md`「替代方案实测」。

### 唯一源码与同步（2026-11-09 收口）

活跃源码只有一份：`.workbuddy/skills/h3-long-video/`（capsule 适配器、所有 drive
脚本、`pipeline.py` CLI 都指它）。`ComfyUI-Installs/h3-long-video-repo/skill/`
是**同步副本**——节点仓库被 `custom_nodes` 软链引用，里面的
`align_exposure.py` / `anchor_probe.py` / `gen*.py` 会 `import pipeline as P`，
所以副本不能删，但**绝不直接在副本里改代码**。

两边曾各有一个对方没有的修复（活跃版 `comfy_free` + 背景 QA，repo 版链式段首帧
guard），这就是分叉的典型来源。改完活跃版必须跑：

```bash
python scripts/sync_repo.py --check   # 只读，列内容分叉（忽略 CRLF/LF）
python scripts/sync_repo.py --apply   # 同步脚本 + SKILL.md 到 repo（repo 独有文件不动）
# 再跑一次 --check，应为 ALL SYNCED
```

`--check` 返回非 0 = 有分叉，适合当提交前闸门。

权衡不变：**背景锁得越死，机位与调度自由度越低**。叙事片用「多机位资产 +
一镜到底再剪」的混合式，不要把所有镜头都钉在一张板上。

## 两张参考图（首尾帧的心智模型，**2026-11-05 18:16 已接入 pipeline**）

> 这一节是**① 有参考图 / 图生视频**链路，用户已确认掌握。
> 用户没给参考图的文生视频：先用 `refs` 造出这两张图，然后**回到本节**走同一条双图链路；
> 规则见后面「文生视频」章节。

```
image_1 = 人物参考图    管身份 / 长相 / 服装（权重最高）
image_2 = 场景空镜      管场景大类 / 构图 / 光影基调，必须没有人
        → V6 原版双图直出 + 随机 seed（禁 100000）
```

- 场景空镜**从零生成**（空白画布 + 背景提示词）或单图编辑移除人物；
  不用抠图 / 透明底，不把生成结果喂回 image_2（会代际累积锐化）。
- **不量化、不抠图、不合成、不加节点**：那些是 seed=100000 污染下推出的过度方案。
- 机位 / 景别 / 人物比例 / 光影都靠提示词调度，同一张空镜能出远景、半身、仰拍、
  侧 45°、特写；每段首帧写 `camera` + `shot` + `light`，尾帧继承首帧机位。

### 分镜里怎么写

```json
{
  "reference_images": ["qt_person.jpg"],
  "bg_ref": "street_bg.png",
  "space_lock": "门在画面左侧、橱窗在右、主光来自左上，全片不镜像",
  "keyframe_lora": "viggle6",
  "segments": [{"camera": "镜头：平视全身景…", "light": "午后侧逆光…",
                "camera_move": "缓慢拉远", "lighting_fx": true}]
}
```

- `bg_ref` 存在 → 新链路：图2 **原样喂、不量化**、不固化首帧当锚、跳过亮度对齐；
  没写 → 兼容旧链路（`scene_ref` / 固化锚 / `quant_anchor`），`check` 会提醒。
- `space_lock` 是背景一致性契约（固定方位词，防多机位左右翻转）；
  `camera_move` / `lighting_fx` 让背景 QA 只存档不刷提示。
  `bg_ref` 视为**全片唯一资产**：不要逐镜重生成、不要把生成结果回喂当空镜。
- **首帧模板已删「同一机位同一景别」**，改由 `camera` / `shot` / `light` 定机位；
  **尾帧模板保留机位尺度锁**（图2 = 本段首帧），保证首尾不跳、装束一致。
- 画质闸门换成 `frame_quality()` 三项：hf 只存档，**强边缘均值 >200 或人物区 >35% 才拦**。
  不再用「全图 hf > 20%」——街景的橱窗框 / 石板缝是合法硬边，hf 天然 24~26% 会被误杀。
- `keyframe_seed` 默认按分镜路径派生（不同片子不同、同片可复现）；显式写 100000 会自动替换并警告。

### 两条命令

```bash
# 正式：按分镜出首尾帧
pipeline.py frames --storyboard sb.json --seg 1

# 试构图：两张参考图直接出一张，不写分镜也行
pipeline.py direct --ref qt_person_916.jpg --bg street_bg.png --prompt "…" --out direct_out.png
# 或复用分镜里的两张参考图与该段首帧提示词
pipeline.py direct --storyboard sb.json --seg 1 --out direct_out.png
```

`direct` 会打印 hf / 强边缘 / 人物区与画质闸门结果。

### 导演台 `H3-Long-Video-Director` 也已接两张参考图（18:53）

`director.py` 的 `INPUT_TYPES` 本来就有 `reference_1`（必填）+ `reference_2..9`（可选），
`config.json` 默认也是两张——**不用改 director.py、不用重启 ComfyUI**，只改数据层：

| 位置 | 改动 |
|---|---|
| `_new_director_job` | `reference_images = [reference_1.png 人物, reference_2.png 场景空镜]`，并写 `cfg["bg_ref"]` |
| `write_director_workflow` | node 1 = 人物图、node 2 = **场景空镜**（原来两张都是同一张人物图）、node 5 = 首帧 |
| 用户工作流 `user/default/workflows/H3-Long-Video-Director.json` | 3 个 LoadImage 默认值清成 `placeholder.png` + 中文标题；不再留别人的真人图 |
| repo 模板 `example_workflows/H3-Long-Video-Director.json` | node 1/2/5 加中文标题标明语义 |

**顺手修了一个真 bug**：`_new_director_job` 原本在参考图处理**之前**就把 `config.json`
写盘了，改动只留在内存里 → 新任务永远继承模板里的旧真人图（就是之前那个
「身份锚接错人」的残留）。现在改成最后再落一次盘，已复验。

**边界**：第二张必须是**无人的场景空镜**，不能是第二张人物照——
repo `build_qwen_keyframe_graph` 源码警告「canvas + 多张身份参考会画出两个人」针对的是
人物照；空镜里没人，正好充当 `image_2`。而且 `push` 把首尾帧写成 manual 关键帧，
Director 不会自己出关键帧，这条风险更低。

**怎么用**：界面里那张是中性占位模板，真实的两张图由
`pipeline.py push --storyboard sb.json` 生成的 `<分镜>.director.workflow.json` 烤入；
也可以直接在界面里把人物图和场景空镜拖进标好标题的两个槽位。

验证：新任务 `config.reference_images = [reference_1.png, reference_2.png]`、
`bg_ref = reference_2.png`；工作流 node1=`qt_person_916.jpg`、node2=`street_bg.png`、
node5=`bgtest_s01_first.png`。

**端到端实跑通过（19:15–19:19）**：`push --storyboard sb_bgtest.json` → `resume`
→ 6 秒成片 `ComfyUI-Shared/output/h3_long_video/long_video.mp4`
（832×1472 / 24fps / 144 帧），`status=completed`、1/1 段。
抽 5 帧（0.2 / 1.5 / 3.0 / 4.5 / 5.6s）：**人脸数全部 = 1**（没有第二个人），
服装与街景连贯，转头 → 侧身 → 抬手扶玻璃动作连续，画质闸门 5/5 PASS
（hf 10.7~19.8%、强边缘 125~146、人物区 10.1~14.1%）。
→ **之前担心的「两张参考图画出两个人」在导演台链路里没有发生**；
第二张是无人空镜 + push 写 manual 关键帧，风险确实低。
样本 `output/shopping/e2e_frames/e2e_sheet.png`。

- 实跑验证：`sb_bgtest.json` 一段跑通，首帧一次过、质检 PASS、位移 0.16；
  `direct_bgtest.png` 中近景也成立（hf 17.5% / 强边缘 135 / 人物区 14.9%）。
- 详见本文件「seed 100000 是坏种子」「景别 / 机位 / 光影」两节、手册 §7.16.2 / §7.16.3。

### 3 段实跑：分镜提示词要「贴合两张参考图 + 逐段换镜头」（19:44–19:53）

用户要求「重新编一个靠谱的分镜提示词，搞 3 段看看效果」。旧 `sb_bgtest.json` 的
`style` 写「傍晚商业步行街 / 暖黄橱窗灯 / 街灯虚化」，但 `street_bg.png` 其实是
**白天柔和天光的现代商业街**（黑框玻璃橱窗 + 米色石墙 + 浅灰石板路 + 远处行道树）——
景物写错、模型只能半猜，这就是"不靠谱"的根因。

新分镜 `output/shopping/sb_3shot.json`（3×5s = 15s，`check` 零警告）：

| 段 | 景别 / 机位 | 人物占比 | 光影 | 动作（只归一段，收尾停住） |
|---|---|---|---|---|
| 1 | 平视全身远景 | ~55% | 白天漫射天光，正面柔光 | 站定 → 左转 30° 看橱窗 → 右手抬起指向并停住 |
| 2 | 侧 45° 腰部以上 | ~80% | 橱窗暖黄光近距离侧打脸 | 上身前倾 → 手掌贴上窗框 → 低头看陈列 |
| 3 | 略高机位俯拍中景 | ~70% | 午后侧逆光 + 轮廓光 + 斜向淡影 | 收手 → 直起站正 → 转头望街尽头，不回正 |

写法要点（以后照这个抄**结构**，别抄内容）：

1. `shot` / `camera` / `light` / `frame` 各写各的：`camera` 管机位+景别+人物占比，
   `light` 管方向/色温/影子，`shot` 只写场景里真实存在的东西；
2. 每段 `scene_items` 用正面清单，且必须和该景别能看到的物件一致
   （远景写街道纵深，中近景只写窗框和暖光）；
3. 段 2/3 用 `chain: true` + `open_action` 明写「承接上一个镜头收尾的姿态」，
   因为 2.1 后端**不复制首帧**，接缝靠文字对齐 + 短叠化；
4. 每段 `beats` 三拍（时间锚自动摊到 1.5/3.0/4.5s）、`soundscape` / `music` 各写各的，
   转头只写 30° + 侧面轮廓，别写「完整侧脸」。

**实跑结果**：`frames` 6 张（7m33s）→ `push` 任务 `20261105_194406_b5f61c15`
（3 段 manual 关键帧、reference_1 人物 / reference_2 空镜）→ `resume` 约 9 分钟
→ 成片 832×1472 / 24fps / 317 帧 / **13.21s**（Director 内部剪了约 1.8s 静止帧），
`status=completed`、3/3 段，QA 全绿（首/尾帧 SSIM 0.976~0.986、static_ratio 0、零 flags）。
关键帧 `output/shopping/s3_frames/s3_sheet.png`、成片抽帧 `s3_video_sheet.png`、
成片 `s3_3shot_final.mp4`。
肉眼：三段人物比例 / 街景角度 / 光影确实逐段变化，服装与长相 6 帧一致，
无第二个人、无桌子白边、无斑驳锐化；段 2 明显偏暗是设计（暖光侧打），三段亮度跳变属切镜。

### ⚠ 两条闸门 / 合并边界（这次实跑暴露）

- **亮度闸门误杀分段光影**：`cmd_frames` 原来固定拿「段 1 首帧」卡全片 ±8 硬 FAIL，
  段 2 被判 -22.6 / -19.8、段 3 -8.0，换种子重出也修不掉（提示词决定的明暗）。
  现在 `bg_ref` 新链路 + 段级 `light` 时改为**只查段内首/尾曝光差 >±8**，
  跨段差异降级为 `[提示]` 存档。重算质检后 3/3 PASS——
  已有帧文件不会被覆盖，直接 `frames --seg 2` / `--seg 3` 秒级重算即可。
- **Director 内部合并是硬切 + 剪静止帧**，不读分镜的 `join_dissolve_seconds`。
  三段本来就是不同机位的切镜，硬切符合电影语言，实测无双影；
  但 15s → 13.21s 少了约 1.8s。要短叠化得走 pipeline 的 `merge --join-dissolve 0.25`。

### ⚠ 首尾帧必须真链式：`chain` 不是装饰（20:00 用户指出「direct 流程首尾帧没连到一起」）

**现象**：19:44 那版 3 段视频看着没问题，但段 1 尾帧与段 2 首帧是两张独立生成的图
（还故意换了机位），在 Director 关键帧面板里一眼就看出没接上。
用户原话：「昨天 skill 不都弄好了吗 你看看文档」。

**文档依据**（本来就有，这次没照做）：

- `repo/docs/工作流关系.md` §3.2：`chain_segments=true` 时「段 N 的尾帧**就是**
  段 N+1 的首帧，同一张图，不是相似的两张」；首帧优先级
  「手动替换的 `segNN_start_manual.png` → 上一段尾帧（链式）→ …」。
- 本文件「改尾帧 = 整条链会往下重出」：`chain` 段首帧就是上一段尾帧（byte for byte）。
- `Q21_LAST_TMPL` 明写「图2 是本段首帧，是同一个镜头」——首帧不继承，这句是假的，
  尾帧的机位/服装也就没有同一镜头的硬基准。

**根因（两处，都已修）**

1. `cmd_frames`：2.1 后端的 `chain` 分支原来只打印「图2 场景锚恒定」，不复制上段尾帧
   （只有 2511 复制）。现在两个后端都 `shutil.copy(prev_last, first_name)`，
   `rec["first"]` 直接指向副本。
2. `cmd_director_push`：原来给每段都写一张独立的 `segNN_start_manual.png`；
   manual 优先级最高，把 Director `ensure_keyframe_chain()` 的
   `start_frame = 上段 end_frame` 继承顶掉了。现在 `chain:true` 且 i>1 时
   `first_src = 上一段 last`；`state.keyframes[i].continue_from_previous` 按分镜写，
   不再写死 True。

**语义必须分开写**（这次最重要的规矩）：

| 场景 | 分镜写法 | 首帧来源 | 机位 / 光影 |
|---|---|---|---|
| 连续动作 | `chain: true` | 段 N 尾帧（byte-for-byte） | 全链同一机位，不许换 |
| 切镜 | `chain: false` + `transition: cut` | 本段独立生成 | 可以换景别 / 角度 / 光影 |

上一版三段故意换了三个机位，就该是切镜；写 `chain: true` 又各自生首帧，等于两边都不靠。
`sb_3shot.json` 已改成切镜版（chain:false + cut），
`sb_3shot_chain.json` 是链式版（同一机位贯穿三段）。
`check` 也改成：写了 `transition` 的切镜段不再提醒「未设 chain=true」。

**实跑验证（20:14–20:42）**：`frames` 后 md5 两两相等
（`s3c_s01_last` == `s3c_s02_first`、`s3c_s02_last` == `s3c_s03_first`）；
`push` 建任务 `20261105_203221_2f6a3df9`，灌进 Director 的 manual 帧同样 md5 相等、
`continue_from_previous` 全 True；`resume` → 3/3 段 `completed`，
成片 832×1472 / 24fps / 296 帧 / **12.33s**（Director 内部仍剪静止帧），
QA 全绿（首尾 SSIM 0.983~0.986、static_ratio 0、零 flags）。
相邻帧灰度 MAE 中位 1.35、段间峰仅 5.9（上一版三机位硬切是整张换构图），
抽帧无跳变 / 换人 / 双影。
产物 `output/shopping/s3_frames/`：`s3c_chain_final.png`（4 帧动作链）、
`s3c_chain_video_sheet.png`、`s3c_chain_final.mp4`。

**顺带修的第二个闸门**：`bg_ref` + 段级 `light` 时，过曝阈值 `ao+0.015`（≈1.7%）
会把白天街景玻璃反光 / 天空的 2.0% 判成「墙面死白」；
现在放宽到 `max(0.030, ao+0.020)`，真过曝仍拦得住。
和上一节的曝光门一样，改完用 `frames --seg N` 秒级重算，不重出图。

**遗留边界**：段 2 的 `disp 0.10 / arm 0.19` 仍差闸门 0.02 / 0.01
（编辑模式从「指向」到「掌贴窗框」幅度保守），肉眼动作链清楚、成片没定格；
要彻底过闸得把段 2 换成更大的单臂 / 躯干动作，那会级联重出段 3。

## 阶段与确认点

```
S0 定链路        → 逐帧对齐? frame_exact : 有原片? source_frames : frames
S1 分镜 + 资产   → ① 用户确认分镜（时长/动作/机位/**每段提示词**）
                    并定稿空镜板 / space_lock 方位词
S2 首帧（每段）  → 质检：比例/头顶/身份/嘴
S3 尾帧（每段）  → 质检：比例/头顶/身份一致/嘴
                    + 背景一致性四项（SSIM/色调/改动量/布局，提示级）
                    → ② 用户看缩略图确认
S4 H3 视频（每段）→ 质检 8 项，不合格按失败码重跑（≤3 次）
S5 合并          → ③ 整片抽样质检 → 交付
```

**不要在 S4 全部跑完才验**。一段一段闭环：这段过了，才做下一段。

## 确认点 ① 要给人两样东西：分镜表 + 提示词

分镜表说"这段拍什么"，提示词说"这段到底喂了什么"。缺了后者，用户只能说
"第二段感觉不对"，改不动具体那一句。

所以 `check` 跑完会自动把每段四份提示词渲染出来，落成两个文件：

| 文件 | 用途 |
|---|---|
| `<name>.prompts.md` | 人读清单，贴进对话用（每段：机位/开场/收尾/节拍 + 首帧/尾帧/视频提示词全文） |
| `<name>.prompts.html` | 预览页：分镜卡片 + 首尾帧缩略图（有 state 就内嵌）+ **可直接编辑的提示词框** |

给用户看的时候用 `present_files` 打开 `.prompts.html`，**别把 12 段提示词全贴进对话**——
那是几千字，用户只会滚过去。要贴就贴当前正在讨论的那一段（`prompt --seg 2`）。

改提示词有两条路，效果一样：

1. **页面内改**：预览页里直接编辑文本框 → 点"导出改后的 JSON" → 用下载的文件覆盖原分镜 →
   接着跑 `frames` / `video`；
2. **对话里说**：用户说"第 2 段尾帧把低头改成侧头"，agent 把整段文字写进
   `segments[1].prompt_last`，重跑 `prompt` 刷新预览。

## 想手动在 ComfyUI 里出一段：`lab/H3-One-Segment.json`

不喜欢命令行的话，这一段可以全在 ComfyUI 界面里做完——**一张图 = 一段视频**，
不用在三个文件之间来回切、也不用存盘再载入（首帧直接接到尾帧的编码器上）。

打开 `example_workflows/lab/H3-One-Segment.json`，要改的只有四处：

| 节点 | 改什么 |
|---|---|
| `#10` LoadImage（标题"参考脸（只换这张）"） | 换人物参考图 |
| `#28` TextEncodeQwenImageEditPlus | 首帧提示词 |
| `#528` TextEncodeQwenImageEditPlus | 尾帧提示词（"保持图1…只改变姿态"那套） |
| `#600` H3SingleClipDirector | 视频提示词 + `seconds`（最短 5） |

点 Queue 就跑：出首帧 → 出尾帧（两张都存到 `ComfyUI-Shared/output/`）→ 自动排视频任务。
实测 720×1280 一段约 **3 分 20 秒**（出图 1'40" + 出片 1'39"），成片 5.0s / 有 AAC 音轨。

注意两点：
- 8GB 显存下 Qwen（19GB）和 H3 会来回换页，所以这张图比分开跑慢一点；批量 12 段还是走命令行更快。
- 这张图**没有质检**。出了图要自己看：人物有没有换、鞋尖是不是同向、首尾帧差距够不够。
  命令行那条链路的 8 项质检、失败码自动重试、段间链式，都在这张图之外。

## 长视频：为什么不能把单段流程套进循环

`lab/H3-One-Segment.json` 只管一段。想用 ComfyUI 的 `StartLoop / EndLoop` 套着它循环 12 次，
**套不了**，两个硬原因：

1. **单段节点是异步的**。`H3SingleClipDirector` 提交一个后台任务就返回（实测：图 1'40" 就返回，
   视频又跑了 1'39"）。套进循环会在几秒内提交 12 个任务，8 GB 显存直接排爆。
2. **段间要传递**。下段首帧 = 上段尾帧，循环节点不负责把上一段的产物喂给下一次迭代。

真正"自带循环"的是 `H3-Long-Video-Director.json`：一个节点吃整段 segments 数组，
内部串行跑（关键帧 → 确认 → 视频），自己接续。它的代价是没有质检、没有失败码重试、
关键帧由它自己生成而不是走 Qwen 首尾帧那套。

转换成它的输入：

```bash
$PY scripts/pipeline.py director --storyboard assets/三镜测试.json
# → assets/三镜测试.director.json（{"segments":[{seconds, two_pass, prompt}]}）
```

粘进 `H3-Long-Video-Director.json` 的 #4 节点 storyboard 框即可。每段的 `prompt` 来自
`DIRECTOR_TMPL`（把开场、收尾、运动并成一句），在 `.prompts.html` 里对应「整段（ComfyUI Director 用）」
那一栏，改词仍然是一段一张卡。

## 让 Director 用我们的关键帧（push / resume）

Director 默认用 MiniMax H3 自己出关键帧，而且**尾关键帧是"跑一段 5 秒草稿取最后一帧"**
（`keyframe_draft_seconds=5`，`unet=DasiwaMinimaxH3_*`），一个关键帧的成本≈一段视频，
画质和身份还不如 Qwen 2511 + 参考脸。

但它的关键帧阶段支持人工指定：`state["keyframe_overrides"]` 命中的帧会被直接采用、不再生成。
所以不要改它的源码（升级会被覆盖），用这两条命令把我们的东西灌进去：

```bash
$PY $SK push --storyboard s.json              # 自动建任务 + 灌首尾帧 + 写提示词和秒数
$PY $SK push --storyboard s.json --job <id>   # 灌进指定任务（段数不符会自动另建）
$PY $SK resume --job <id>                     # 让它接着把视频跑完并合并
```

`push` 做四件事：① 段数不符或没有任务时，抄一份现成任务的 `config.json` 新建任务目录
（**不烧 GPU**，任务 id 形如 `20261101_132345_1fc69e81`）；② 把 `state` 里的首尾帧复制成
`keyframes/segNN_{start,end}_manual.png` 并写进 `keyframe_overrides`；③ 把每段提示词
（`DIRECTOR_TMPL` 渲染的那份）和秒数同步进 `config.json`；④ 分镜里**手写**过
`prompt_first` / `prompt_last` 的段，把那两句原样写进 `start_frame_prompt` /
`end_frame_prompt`——这样面板的「首帧/尾帧提示词」框里显示的就是你自己那句话，可以直接改。
没手写就留空（= 跟主提示词一样），不会把 Qwen 专用的模板词塞进 MiniMax H3 的提示词里。

实测日志（3 段）：

```
[H3] Segment 2: using replaced start keyframe .../seg02_start_manual.png
[H3] Segment 2: using replaced end keyframe   .../seg02_end_manual.png
[H3] Keyframe pair 2/3 ready
[H3] Segment 1: submitted ...
```

**关键帧一张都没用 H3 生成，直接开始出视频。** 于是这条链路变成：
我们的 Qwen 关键帧 + 我们的提示词 → Director 只当"串行出片 + 合并"的引擎。

⚠️ **面板看不到关键帧怎么办**：面板右上角有一个任务下拉框（`GET /h3-long-video/jobs`），
列出 `jobs_dir`（默认 `ComfyUI-Shared/output/h3-long-video/jobs/`）里最近 40 个任务，按修改时间倒序。
没有 `node.properties.h3_job_id` 时，面板会**自动挂到最近的那个任务**上，所以从旧工作流
打开也能直接看到关键帧；需要看别的任务就在下拉框里选。`push` 仍然会顺手生成一份
"已经指向该任务"的工作流 `<分镜名>.director.workflow.json`（job id 写进
`properties.h3_job_id` 和 `resume_job_id`），打开它、点「刷新预览」即可。

## 给任务起名 / 改名 / 删除

- **起名**：节点上 `job_name` 填一个名字，任务号就是 **`日期-名称`**（`20261101-宿舍试穿`），
  文件夹同名；留空仍是 `20261101_153045_9f3ab21c`。空格自动变 `-`，`<>:"/\|?*` 会被剔除。
- **改名**：面板顶栏 `✎ 改名` → `POST /h3-long-video/rename/<job>`，文件夹一起改名，
  `config/status/.state` 里的绝对路径同步改写；日期沿用任务原来那天。
- **删除**：面板顶栏 `🗑 删除` → `POST /h3-long-video/delete/<job>`，删掉整个任务目录，
  再按 `.state.json` 里记的绝对路径删掉它自己生成的分段视频（成片 `long_video.mp4` 和别的任务
  共用的文件不动），返回释放的字节 + 删掉的产物数；`running` / `scheduled` 的任务删不了（先中断）。

## 关键帧用哪个模型（全局配置）

Director 节点最后一个控件 `keyframe_backend`：

| 选项 | 含义 |
|---|---|
| `H3 自己出（默认）` | H3 草稿自动出图（最快，5 秒一段首末帧一起拿） |
| `Qwen-Image-Edit 2511` | **代码建图自动出图**（`build_qwen_keyframe_graph()`），40–90 秒一张，能转角度但脸会漂 |
| `Qwen-Image-2.1` | 同上，20–30 秒一张，最像参考但角度跟参考图走 |
| `只用手动图` | 跳过 AI，任务标「等你放图」，面板逐张「替换」后继续 |

三个 AI 后端分辨率都写死 608×1056（`keyframe_width/height`，32 的倍数）；Qwen 尾帧用上一帧当
画布锁构图（派生帧默认**不喂参考图**，否则同一个人会被画成两个人），提示词自动带「不新增人物」
后缀。模型/步数在 `run_h3_long_video.py` 的 `QWEN_KEYFRAME_PRESETS`，覆盖键见 `docs/参数配置.md`。

**转角镜头**（转身/回头/看侧面）：2.1 转不了角度，2511 转角后脸会漂。走两步混合——
2511 出角度底图，再让 2.1 拿它当**糊画布**（`keyframe_qwen_canvas_blur_mp: 0.12`）+
正面参考照换回原脸（`keyframe_qwen_canvas_refs: true`），出图后逐帧「替换」。
清晰画布下 2.1 几乎不吃参考，糊一下才有效。详见 `docs/Qwen改图.md`「先出角度图、再换回原人脸」。

`example_workflows/Qwen2511-Reference-Edit.json` / `Qwen21-Reference-Edit.json` 保留为**手动
调试用**（试提示词、单张出图），Director 运行时不依赖它们，也不用导入 ComfyUI。
参数和踩坑见 `docs/Qwen改图.md`。

## 在 Director 面板里直接改秒数和提示词

关键帧卡片下方新增了一行：`5 秒 · 提示词摘要 · 改词`（单独写了首/尾帧词时，还会多出
`首帧词` / `尾帧词` 小徽章，悬停看全文）。点「改词」会在关键帧网格上方展开编辑器：
**左边是这一段的首帧/尾帧缩略图，右边三个文本框，顶上是秒数**，边看图边改：

| 框 | 送进哪一步 | 留空时 |
|---|---|---|
| 主提示词 | 这一段视频（H3 首尾帧插值） | 必填 |
| 首帧提示词（折叠，有值自动展开） | 生成这一段的**首帧** | 回落到主提示词 |
| 尾帧提示词 | 生成这一段的**尾帧** | 回落到主提示词 |

三个框分别对应 `segments[i-1]` 的 `prompt` / `start_frame_prompt` / `end_frame_prompt`。
runner 里是 `end_frame_prompt or prompt` 的回落规则，所以**清空 = 回到跟主提示词一样**。

**改哪一栏就只重画哪一帧**（这是重点，别改成"一改就两侧全重摇"）：

- 改尾帧词 → 只清 `end` 的 sha1，只重出尾帧，首帧原样保留；
- 改首帧词 → 只重出首帧；
- 改主提示词 → 只影响那些**没单独写**的那一侧（两侧都写了专属词时，关键帧完全不动，
  只影响视频那一步，面板会说明）；
- 只改秒数 → 一张图都不重画。

两个按钮：

| 按钮 | 行为 |
|---|---|
| 只保存（不动关键帧） | 只写该任务 `config.json` 的 `segments[i-1]`；秒数变了下次出片按新时长跑，图不用重画 |
| 保存并重画关键帧 | 写配置 + 清受影响那侧的非手动帧 `sha1` 并起 keyframes worker |

⚠️ **手动帧默认不动**：`push` 灌进来的、或你在面板「替换」上传的图（`*_source == "manual"`）
不会被提示词覆盖，否则会冲掉辛苦调的图。这时编辑器底部会出现勾选框
**「连我自己放的图一起重出」**——勾上再点保存并重画，才会撤掉那段手动图（override + attempt
一起清，不可恢复）交给 AI 按新词重出。没勾就只返回一句 note 说明为什么没重画。

底层是 `POST /h3-long-video/script/{job_id}/{index}`（body：`seconds` / `prompt` /
`start_frame_prompt` / `end_frame_prompt` / `force` / `redraw` / `two_pass`），
秒数限 1–30、主提示词不能为空，首尾帧词可以是空字符串。返回里的 `sides` 告诉你这次
实际重画了哪几帧，`cascade` 告诉你后面哪几段会跟着重出。

### 改尾帧 = 整条链会往下重出（这不是 bug，要提前说清）

`chain` 为真的段，它的**首帧就是上一段尾帧**（同一张图，byte for byte）。所以：

```
改第 2 段尾帧
  → 第 3 段首帧必须换成新的那张（否则 2→3 之间会跳）
  → 第 3 段尾帧也必须重出（它是「从新首帧推出来的」，旧尾帧对不上了）
  → 第 4 段同理，一直到链尾或遇到 free_end / 关闭 chain 的段
```

4 段的任务改第 2 段尾帧，实际要重出 **6 张图、6–8 分钟**。后端在响应里返回
`cascade: [3, 4]`，面板提示语会写明"后面第 3、4 段的首帧要跟着它走"。

**中途查看时不要以为断了**：上一段尾帧刚重画、这一段还没轮到的那两三分钟里，
state 里存的还是旧文件。`_job_keyframes` 对此做了两件事：

- 链式段的首帧**一律按上一段的尾帧展示**（`start_inherited`），上游一变，下游面板立刻跟着变；
- 同时给 `start_stale = true`，卡片上显示橙色的**「待同步」**，鼠标悬停说明"等下一次关键帧
  阶段就会跟上"。跑完整条链后这个标记自动消失。

所以判定"继承还在不在"只有一个标准：看卡片上是不是「继承」/「待同步」。
两者都说明链是连着的，区别只是这一段轮到没轮到。

**三张按钮的语义别混**（这是踩过的坑）：

| 按钮 | 对 `push` 灌进来的帧 | 对面板「替换」上传的帧 |
|---|---|---|
| 替换 | 覆盖成新图，原图不保留 | 覆盖成新图，**旧图存进 `*_original`** |
| 重画 | 清 sha + 加 attempt，下一次跑关键帧就用 AI 重出这一张 | 同左 |
| 还原 | **按钮置灰**（没有原图可还），提示改用「重画」 | 把 `*_original` 里的 AI 图放回去，不跑 GPU、不排队 |

以前「还原」在无原图时会走 `sha1=""` 分支，卡片立刻变「待重画」，下一次关键帧阶段就重画了——
用户点的是"还原"，却触发了重画。现在后端在无原图时直接 400 拒绝且不动 state，
面板用 `start_has_original` / `end_has_original` 把按钮置灰。

⚠️ **改了 `director.py` 必须重启 ComfyUI**（路由在启动时注册）；只改 `web/director.js`
的话浏览器硬刷新（Ctrl+F5）即可。原始文件备份在
`skills/h3-long-video/backups/director.{py,js}.orig`，`git checkout` 也能还原。

⚠️ 两个前提：`<分镜名>.frames/` 或 `frames` 阶段得先有图（否则那几张还是 H3 生成，
日志里会写 "缺图"）。

## 存储位置：Director 链路在 ComfyUI-Shared 下（2026-11-01 起；分段接续链路有例外）

安装目录只放代码和工作流，**任务、关键帧、分段、成片、测试图一律不进去**：

| 内容 | 路径 | 由谁决定 |
|---|---|---|
| Director 任务（config/state/keyframes/segments） | `ComfyUI-Shared/output/h3-long-video/jobs/<job_id>/` | `config.json` 的 `jobs_dir` |
| 单段流水线任务（`video` / `merge` 的 state） | `ComfyUI-Shared/output/h3-long-video/jobs-single/` | `pipeline.py` 的 `JOBS` |
| 段视频 / 单段成片（SaveVideo 节点） | `ComfyUI-Shared/output/h3_single_clip/`、`.../h3_long_video/` | ComfyUI 启动参数 `--output-directory` |
| 合并成片 | `ComfyUI-Shared/output/h3-long-video/<标题>.mp4`（`merge --out` 可改） | `pipeline.py` 的 `SHARED_ROOT` |
| 参考图 / 首尾帧（LoadImage 读的） | `ComfyUI-Shared/input/` | ComfyUI 启动参数 `--input-directory` |
| 实验截图、对比图 | `ComfyUI-Shared/output/h3-long-video/tests/` | 约定 |

**例外（2026-11-11 分段接续链路）**：`run_selflift.py` 用 `--cfg`，
产物写到**分镜 json 同目录**（`output/<片名>/segments|refs|final|qa/`），
让一个片子自包含、方便多版本并排对比；只有 ComfyUI 节点直接吐出的 mp4
与喂进去的参考图仍落在 `ComfyUI-Shared/output` / `input`。
这不是把产物放回安装目录，别按上表去找新链路的成片。

`jobs_dir` 是 config.json 的新键，Director 启动时读一次；改完要重启 ComfyUI。
老版本默认 `custom_nodes/ComfyUI-H3-LongVideo/jobs/`（其实是指向 repo 的 junction），
现在那个目录已经不存在了，`pipeline.py` 的 `DIRECTOR_JOBS` 也从同一个 config 键解析。

**仓库本身只保留三条流程**（2026-11-01 清理）：`H3-Long-Video-Director`、
`H3-Single-Clip-FirstLastRefs`、`lab/H3-One-Segment`，加上关键帧阶段依赖的
`Qwen2511-FirstFrame916` / `Qwen2511-Reference-Edit` / `lab/Qwen2511-PoseFix-MultiAngle`。
Krea2/Krea3/Qwen21 那几套工作流、示例分镜 `examples/`、对应文档和构建脚本都已删除
（备份在 `skills/h3-long-video/backups/repo-cleanup-*.zip`），不要再照着旧文档找它们。

## 用户自己要动手的三件事，分别在哪做

用户经常想自己接管：改一句提示词、把某张首尾帧换成自己做的图。别让他去翻代码，
就三条路，各自有唯一入口：

| 要做的事 | 在哪做 | 做完之后 |
|---|---|---|
| 改每段提示词 | 打开 `<分镜名>.prompts.html`，卡片下面直接改 → 导出 JSON 覆盖原分镜 | 跑 `frames` / `video` |
| 换某段的首帧/尾帧 | 把图命名成 `s01_first.png` / `s01_last.png` 放进 `<分镜名>.frames/` | 跑 `adopt` 收编 + 质检，再 `video` |
| 按提示词重新出图 | `frames --storyboard s.json --seg 2 --retry` | 图出来后再看预览页 |

`adopt` 是"外部图收编"：转 png → 复制进 `ComfyUI-Shared/input` → 归一化到精确 9:16 →
写进 state（`rec.first` / `rec.last`）→ 跑一遍首尾帧质检（比例/尺度/参考脸/首尾相似/留白/动作量）。
收编过的段，`frames` 会因为 state 里已有图而跳过生成，`video` 直接用这套图。
**用户自己换的图常常是 3:4 或 16:9**，所以默认会裁成 9:16；不想裁就加 `--no-normalize`，
但那样 `video` 和 `merge` 会因为分辨率不齐出问题。

回填字段（分段写只影响那一段，写到根上就是全局默认，**一写就不再套模板**）：

| 字段 | 覆盖的提示词 |
|---|---|
| `prompt_first` | 首帧生成 |
| `prompt_last` | 尾帧编辑 |
| `prompt_pose_fix` | 首帧转身修正（只在 `dir.body` 为 left/right 时才用） |
| `prompt_video` | H3 视频 |

⚠️ 手改提示词的边界：**别把模板里那几句硬约束删掉**（方向锁、服装锁、闭嘴、
固定机位、只有一个人）。它们是拿废片换来的。要放行用 `camera` / `mouth` / `feet` /
`pace` / `extra` 字段（见「强动作镜头豁免」），不要手删。改完跑一次 `check`，
方向契约和幅度校验照常生效。

## 分镜怎么写

一条分镜的硬约束（来自实测）：

| 约束 | 值 | 原因 |
|---|---|---|
| 每段时长 | **基本固定 5 秒**（6 秒是上限） | H3 的 `seconds` 是**硬性输出时长**，最短 5 秒。动作的自然单位是 1–3 秒，两个动作 ≈ 4–5 秒，正好填满 5 秒 |
| 总时长 | **60–120 秒** | 更长则漂移累积、成本线性上升。**60 秒 = 12 段 × 5 秒**，不是 8 段 × 7.5 秒 |
| 单段动作数 | **2 个同向连续动作**（4–5.5 秒） | 一个 0.5–1 秒的动作撑不满 5 秒，空出的部分 H3 会自己补。但两个动作必须**同向同性质**（转头+低头可以），不能对立（站→坐→站必崩） |
| **首尾帧变化幅度** | **随段长递增**：5 秒=中等，6 秒=中偏大，≥7 秒=大（不推荐） | 幅度不够 → 静止/往复走；幅度过大 → 插值崩。见「变化幅度对照表」 |
| **首尾帧人物尺度** | **必须不变（≤1.10×）** | 人变大 → H3 当成推镜，一路推到脸部特写并**重绘成另一个人** |
| 段间关系 | 链式（下段首帧=上段尾帧）或转场 | 决定首帧是复制还是重新生成 |

### 一个动作只归一段（coffee 定稿，2026-11-04）

分段时最容易出的不是"崩"，而是**同一个动作被演两遍**：观众看到"喝一口 → 放下 → 又拿起来喝"。

### 转头类动作：几何描述 + 收尾绝不回正（shopping 定稿，2026-11-05）

首尾帧头朝向一样 → 视频里人脸全程一个方向，且 H3 差异量不够就去"加戏"（推镜换脸）。
shopping 10 段 8 段中招，两条铁律：

1. **转头写可验证的几何描述**，"左转45度"出来只有 ~20°：
   「头部明显向左转，转到鼻尖几乎指向左肩方向、能看到完整的右侧脸颊和右耳的程度」；
   低头写「下巴收向锁骨」；回眸写「能看到完整的正脸而不是侧脸」。
2. **尾帧停在转头位，绝不回正**——"转回平视前方""放下回正微笑"这种收尾等于绕圈
   回原点，首尾帧必然一样。beats 只有两拍：平视 → 转头到位停住。

附带：尾帧含吸管/张嘴时删掉「双唇自然闭合」句；**20° 微转是最危险档**（看着有差异，
不够插值），要么 40°+ 大转，要么换手臂大位移动作。详见手册 §1.22。
机制是：段1 的收尾没停准（过冲到下一个动作），段2 接到的首帧已经错位，只好从自己设计的起点重演。

**转头角度定板：写 30°，别写"侧脸几乎对着镜头"（2026-11-05 全身实测）**

量化口径是 `frames` 打印的 Δyaw（-1~+1 的余弦值），经验换算 **Δyaw ≈ 角度/100**：

| 文案 | 实测 Δyaw | 约合角度 | 结果 |
|---|---|---|---|
| 「鼻尖几乎指向左肩、能看到**完整**的右侧脸颊和右耳」 | 0.45 ~ 0.70 | 50~70° | 超阈值 0.35，**H3 容易演成转身** |
| 「头部向左转约 30 度，能看到右侧脸颊的**侧面轮廓**和右耳，鼻尖偏离镜头」 | 0.02 ~ 0.35 | —— | 目标区间 |

- 阈值在 `frames` 里是 `|Δyaw| > 0.35` 就换种子重出，3 次仍超限只警告；
- **"完整侧脸 + 完整耳朵"这种描述一定会把角度顶到 55° 以上**，因为它要求脸完全侧对；
- 要"看得出转头"又"不转身"，就写 **30° + 侧面轮廓 + 鼻尖偏离镜头**，
  靠手臂动作（扶玻璃/抱臂/叉腰）补足位移，别靠加大转头角度补。

**提示级问题不挡出片（2026-11-05）**：`首尾帧质检` 里带 `[提示]` 前缀的
（主要是"转头幅度偏大"）不再计入 FAIL——实测 0.42~0.50 出片是能用的，
为一条提示每段重出 3 次纯浪费。硬伤仍然 FAIL：尺度 >1.08×、位移 <0.12、
亮度偏离 >8、全图高频 >20%、比例不是 9:16。

**亮度偏离自动归一（2026-11-05）**：2.1 每帧独立出图，同提示词有 ±20 的亮度漂移，
而「偏离场景锚 >8」是硬 FAIL。段2 实测 -12.8 / -18.5，**换种子重出 3 次修不掉**
（那是提示词决定的场景明暗，不是随机噪声）。现在 `frames` 在锚固化之后、质检之前
自动跑一次 `align_exposure()`（均值增益，只动 LAB 的 L 通道），实测 152→160、149→160。

**三条铁律**

1. **切分点落在"可命名的静止状态"**：写 `手已握住杯柄，杯底仍在桌面`，不写 `手正在伸向杯子`。
2. **收尾必须写"停在哪" + 硬约束"X 不离开 Y"**：
   `握住杯柄即停，杯子始终放在桌面，杯底不离开桌面，手臂不向上抬，绝不把杯子端起来`。
3. **段末禁止用会自发续集的动词收尾**（端起 / 拿起 / 抬起）——下一段若也从桌面起手，必重复。

**coffee 三段切分（可直接套）**

| 段 | 起点 | 动作 | 收尾（必须停住） |
|---|---|---|---|
| 1 | 双手在键盘上打字 | 右手移到杯柄并握住 | **握柄、杯底不离桌**（写死"绝不端起"） |
| 2 | 手握杯柄、杯在桌面 | 端起 → 送唇 → 后仰倾斜真喝（含杯沿 1s、吞咽） | **停在唇边，不落桌** |
| 3 | 杯在唇边 | 放回桌面、杯柄归位 | 转头看屏幕、渐渐微笑 |

**同一案例的提示词前后对照（这才是能抄的部分）**

```text
# 段1 motion —— 旧（错）：只写"移到杯柄"，没说停 → 模型握完直接端起来喝了
只有双手和右臂移动，从键盘移到杯柄，上身其余部分保持稳定…
# 段1 motion —— 新（对）：写停在哪 + 硬约束不离开
只有右手和右臂移动，从键盘移到杯柄并握住…全程杯子不离开桌面：握住杯柄即停，不端起、不抬起、不倾斜

# 段2 motion —— 旧（错）：端杯 + 低头就是全部，没有任何"喝"的可画细节
只有右臂端杯和头部微微低下的动作…
# 段2 motion —— 新（对）：写到后仰/倾斜/含杯沿/吞咽
右臂把咖啡杯从桌面端起送到唇边…头轻轻后仰、杯身倾斜，真的喝进一口：
双唇含住杯沿停留约一秒，喉部有吞咽动作…全程杯子不落回桌面，喝完停在唇边等待下一段
```

旧版成片症状 = "碰一下嘴 → 放下 → 又拿起来喝"；v2 修好后 = "握柄 → 端起真喝 → 放下"，
接缝 5.20/5.21、末帧 MAE 9.8/11.5/13.7，一次过。

> 完整案例（帧证据、旧/新全字段 JSON、结果对照表、开工前三步自查）见
> `D:/Comfy-Desktop/ComfyUI-Installs/h3-long-video-repo/docs/模型手册/模型提示词与配置手册.md` 的 **§1.19**。写新分镜前先照它过一遍。

### 动作动词要写到生理细节，否则只演壳

写「喝一口」，模型只会把杯子贴到嘴上——"喝"在画面里没有可画的形状。

| ❌ 空壳 | ✅ 可执行 |
|---|---|
| `啜饮一口` / `双唇接触杯沿` | `头轻轻后仰，杯身倾斜，双唇含住杯沿停留约一秒，喉部有吞咽动作，随后杯身回正` |
| `回头看` | `头部转向画面左侧约四十五度，下巴抬到与肩同高，视线落到身后` |

**`接触 / 碰到 / 贴着` 描述的是接触，不是动作目的**：想让她喝，就要写后仰 + 倾斜 + 吞咽。

### 模板静态锁会和动作打架 → 用段级 `mouth` 覆盖

默认嘴锁是 `双唇自然闭合、嘴角平直、不微笑、不张嘴`。当动作里出现 `笑 / 嘴 / 唇 / 喝 / 咬 / 说` 时，
它会把动作压回去（笑不出、只贴嘴不喝）。**不要改模板**，在该段写段级覆盖：

```json
{"mouth": "嘴部随喝咖啡动作自然变化：端杯时双唇轻含杯沿，吞咽自然，不夸张张嘴；表情放松"}
```

验收：末帧锚定 MAE ≤14 只是初筛，**必须每 0.6~1s 密集抽帧看动作有没有重复/倒退**——
实测 MAE 9.8 的一段同样可能动作演错（过冲后重演）。

## 能被改动的只有头、手臂、躯干——腿不行

这是实测出来最硬的一条。编辑模型（Qwen-Image-Edit）做局部重绘时，**腿和脚基本改不动**。

证据：我明确写「左脚前移踩地、右脚后退抬起脚跟，两腿前后关系与首帧完全相反」，
生成结果的腿**照抄首帧**。同期做像素对比：整图有 43.5% 的像素被重绘（背景、光影都变了），
但腿脚区域的姿态没变——它把算力花在重画背景上，动作没做。

所以分镜的动作必须落在**编辑模型动得了的三个部位**：

| 部位 | 能改 | 实测 |
|---|---|---|
| 头部（抬头/低头/转头 20–60°） | ✅ 很稳 | S2「抬头 25°」一次成功，抬头幅度清晰可信 |
| 手臂（抬手、扶包带、手移到身后） | ✅ 稳 | 卧室案例：双手从前撑→后撑，成功 |
| 躯干（前倾→立直、后仰 20–35°、转身 45°） | ✅ 稳 | 卧室案例：上身后仰 35°，成功 |
| 腿 / 脚 / 膝盖 / 重心 | ❌ 不动 | S1 写「换重心」，首尾帧腿姿完全一致，视频必然静止 |
| 整体位移（从 A 点走到 B 点） | ❌ 不动 | 写了「平移到画面左侧三分之一」，生成后仍原地 |

**腿只能当伴随描述**（"双腿保持盘立姿势"、"裙摆随动作摆动"），不能当主动作。
`check` 现在会对把「换重心 / 换腿 / 换步 / 屈膝 / 抬脚 / 踏上」写进 `open_action`/`end_action` 的段落直接报警。

如果一段的首尾帧"看起来一样"，不要怀疑参数：**去量腿**。首尾帧一样，这一段就不该存在，
它出来必然是静止或乱晃。

## 变化幅度必须跟着时长走（第一原理）

H3 做的事情只有一件：**从首帧 A 插值到尾帧 B**。所以 A 和 B 的差距，决定这段视频有没有内容。

- 差距不够 → 中间没东西可插，H3 只能放慢、晃、或者**走出去再走回来**；
- 差距刚好 → 动作流畅，5 秒正好做完；
- 差距过大 → 插值崩，肢体变形、推镜、变脸。

所以**时长不是独立参数，它是"变化幅度"的函数**。定分镜的顺序必须是：
**先估幅度 → 再由幅度定秒数**，不是先写 8 秒再想办法填。

### 变化幅度对照表（这是这张表的全部意义）

| 段长 | 需要的幅度 | 具体基准（对着人的身体量） | 风险 |
|---:|---|---|---|
| **5 秒（推荐）** | 中等 | 手臂从垂在身侧抬到身前（位移 ≥ 30 cm）**或** 上身后仰 25–35° **或** 转头 45–60° | 最稳，一个部位的中等变化就够 |
| 6 秒（上限） | 中偏大 | 上身后仰 35–45° **且** 双手大位移，**或** 转身 60–90° | 需要两个部位同时变，崩坏率开始上升 |
| 7–8 秒 | 大 | 3–4 个连续动作单元叠加，或一个 90° 级的大转身 | **不推荐**，实测废片率最高 |
| > 8 秒 | — | — | 不要做，单段内做不完 |

**反过来同样重要：既然 5 秒配中等幅度最稳，60 秒就切成 12 段 × 5 秒，
而不是 8 段 × 7.5 秒去硬撑幅度。** 段数多一点不会让成本上升多少，
但每段的幅度可控，通过率完全不同。

### 为什么"拉长时间 + 加大幅度"不是解法

听起来"要 8 秒就配个更大动作"能成立，实际不行，三条硬限制：

1. **幅度有上限**：上身后仰 >40°、转头 >90° 就开始变形，再大要做背面（模型不会凭空画后脑勺）；
2. **能用的部位只有三个**：腿脚改不动，所以"大动作"只能靠躯干和手臂，而这两处的物理幅度很有限；
3. **动作量超过 6 秒必然失守**：中间帧的拐点、衣物穿插、手部会出畸形。

所以 8 秒不是"需要更大动作"，而是**这一段本来只有 3 秒的信息量，多出来的 5 秒 H3 只能拿来犯错**。
正确做法是切段，不是加戏。

### 怎么快速判断"幅度够不够"

把首尾帧并排，**遮住背景，只盯着人的肩膀和手**：

- 一眼就看出肩膀角度变了、手的位置变了 → 够；
- 要来回切换才看得出差别 → 不够，这段会静止或乱晃；
- 只有背景和光影变了、人是复制的 → **完全不够**，这段必废。

量化参考：上半身区域（画面中间 1/3）的明显变化像素应 ≥ 30%。
但整图重绘会把这个数字抬到 40%+，所以**别只看全图数字，要看热力图落在人还是背景上**。
S1 第一版就是典型案例：全图差异 43.5%（看着很"有变化"），实际腿姿一模一样。

### 幅度上限（超过就崩）

| 幅度 | 结果 |
|---|---|
| 上身后仰 ≤ 40° | 稳（卧室案例 35° 成功） |
| 转头 / 转身 ≤ 90° | 稳（实测 90° 侧脸清晰、没变形） |
| 手臂抬到脸附近 | **危险**：会做成"手捂脸 + 低头"，人脸相似度掉到 0.055，整段废 |
| > 90° 或转成背面 | 失败，需要凭空生成后脑勺 |
| 站→躺、跨房间位移、换场景 | 必然崩，必须拆段 |

### 景别越远，动作要越大（2026-11-05 全身实测）

关键点位移是在**归一化画面坐标**里算的，所以同一个动作，景别越远位移越小：| 景别 | 段1「转头 + 掖头发 + 重心微移」的关键点位移 | 质检 | H3 实际表现 |
|---|---|---|---|
| 中景（头到大腿） | 0.13 左右 | 过 | 推镜 3.0× |
| **全身（头顶到鞋）** | **0.08** | **FAIL（阈值 0.12）** | **推镜 4.0×，face_zoom 0.0132→0.0532** |

**机制**：H3 只有首尾两帧，中间靠插值。两帧差异不够大时，它不会"少演"，
而是**自己编动感**——最省力的编法就是推镜。所以「动作太小」的直接后果是推镜，不是静止。

**规则**：
- 全身景别的段，`end_action` 必须含**一个可见的大幅动作**：抬手扶橱窗/扶包带、
  身体明显侧倾、抬手指向、膝盖微屈换重心（腿改不动，别指望）；
- 只写"转头 / 视线移动 / 重心微移"在全身景别下不够，必被判 FAIL；
- 判断口径：`frames` 打印的关键点位移要 ≥ 0.12，低于这个数就加大动作，
  别指望 H3 能演。
- **位移靠手臂，不要靠加大转头**：段落里同时有头转和手抬时，把转头的 Δyaw 控制在
  0.30 左右（≈30°），差额用"另一只手也动起来"补（叉腰 / 扶腰 / 扶玻璃），
  比把转头加到 50° 安全得多（50° 会演成转身，而且 H3 转头超 0.35 就重出 3 次）。

**全身景别必须加比例锁（2026-11-05）**

竖构图 + 全身，2.1 会把头画大、腿画短（实测头身比约 1:5.5，看着矮胖）。`frame` 里加：

```
人物从头顶到鞋底完整入画（全身站姿），真实成年女性人体比例、头身比约 1:7，
腰线位置自然、腿长与上半身协调，不做任何纵向拉伸或压缩变形；
头顶上方留少量空间，脚下的石板路清晰可见
```

实测同一段加与不加：头身比 1:5.5 → 1:7，腿长明显回来，`face_ratio` 不变（不影响 identity）。

### 动作量（秒）作为幅度的代理指标

幅度没法直接写进 JSON，所以用一个可计算的代理：**动作的自然时长**。

| 动作 | 自然时长 |
|---|---:|
| 走 1 步（换一次脚） | 0.5 s |
| 回头 / 转头 20° | 0.4 s |
| 转身 45° | 0.8 s |
| 上身后仰 30° | 1.0 s |
| 重心换腿 | 0.6 s |
| 抬手臂（不到脸） | 0.8 s |

**填充率 = 动作自然时长 ÷ 段时长，落在 80%–110% 最稳**（5 秒段配 4–5.5 秒动作量）。
缺口 > 2 秒且占比 > 40% 就会出问题。

真人速度只有 0.4–1.0 秒，所以**分镜里要写"缓慢"**——把 3 秒的动作摊到 5 秒里慢慢做完，
比 1 秒做完再空 4 秒安全得多。估算器对"缓慢/慢慢/缓缓/逐渐"给 ×1.3 系数，就是承认这一点。

反例：8 秒的段落只写"换一次步"，动作量 ≈ 1 秒，填充率 13%。
整段变成"往前走两步、再退回来"，往返比（总行程 ÷ 净位移）= **2.16**（正常应接近 1.0）。
同一组首尾帧压到 5 秒后往返比降到 1.96，但净位移只剩 26px——
**压时长能改善，救不了错的动作类型。**

## check 会自动算时长

`check` 现在会估每段动作量，并直接给建议：

```
段     设定    动作量  建议   动作节拍
1     5s   4.5s  5s     换重心 + 裙摆被风带动
2     5s   3.5s  5s     抬头 + 视线转向
12    5s   6.5s  拆两段   转头 + 停住 + 头发被风吹
```

估算来自 `ACT_SEC` 关键词表（`pipeline.py`），规则：

- 每个动作单元按其自然时长累加（换重心 1.5s、转头 2.0s、转身 3.0s、迈一步 1.2s…）；
- 文本里出现"缓慢 / 慢慢 / 缓缓 / 逐渐"→ 总时长 ×1.3（放慢本身是合法的填充方式）；
- 段里可以显式写 `"beats": ["换重心", "头发被风吹"]`，优先于自动识别，用于精确控制。

**结论：60 秒片不要去凑"每段 7.5 秒"，那是给自己找麻烦。** 就是 12 段 × 5 秒，
每段两个轻微动作——这也正好是时尚街拍的正常剪辑节奏。

**这条最容易踩。** 实测：8 秒"由远及近走来"这一段，第 4 秒画面被推成脸部大特写，
脸换成了另一个人还带着笑，`flicker` 跳到 0.439。原因不是参数，是设计——
尾帧里人变大，H3 就理解为"镜头在推进"，然后它不会停。

**所以"走来"要写成"横向走过"**：人物大小不变，只做画面内的左右平移。
`pipeline.py check` 会扫 `走近 / 迎面 / 远处走来` 这类词并拦住这类分镜。

单段内**不能做**：站到躺、跨房间位移、换场景、脱外套、完整转身 180°。
这些要拆成两段。

**还不要写"手抬到脸附近"的动作**：拨头发、摸脸、托腮、遮眼、整理刘海。
实测 H3 会把它做成"手捂住脸 + 低下头"，脸被手和头发挡住，
相邻帧人脸相似度掉到 0.055，整段必须重做。想表现"整理头发"就改成
头部微转 + 重心换腿这种纯躯干动作。

字段说明见 `assets/storyboard.example.json`。

## 命令

```bash
PY="D:/Comfy-Desktop/ComfyUI-Installs/ComfyUI/ComfyUI/.venv/Scripts/python.exe"
SK="D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts/pipeline.py"

$PY $SK check    --storyboard s.json        # 分镜校验 + 自动产出 .prompts.md / .prompts.html
$PY $SK prompt   --storyboard s.json        # 只渲染提示词（打印全文 + 刷新两个预览文件）
$PY $SK prompt   --storyboard s.json --seg 2   # 只看第 2 段，适合贴进对话
$PY $SK frames   --storyboard s.json        # 生成全部首尾帧 + 质检
$PY $SK frames   --storyboard s.json --seg 2   # 只做第 2 段
$PY $SK anchor_probe --storyboard s.json     # 锚图冒烟验收：3 个种子全绿才收（见「锚图卫生」）
$PY $SK adopt    --storyboard s.json        # 收编 <分镜名>.frames/ 里用户自己的首尾帧
$PY $SK adopt    --storyboard s.json --seg 2 --no-normalize
$PY $SK director --storyboard s.json        # 转成 ComfyUI Director 节点吃的 <分镜名>.director.json
$PY $SK push     --storyboard s.json        # 建 Director 任务并灌入我们的首尾帧 + 提示词
$PY $SK resume   --job 20261101_132345_1fc69e81   # 让 Director 接着出视频并合并
$PY $SK video    --storyboard s.json        # 逐段出视频 + 质检 + 自动重试
$PY $SK video    --storyboard s.json --seg 2
$PY $SK merge    --storyboard s.json        # 合并 + 整片抽检 → Shared/output/h3-long-video/<标题>.mp4
$PY $SK merge    --storyboard s.json --out D:/x/a.mp4   # 自己指定成片路径
$PY $SK merge    --storyboard s.json --join-dissolve 0.25  # 接缝短叠化（2.1 后端推荐，见「微调叠化接缝」）

# 帧级曝光归一（见「曝光」一节），只调亮度不动锐度
$PY $SK2 --anchor ds_s01_first.png --dry ds_s02_first.png ...   # 预演
$PY $SK2 --anchor ds_s01_first.png ds_s02_first.png ds_s02_last.png
```

其中 `SK2=.../scripts/align_exposure.py`。

**长视频默认走另一套驱动**（见「分段接续链路」，不走上面这些 Director 命令）：

```bash
RUN="D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/scripts/run_selflift.py"
$PY $RUN plate --cfg <片>/segments.json     # 造无人空镜板
$PY $RUN graph  --cfg <片>/segments.json     # 校验 API 图
$PY $RUN all    --cfg <片>/segments.json     # 逐段直出 + 尾帧接首帧
$PY $RUN merge  --cfg <片>/segments.json     # 0.25s 短叠化接续 + 拼接 + 静音版（默认）
$PY $RUN merge  --cfg <片>/segments.json --join black    # 可选：切回 0.8s 纯黑硬切
```

分镜模板 `assets/segments.selflift.example.json`；`--force` 全片重跑。

状态存在 `storyboard.json` 同目录的 `<name>.state.json`，中断可续。

## 关键帧链路：Qwen-Image-2.1 双图（2026-11-02 定版）

这一节是整条流水线里返工最多的地方，全部结论都是实测撞出来的，改之前先读完。

### 出图尺寸由「图1 的宽高比」决定

2.1 的出图尺寸 = **图1（人物参考图）的宽高比 × 约 1.05MP**，再对齐到 32 的倍数。
不是"你传多少出多少"，也不是"你要求 9:16 就出 9:16"。

| 图1 | 出图 |
|---|---|
| 500×625（4:5） | 928×1152 |
| 864×1536（9:16） | 768×1376 |

**推论（踩过）**：不要靠给参考图垫白边去"凑"目标比例——垫出来的白边会被模型当成画面内容，
出图变糊、变脏。要么换一张原生比例的参考图，要么出图之后再补画幅。

### 铁律：参考图先做成 9:16，然后全程都用这一张（2026-11-05 用户重申，已改默认）

**用户原话**：「把参考图先做成指定的比例，然后就一直用它。」这是对的，现在已经是默认行为。

推理链只有一句话：**出图比例跟 image_1 走** →
喂 422×367 的近方形参考图，出图就是 1088×960 横图 →
每张帧都要再跑一次 `outpaint_916` 才能用 → **每段白花 2 次出图（约 1 分钟）**，
10 段就是 10 分钟，还多两次"扩图撞车 / 扩出桌面"的机会。

改成喂 768×1376 的 9:16 参考图之后：

| 参考图 | 出图 | 每段要扩图吗 |
|---|---|---|
| 422×367（头肩裁切，近方形） | 1088×960 横图 | 要，2 次 |
| **768×1376（pad 成 9:16）** | **768×1376** | **不要，0 次** |

代码在 `ref_name()`：自动 `to_916()` 出一张 `<名字>_916.png` 并缓存（mtime 判断，只做一次），
之后首帧、尾帧、视频身份锚全都用它。分镜写 `ref_pad_916: false` 可关。

实测（2026-11-05，shopping 段1）：出图 768×1376，场景仍是步行街橱窗前、下方石板路、
无家具、服装正确、脸清晰。**和下面的 A 方案不是一回事**——这里只 pad 画布（墙色渐变），
人物像素不重绘。

> 补白区域会不会被画成室内？实测没有。pad 只提供画布比例，内容由 `shot`/`frame` 决定。
> 但 pad 会让模型把人物画成**全身**（因为画布下方空间大，模型把腿画完了）——
> 景别会从中景变全身，这是可见副作用，要提前跟用户确认。

### 两段式：先生成，再扩图（A/B 实测，2026-11-02）

| 方案 | 做法 | 结果 |
|---|---|---|
| A | 先把参考图**用扩图 LoRA 重绘**成 9:16，整条链都用它当图1 | 清晰度 335 → 133，桌子消失，白背心出现彩虹马赛克伪影。**否决（重绘糊）** |
| B | 图1 用原图（4:5），出图后 `outpaint_916()` 用扩图 LoRA 补成 9:16 | 真实桌面，无灰边无伪影；每帧多约 35 秒 |

> **2026-11-05 修正**：A 被否是因为"扩图 LoRA 重绘"把画质砍半，不是"定比例"这个思路错。
> 现在用 **pad 画布（不重绘）** 已达到同样的"全程只用一个比例"效果，且不掉清晰度，
> 所以默认改成了 pad 式（见上一节）。上面这条 B 只在 `ref_pad_916: false` 时才会走。

代码：`normalize_916()` / `center_crop_916()` 只做"末段补画幅"，生成阶段不补白；
`outpaint_916(path, seed)` 负责 4:5 → 灰底画布（上下按 22%/78% 留白）→ 扩图。

补白画布 `_wall_grad()` 用的是**背景墙色的平滑渐变**（top = 墙面中位数，bottom = ×0.955）。
旧版"末行拉伸 + 高斯模糊"会被模型当成纹理先验，直接生成一片毛糙，这是"锐化过度"的根因之一。

### 扩图配方（照 `▶▷Qwen-image21-功能流` 的扩图部分）

实际跑的是 `Qwen21-Outpaint916.json`：

- 模型：**官方 `qwen_image_2.1_int8_convrot`**（不是 ray）
- LoRA：`Qwen-功能2.1/Qwen21-图像扩展qwen-image-2.1-outpaint-v2`
- 采样：20 步，cfg 1.0，euler / simple
- 画布：纯灰 `(127,127,127)` 768×1376，原图居中贴入，**上留 22% / 下留 78%**
- 触发词（`OUTPAINT_PROMPT`）：
  `Outpaint the image: replace the solid gray areas with a seamless continuation of the scene, keeping the existing picture unchanged.`

扩图失败会自动退回 `normalize_916()`（铺墙色补画幅），不阻塞流程。

`Qwen21-RefOutpaint.json` 是另一种写法：`p_qwen_image_2.1_8step_v0.1` + outpaint LoRA 串联、8 步，
画布 864×1536。速度更快，但接口和主链路不同，属于备选。

**分工要记住**：关键帧用 **ray 模型**（官方模型人物角度会错），只有扩图用官方模型。

### 双图分工

- 图1（`images.image_1`）= 人物参考 `ds_face.png`（原图，4:5）
- 图2（场景锚）= 扩好的 `ds_face_916.png`，**全程恒定，不链式**

链式（拿上一段输出当图2）会代际累积花斑；恒定锚则背景锁死、无退化。
分镜里写 `scene_ref` 就用它当全程图2。

### 锚图卫生（铁律：所有关键帧任务都适用，2026-11-04 定）

**图2 的每一处像素瑕疵，都会被 2.1 复刻进之后每一帧，而且放大。**

2.1 编辑模式会把图2 当"底版"逐像素重绘。实测（咖啡分镜）：坏帧的斑点分布与
**锚图自身的高频纹理空间相关 0.950**，与人脸参考图只有 0.051 —— 斑点不是随机噪声，
是锚的纹理被搬过去的。同一段分镜，脏锚触发"复刻模式"的概率约 **8/15**；
换成干净锚后，六帧**零重试**全部干净。

**锚图验收清单（三条都要过）**

1. **无畸形解剖**：手指数目、脚/腿部结构、四肢是否被桌沿裁断。AI 生成的锚最容易
   在画面下缘凭空长出脚，这些脚会被复刻得比原图更恐怖；
2. **无凭空道具/特效**：杯口热气、烟雾、漂浮颗粒、多余阴影 —— 模型会把它们当纹理先验；
3. **无斑驳纹理底噪**：整图放大看没有颗粒状脏斑（`hf_ratio < 15%`）。

**冒烟验收（静态指标测不出来，只能实跑）**

```bash
# 拿候选锚当图2 连跑 3 个种子（SceneRef 编辑模式，最易复刻的那条路径）
$PY pipeline.py anchor_probe --storyboard <sb.json>                    # 测当前锚
$PY pipeline.py anchor_probe --storyboard <sb.json> --anchor cand.png   # 测候选锚
```

判据（**不是"全绿才收"**）：每个种子量 脸部 Lap<380 且 全图高频<15% 记 PASS，
然后看坏的比例——

| 坏的种子 | 结论 | 动作 |
|---|---|---|
| 0/3 | 锚优 | 直接用 |
| 1/3 | 锚可用 | **必须把 PASS 的种子写进 `keyframe_seed`**，并保留生成时闸门 |
| ≥2/3 | 锚该换 | 重出锚，别试下面的补救手段 |

**为什么不能要求全绿**：触发是「锚 × 种子」的**交互**，不存在绝对免疫的锚。
实测同一对锚：旧锚 100000 坏 / 100001 好；新锚 100000 好 / 100001 坏——
同一个种子换锚会**翻面**。所以：

> 换干净的锚，实质是"把默认种子那条路修通"（我们默认 `keyframe_seed=100000`，
> 新锚在这条路上零重试），而不是让模型从此免疫。
> **真正的兜底永远是两道帧闸门 + 换种子重出**，锚只是把猜中概率从 8/15 降到 1/3 左右。

旧锚 vs 新锚实测：旧锚触发率 8/15（同一段分镜连跑），换锚后同种子连出六帧零重试。

**生成锚时自动带约束**：pipeline 在生成"即将固化成锚"的那一帧（段1首帧、
未指定 `scene_ref`）时，会自动追加 `ANCHOR_GUARD`（`画面边缘完整收尾，四肢完整不被裁断，
画面中不出现多余的脚、腿或手；道具表面干净，没有热气、蒸汽、烟雾或漂浮颗粒`）。
医疗/道具类分镜可在分镜里写 `anchor_guard` 覆盖，`false` 关闭（不推荐）。

**锚脏了怎么修**（按性价比排序）

| 做法 | 效果 | 代价 |
|---|---|---|
| ✅ 重出锚，提示词加"桌下不可见/无蒸汽"，挑一张干净的 | 根治，下游全救 | 1 次生成 |
| 已出的锚用 inpaint 抹掉畸形部位 | **无效**（实测抹掉脚 493→669 仍然坏） | 白干 |
| 锚图软化（0.5× 往返）再喂 | 只救脸，四肢仍斑驳、畸形仍被复刻 | 画质有损 |
| 靠种子重试闸门兜 | 能拦住，但触发率 8/15，浪费算力 | 3 倍生成成本 |

### 铁律：场景锚必须「去纹理化」再喂图2（2026-11-05 定，量化锚）

上面那套「锚图卫生」只解决了"锚有多脏"，**没解决"锚太像照片"**。实测一个够干净的锚
（hf 13.3%，肉眼无斑驳、无畸形道具）照样能把帧冲到 **58% 高频**，换种子重试 4 次都压不下去。

**八组对照（段7 首帧，同 seed / 同步数 / 同提示词，只改一个变量）**

| 方案 | 出图 hf | 结论 |
|---|---|---|
| 原锚 hf 13.3% | 58.1% | 基线 |
| image_1 换成真 9:16 全身图 | 61.4% | 换人物参考**无效** |
| base20 二十步（去掉加速 LoRA） | 50.2% | 加步数**无效** |
| 提示词写"平整地面 / 背景柔和虚化" | 53.7% | 改文案**无效** |
| 去掉 frame 里"脚下石板路清晰可见" | 57.2% | **无效** |
| 锚做保边平滑（hf 5.6% / 3.8%） | 54.2% / 51.8% | **无效** |
| 只处理锚的地面区（hf 7.7%） | 32.9% / 35.9% | 部分有效，不够 |
| 锚整图虚化（hf 1.5%） | 23.3% | 段7有效，**段9 反被拖脏** 13.5%→23.7% |
| 干脆不给 image_2（单图） | **11.0%** | 最干净，但每段背景各自发挥，拼接会跳 |
| **MeanShift + 12 色量化锚** | **16.0%** | ✅ 干净且保住布局 |
| 线稿锚 | 17.3% | 略差，会出镜像感 |

**机制**：2.1 的 `image_2` 不是"参考它的布局"，而是"照着这张照片重渲染"。锚里任何高频纹理
都会被逐像素复刻，并按景别放大（全身景约 4 倍）。所以问题不是"锚脏"，而是"锚太像照片"——
**只有把锚剥成没有纹理可复刻的大色块才管用**。

**pipeline 已内置**（`quant_anchor()`）：`MeanShift(24,32)` + `kmeans 12 色`，缓存成
`<锚名>_q.png`，kmeans 初始化固定（`setRNGSeed(0)`）保证同一张锚每次结果一致。

- 首帧的图2（全片场景锚）用 **12 色**：去纹理优先；
- 尾帧的图2（本段首帧）用 **24 色**（`_q24.png`）：要保住袖长/蕾丝边/配饰，去纹理弱一档。

全片实跑（shopping 10 段）：段5/7/9 = 17.4% / 16.0% / 16.9%，最差段从 58% 收敛到 17%，
脸 Lap 从 152 降到 75。干净段会略升 3~5 点，换最差段降 42 点，净收益极大。

> 判据仍然看**生成帧**的 `hf_ratio`，不是锚。锚量化后 hf 大约 4~7%，但关键看输出落在 20% 以内。
> 量化锚不能救"畸形/多脚/鼻涕状道具"——那是布局问题，还得重出锚。



**人物查完还要查静物。** 视频模型把首尾帧当唯一真值：首帧杯柄朝左、尾帧朝右，它只能
**自己编一个旋转过程**（杯子瞬移、把手中途消失再出现、悬空转 90°）或接缝跳变。

- coffee 复查实锤：S1_FIRST 与 S3_LAST 杯柄都朝画面右，但**杯身旋转角不同**
  （S3_LAST 杯口和液面朝镜头露出、杯子更大更近）。这次靠视频提示词"杯柄归位"圆过去了——
  **是运气不是设计**。
- 同类风险清单：椅子靠背朝向、门的开合方向、笔的本体朝向、屏幕内容、窗帘开合、壶嘴朝向。

**关键帧提示词：静物写"朝向 + 相对位置 + 状态"三件套，同一段戏所有关键帧复制同一句**

```text
❌ 桌上放一只白瓷咖啡杯
✅ 白瓷咖啡杯放在桌面右侧（笔记本右手边），杯柄朝向画面右方，
   杯口侧对镜头、看不见杯内液面，杯底与桌沿平行
```

**视频段兜底**：`杯柄始终朝向画面右侧，杯子只被拿起/放回，不在桌面旋转、不改变朝向和位置`。

**QA 清单加一条**：人物三项过完后，把画面里每件静物列出来逐项核对 朝向/位置/比例/状态/遮挡。
**发现首尾帧静物矛盾不要试图用视频提示词纠正**（两帧矛盾时模型只能编中间态），
回去重出关键帧——和"锚图脏了就重出"同一条原则。

**记录约定（长期有效）**：以后跑片发现新的细节问题（画面瑕疵类），当天按
`现象 → 帧证据 → 根因 → 规避写法` 四行同时记入手册（§7.6 坑清单或通用规律小节）和本 skill
对应章节；坑只增不删，修复验证过的标"已规避"。

### 尾帧提示词：「变」写在最前，「不变」必须限定范围（2026-11-05）

**症状**：10 段里 9 段质检 FAIL，`关键点位移 0.02`（阈值 0.12），`end_action` 明明写着
"右手抬到肩膀高度贴橱窗"，出图手臂一动没动。用户看到的就是"每一段都是站着不动"。

**根因**：`Q21_LAST_TMPL` 旧版为了锁住服装/身份/场景，整段 90% 的篇幅在写
「与图2 完全一致 / 保持原状 / 不许改动 / 不新增也不删除」，只有一句 `{end_action}` 在写"要变"。
最后还有一句「人物在画面中的大小**和位置**与图2 保持一致」——模型直接理解成**姿势也别动**。
模型对"不许改"的服从度远高于"要改"。

**修法（已改进模板）**：

1. **动作句提到第一句**，并用"这一张画的是动作做完之后的样子"+「**姿势必须与图2 明显不同**」
   开头，明确点出手臂位置/手的落点/头部朝向/上身倾角四项必须变；
2. 「不变」收缩成三样：**身份 / 服装配饰 / 场景**，其余一律让位给动作；
3. 把「大小**和位置**保持一致」改成「**尺度**一致」，并紧跟一句
   `这个尺度一致只约束人物大小，不约束姿势`——不写这句，模型会连姿势一起锁；
4. 头发锁保留，但只写发长/分缝/不飘散，别把"垂落位置不变"也当硬锁（手抬起来时头发本来会动）。

实测（段1，同 seed 同文案）：位移 **0.02 → 0.14**，`主要变化：右腕 0.88`，质检从 FAIL 转 PASS。

> 同理适用于任何"编辑模式"提示词：**先说要变成什么，再说哪些不能变，且不能变的范围必须点名到具体项**。
> 「保持一致」这种无范围的措辞会被模型当成"整张图都别动"。

### 代际累积：把上一张输出喂回 image_2 会越跑越锐（2026-11-05 定）

**实测曲线**（同一 seed / 提示词 / image_1，每次把上一次输出直接当 image_2）：

| 代 | image_2 | 全图 hf | 脸 Lap |
|---|---|---|---|
| 1 | 12 色量化锚 | 18.9% | 62 |
| 2 | 第 1 代输出 | **47.3%** | 134 |
| 3 | 第 2 代输出 | **65.6%** | 192 |
| 4 | 第 3 代输出 | **69.9%** | 185 |

对照组：**同样参数、不换图，连跑 4 次 → 逐像素 MAE = 0**（后 3 次 5s 是节点缓存复用）。
所以不是"跑的次数"在累积，是**"把生成结果喂回 image_2"**这一件事——每一代都在上一代
被放大的纹理上再复刻一次，四代就饱和到用户报的"锐化过度"那种图。

> 硬规则：**任何要当 image_2 的图，必须先剥成色块；永远不要把原始输出直接喂回去。**
> 连续编辑要么退回单图模式，要么走下面这个内置去纹理工作流。

### 工作流内置去纹理（`Qwen21-SceneRef916-V6-Q.json`）

上面的规则靠人记不牢，所以做成工作流里的硬连线，`LoadImage(11)` 之后强制串两级：

```
LoadImage(11) → Image Median Blur (size=32 → 实际 ksize 31) → ImageQuantize(12 色, dither=none) → TextEncodeQwenImage21.images.image_2
```

- `Image Median Blur` 来自 RES4LYF，**节点内部有 `size -= 1`**，所以要传**偶数**让 ksize 落奇数；
  传 16 → ksize 15 不够（22.0%），传 32 → ksize 31 才够。
- `ImageQuantize` 是 ComfyUI 内置（`comfy_extras.nodes_post_processing`），
  **单独用无效**（12 色 45.1%、4 色 37.8%）——它只做颜色聚类，砖缝轮廓还在。
  必须先有中值滤波把连续边缘并掉，再做颜色量化。
- 中值 31 + 12 色，链式喂回 5 代：**15.1 → 16.7 → 17.7 → 17.6 → 16.4%**，收敛不再累积，
  人物/服装/橱窗/地面全部清晰正常。

⚠ **2026-11-05 复核补充：hf 收敛 ≠ 画面不退化**（换 `qt_ref.jpg` 新参考图、
从**未经量化的原始照片锚** `shopping_anchor.png` 起手，同 seed 100000）：

| 代 | V6-Q 全图 hf | 脸 Lap | 地面肉眼 |
|---|---|---|---|
| 1 | 13.7% | 42 | 正常灰石板 |
| 3 | 16.1% | 53 | **黑白迷彩／大理石花纹** |
| 5 | 16.0% | 53 | 迷彩加重、铺满整个地面 |

- **现象**：地面从第 2 代起长成高对比黑白斑块并逐代放大；同时 `image_2` 把上衣
  "黑色长袖皮夹克"演成"黑色无袖马甲 + 白色泡泡袖"（第 1 代即如此，属提示词/参考图
  理解问题，不是代际退化）。
- **帧证据**：`output/shopping/qtq_{1..5}.png`、`qt_chain_sheet.png`；地面黑白极化率
  （灰 <60 或 >200）4.7% → 21.5% → 57.3%（降对比版 `qtc_*`）。同图同 seed 原版 V6 对照：
  50.9% → 71.9%、脸 Lap 140 → 230，所以「高频颗粒锐化」确实被挡住了。
- **根因**：去纹理只挡住「高频颗粒复刻」，挡不住「语义级图案强化」——模型把锚里的
  地面光影当成"花纹地砖"，每代照着再画一遍，对比逐代拉满；**大色块边缘少，hf 反而稳定**，
  指标会骗人。
- **规避**：① 永远不要把生成结果喂回 `image_2`（正式 pipeline 用恒定量化锚，不受影响）；
  ② 连续编辑退回单图模式，或每代都复用同一个固定外部场景锚；
  ③ 验收必须开图看地面，不能只看 hf / 脸 Lap。
- **已排除**：量化后再串 `AdjustContrast(0.45)` 压对比完全无效（极化率照样
  4.7 → 21.5 → 57.3%），该实验工作流已删。

顺带修了 `pipeline.py::obj_info()` 的 URL 编码：节点类型名带空格
（如 `Image Median Blur`）原来会让 `object_info/<name>` 抛 `InvalidURL`。

> 这个版本是给**在 ComfyUI 界面里手动跑**用的：拖任何图进图2 都会被自动剥成色块。
> pipeline 自动链路仍走 `quant_anchor()`（Python 层），两者效果等同，别叠加使用。

### image_2 的正解：别给它照片（2026-11-05 源码 + 六组对照）

**源码事实**（`comfy_extras/nodes_qwen.py:144-182`，`TextEncodeQwenImage21`）：
`vae` 是**可选输入**，tooltip 写明「Reference images, seen by the text encoder and
**spliced into the sequence as VAE latents**」。

- 接 VAE → 每张参考图 `vae.encode` 成 latent 拼进 conditioning，`keep_vision=False`
  （视觉塔不看图）→ **纯重渲染**，这就是纹理/图案被逐像素复刻的根源。
- 不接 VAE → `ref_latents` 为空、`keep_vision=True`，图片只被视觉塔"看一眼" → 语义参考。

六组对照（同 seed 100000 / 同提示词 / 同 image_1 `qt_ref.jpg`，只改 image_2 与连线）：

| 方案 | 全图 hf | 地面 hf | 脸 Lap | 肉眼 |
|---|---|---|---|---|
| V6 原版 + 照片锚 | 50.9% | 79.1% | 140 | 碎石锐化 |
| **断 VAE + 照片锚** | 13.5% | 3.0% | 31 | ❌ **身份/场景全崩**：卧室黄地板、床、照片墙串进来，衣服变蓝吊带+牛仔短裤 |
| `resolution=0` + 图2 缩到 256×448 | 30.8% | 47.2% | 59 | ❌ 碎石，无效 |
| 12 色量化锚（旧方案） | 16.0% | — | — | 干净，但代际链式会迷彩 |
| **纯色布局图 + 保持 VAE** ✅ | **14.7%** | **13.7%** | 46 | 商业街橱窗、平整灰石板、**服装全对** |
| 断 VAE + 布局图 | 8.6% | 3.4% | 37 | 最干净，但同样串卧室背景 |

**纯色布局图做法**（`qt_layout.png`，768×1376，**不来自任何照片**）：
米灰墙面铺满 → 下 38% 换成一整块中灰地面 → 左上画深棕橱窗矩形 → 橱窗内画暖黄
灯箱矩形 → 右下画一块深灰石台。无纹理、无光影、无随机斑块。

**为什么它比"用别的模型处理照片"更好**：任何从照片派生的表示（量化/模糊/线稿/低通）
都会残留光影形成的大色块，模型把那些色块当成"花纹地砖"逐代强化；程序画的布局图
只有明确语义的大色块，模型无从复刻图案，只能按提示词重新画干净地面。
意外收益：服装不再被带歪（此前「黑色长袖皮夹克」被演成「无袖马甲+白泡泡袖」），
说明 image_2 的 latent 也影响人物结构，不只是背景。

**边界（重要）**：布局图能锁「场景大类」，**细节一致性取决于布局图的信息量**。
只有几块大色块时，换 seed / 换段后橱窗位置数量、墙面分割、砖缝走向仍各画各的
（背景带 MAE 39.33）；把关键结构都画上（右侧补门框 + 玻璃 + 中缝）后，
同布局换 seed 的 MAE 降到 **23.49**，门稳定复现。
所以不是"锁不住细节"，是**"没画的细节模型自己填"**。要像素级 100% 一致，
仍只能跳出 image_2：单图模式出干净人物 → 后期合成到固定背景板。

**布局图是语义开关：补门实验（2026-11-05）**
- **现象**：`nv_flat` 右侧没有分镜要求的玻璃门，只有一整片暖黄橱窗；
  左侧深框 + 橙黄块反而被画成了带门框的玻璃入口。
- **帧证据**：`door_compare.png`（原布局 / 补门布局 / 两张出图）、
  `nv_door.png`、`nv_door_b.png`。
- **根因**：原布局图右侧只给了一小块下部石台、上半部是墙，**没有任何"门"的色块**；
  提示词里写了门也没用——**色块 latent 的权重压过文字**。
- **规避**：在布局图右侧补一块竖长玻璃门矩形（深框 + 浅灰蓝玻璃 + 中缝），
  门立刻出现且左右对称，画质不变（15.4% / 脸 Lap 45），换 seed 仍复现（17.1%）。
  → **要什么结构就画什么矩形，别指望提示词补**。

**复杂背景可行性（2026-11-05 实测）**：14 个矩形的 `qt_layout_complex.png`
（3 组橱窗 + 2 扇门 + 立柱 + 招牌带 + 顶部横条 + 石台）→ `nv_complex.png`：
多扇玻璃橱窗、深灰门框、立柱、招牌带，正常商业街照片，
全图 16.2% / 地面极化 4.2% / 脸 Lap 45，与 4 矩形版（15.4% / 4.1% / 45）几乎一致。
**矩形数量不是瓶颈**——纯色块本身没有可复刻纹理；且「看起来复杂」≠「布局图要复杂」，
4 个矩形时模型已自己长出玻璃门框、橱窗陈列、灯箱、石柱纹路。

**能力边界与路线分级**：

| 场景复杂度 | 方案 |
|---|---|
| 多门多窗、立柱、招牌带 | 参数化布局图（已验证） |
| 拱门 / 树 / 栏杆 / 人群 / 透视纵深 / 文字招牌 | 矩形表达不了，见下 |
| 任意复杂且要像素级一致 | 背景与人物分离 |

高复杂三条路：① **让另一个模型画布局图**——本地 `ComfyUI-Installs/ComfyUI/ComfyUI/models/`
下 `diffusion_models/` 有 `krea2_turbo_int8_convrot`、MOSS 等，`loras/` 有
`krea2_style_reference`，按文字生成扁平插画再当 image_2；**必须要求扁平色块、
不带照片光影**，否则回到迷彩。② **背景与人物分离**：单图模式出干净人物 →
合成到任意复杂的真实背景板；本地 `controlnet` / `background_removal` / `clip_vision`
目录均为空，需先装抠图模型，还要处理投影与光影匹配。③ **分段多张布局图**：
远景 / 近景 / 店内各一张，段内锁死，段间是设计好的切换。
硬限制：招牌文字、特定物体、精确透视，色块管不了，只能后期贴或走 ②。

**氛围型 vs 叙事型复杂（2026-11-05，用户给的国风战斗插画实测）**
原图 16:9：云层、逆光、飞檐、飘带、空中打斗者、龙、满屏碎片。

- 只抽象「大区域明暗」成 **6 块布局图**（亮天空 + 白色圆光斑 + 左右斜边飞檐多边形
  + 中景带 + 暗地面）→ `epic_layout.png`：云层、逆光、两侧飞檐、旌旗、远处人群、
  深绿飘带、长棍全出来了，全图 hf **6.3%**、脸 Lap 26，很干净。
  **复杂度在纹理/光影时，布局图反而最省力**——那部分模型免费生成。
- 原图裁 9:16 **直接当图2** → `epic_raw.png`：构图、姿势、飘带更贴近原作，hf 12.6%，也没脏。
  **修正 §7.16 的边界**：照片锚会炸是因为「有可被放大的高频地面砖缝」；
  云、柔光这类低频内容放大不会变噪点，所以不是"任何照片锚都必脏"。
- 三处瑕疵：① 多边形飞檐在天空留下淡淡**几何亮斑**——非矩形色块会留痕，
  剪影尽量压到画面边缘或矩形化；② 出图偏写实电影感，不是原作插画风，
  风格得换模型/风格 LoRA，不是 image_2 能解决；③ 空中打斗者、龙、碎片全丢了，
  **叙事性元素色块表达不了**。
- 分级：**氛围型复杂**（云/雾/光/远景）→ 布局图够用；
  **叙事型复杂**（特定人物/道具/文字/动作）→ 必须背景板 + 人物合成。
- 画幅：原图 16:9，竖构图链路会裁掉两侧飞檐，大场面要先定画幅。

**「image_2 就是背景图」方案（2026-11-05 用户定，已闭环）**

流程（**预处理一次，全片复用**，不是每张出图都做）：
1. 参考图 → 单图编辑工作流 `Qwen21-SingleRef916-V6.json`，提示词「移除所有人物…
   只留背景空镜，被遮挡处用同风格自然补全」→ `epic_bg.png`（约 40s）。
   实测：无人、无人形残影、无抠图白边，hf 8.2%。**比 PS 抠图安全**——生成式补全没有修补痕迹。
2. `quant_anchor(bg, colors=12)` 剥纹理（8.2% → 6.1%，约 1s，自动缓存）→ `epic_bg_q.png`。
3. 全片每段 image_2 恒定用它；提示词写「保持图2场景不变，在画面中央加入主角」。

实测对照（同 seed / 同提示词 / 同 image_1）：

| image_2 | 全图 hf | 结果 |
|---|---|---|
| 原始空镜 | **68.7%** | 云笔触被放大成高对比云絮+金色描边，人物被画小、脸检测不到 |
| **12 色量化空镜** | **15.5%** | 人物清晰、飞檐/旌旗/地面都在，画质干净 |

**三个硬条件**（因为 image_2 是 VAE latent 底稿，不是纯参考）：
① 背景图不能有人，否则复刻出第二个人；② 不能用抠图/透明，白边和涂抹痕会被逐像素复刻；
③ 必须过去纹理，否则任何笔触都会被放大。
**残留**：不规则色块边界会被复刻成**硬边剪纸云**（量化版实测），写实云层需提高色数或换风格，
属风格取舍不是画质 bug。样本 `output/shopping/bg_ref_sheet.png`。

**从零生成背景图（2026-11-05 复核，用户要求）**：不需要任何参考图。
给 `blank916.png`（纯灰 768×1376）同时当 image_1 / image_2，提示词只描述背景
并写明「画面中没有任何人」→ `bg_gen.png`（30s，hf **2.9%**，云/逆光/飞檐/旌旗/石板地全在）。
比"从参考图移除人物"更简洁，少一步、成本更低。

| 背景图来源 | 原样当图2 | 12 色量化后 |
|---|---|---|
| 参考图编辑移除人物 | 68.7% | 15.5% |
| **从零生成** | **63.8%** | **12.8%** |

**量化这道省不掉**：背景图自身 hf 只有 2.9%（非常柔和），原样当 image_2 仍炸到 63.8%。
说明「照着重渲染」会**主动放大层次、拉高对比**，不是简单复制输入高频——
判断依据不是"锚干不干净"，而是"有没有连续可追踪的边缘"。
从零生成版量化后人物更大更清晰（`bggen_q.png`），硬边云与暗调高对比很搭，像电影海报。
样本 `output/shopping/bggen_sheet.png`。

❌ **上面这条「生成空镜 → 量化 → 当 image_2」流程已否决（2026-11-05 当日复核）**。
用户指出 12 色量化是当初为「照片场景锚」加的历史包袱，换成专门生成的柔和空镜后只剩副作用：
量化把云的连续渐变压成硬阶，出图变成硬边剪纸云，肉眼就是**锐化过度**。
`hf` 会骗人（12.8% 看着干净），要看**强边缘均值**：柔和原背景 120，
量化出图 195、原样出图 222，`>200` 占比 0.4% → 3.9% → 25.4%。
**量化色数不是旋钮**：24 / 48 / 96 色 + 边界羽化，强边缘均值全部 190~195、`>200` 4.4~5.1%，
与 12 色无差别，hf 反而升到 15~17.5%。

✅ **最终方案：背景图根本不进模型，直接当最终背景板 + 人物合成**
1. 空白画布 + 纯背景提示词 → 生成空镜 `bg_gen.png`（hf 2.9%，柔和），**全程不喂给任何模型**；
2. 人物单独生成：`image_2 = white916.png`（纯白），提示词要求「背景必须均匀纯白、无任何场景元素」
   → `person_white.png`（实测模型听话，白底干净、人物完整）；
3. 色键抠白（**注意 alpha 方向：越白越透明**）→ 轻微压暗暖调 → 合成到原背景。
   样本 `output/shopping/composite_clean_sheet.png`：背景云层完全柔和无锐化，人物边缘干净。
- **为什么这才成立**：image_2 是 VAE latent 底稿，任何图进去都会被重渲染并主动拉高对比，
  干不干净、量不量化都救不了；让背景走「像素直出」而不是「模型重画」才是根治。
- **残留**：人物是均匀柔光、背景是强逆光，合成后光影略不匹配；脚下缺接触阴影。
  可用边缘轮廓光提示词 + 简易阴影层再调，属后期范畴。
- **已排除**：从复杂背景里用 GrabCut 抠人（`composite_demo.png` 带一圈白云残影），
  正式流程必须让模型直接出纯色底。

### ⚠ 17:45 重大修正：seed 100000 是坏种子，上面多条结论被它污染

**严格对照**（同工作流 / 同 image_1 `qt_person_916.jpg` / 同 image_2 `bg_gen.png` /
**同一份含「保持…不变」的完整提示词**，只改 seed）：

| seed | 全图 hf | 强边缘均值 | >200 |
|---|---|---|---|
| **100000** | **61.8%** | 220 | 24.3% |
| 450807536146328 | **6.6%** | 155 | 1.5% |

用户手跑的 5 张同样印证：seed 100000 的两张 61.8 / 62.7%；三个随机 seed 5.5 / 6.3 / 6.7%，
其中 `direct_one_00004` **写了「保持…完全不变」照样干净**。
所以「提示词不写保持不变就没事」现象成立，但**归因错了：真变量是 seed，不是措辞**。

**方法论错误**：为控制变量，今天所有消融实验固定 seed=100000，恰好撞上最脏的种子，
于是把「这个种子的病」当成「机制的必然」，推导出量化色数扫描、背景板合成、
白底抠图等一整串过度方案。**凡本节中基于单次固定 seed 得出的「必然」结论都需重估。**

**仍然成立**（多组 / 全片 / 肉眼验证过）：照片类锚带连续砖缝会被复刻放大、
12 色量化锚能压住、代际链式喂回会累积、单图模式最干净、布局图可控结构。

✅ **修正后的最简流程**：背景空镜 + 人物参考图 + 普通提示词 → **直接双图出图**，
随机 seed + 现有 hf 闸门兜底即可。不用量化、不用抠图、不用合成。
实测 6.6% / 强边缘 155，人物服装保持、融入自然（`output/shopping/seed_sheet.png`）。

**硬规则**：① 做对照实验**至少两个 seed**，单种子结论无效；
② `seed=100000` 在本配置（int8 ray 权重 + viggle6 六步 + 768×1376）下是已知坏种子，
**禁止用作基准**；③ 正式出片保留「换种子重出」闸门，它本来就能兜住这类种子。

#### 18:00 街景 3 段 6 帧实跑（修正后流程的正式验证）

`street_bg.png`（空白画布从零生成的商业步行街空镜，无人）+ `qt_person_916.jpg`
+ 每段一句动作提示词 → **V6 原版工作流直接双图出图**，6 个不同随机 seed。
没有量化、没有抠图、没有合成、没有加节点。

| | hf | 强边缘均值 | 脸 Lap |
|---|---|---|---|
| 6 张范围 | 24.3~26.3% | 165~172 | 15~26 |

- 6 张全部干净，0 次复刻模式重出；黑皮夹克/白针织/黑短裙/白球鞋、长相、
  左侧玻璃橱窗 / 右侧石材墙 / 石板路全部一致，肉眼无锐化噪点、无色块。
- 动作位移 **0.24 / 0.31 / 0.27**（阈值 0.12），手臂 0.33 / 0.46 / 0.40
  → 挥手、扶橱窗、迈步、撩头发都做出来了，可直接进 H3 视频阶段。
- **背景带 MAE（相对段1首帧）4.5~9.9**，远优于旧「布局图换 seed」的 23~40。
  ⚠ 所以「布局图锁不住背景细节」同样是被坏种子污染的结论：
  **用同一张真实空镜 + 随机 seed，背景本来就锁得很稳**，不必上背景板合成。
- 样本 `output/shopping/st_sheet.png`，逐帧 seed / 指标见 `st_meta.json`。

### 景别 / 机位 / 光影：靠提示词就能改，别在模板里锁死（2026-11-05 实测）

**用户指出的贴片感根因**：上面 6 帧的测试脚本里 `SHOT` 常量写死成
「同一机位同一景别，人物在画面中央，头身比约 1:7」——全片每段都套同一个标准正面全身像，
透视和比例从不变化，人当然像被放进去的。

⚠ **别误删尾帧锁**：`pipeline.py:566`（POSE_FIX）和 `:615`（Q21_LAST_TMPL）里的
「同一机位同一景别／人物大小位置与首帧一致」是**尾帧派生必需的**，删了首尾帧机位会跳、
H3 插值直接崩。要改的是**每段首帧的镜头设计**，不是尾帧模板：首帧定机位，尾帧继承首帧机位。

**同 `street_bg.png` / 同 `qt_person_916.jpg` / 同服装 / 不同随机 seed，只改提示词里的机位段**，
6 张全部成功（`output/shopping/cam_sheet.png`、`cam_meta.json`）：

| 提示词写的 | 出来的画面 | hf |
|---|---|---|
| 远景广角、人物站街深处 | 人物明显变小，橱窗/墙线条向消失点汇聚，地面占比大 | 26.4% |
| 中近景、腰部以上 | 半身，背景浅景深虚化 | 11.1% |
| 低机位仰拍、膝盖高度 | 建筑线条向上收拢、腿拉长、天空露出 | 21.4% |
| 侧面 45° 斜机位 | 橱窗变斜向透视、砖缝对角线延伸 | 17.1% |
| 胸像特写 | 头肩充满画面，背景只剩虚化暖黄 | 10.8% |
| 中近景 + 侧逆光描述 | 发丝轮廓光、正面偏暗、色调转暖 | 13.0% |

即 **image_2 是真实空镜时，提示词能重新调度机位和景别，不需要换背景图**。
（这与「纯色布局图压过文字、门必须画矩形才出现」不矛盾：色块没有可重组的语义，
模型只能照抄；真实空镜有完整场景语义，可以被重新取景。）

**光影严格对照**（同机位同景别同 seed 组，只改光照描述，`fusion_sheet.png`）：
`fusion_plain` 写「柔和漫射天光」→ 均匀平光、脚下无影、明显贴片；
`fusion_light` / `fusion_light2` 写「侧逆光从左后方穿过橱窗＋发丝肩部轮廓光＋正面柔和反射补光＋
脚下接触阴影和向右前方的长投影＋人物明暗方向与街道一致＋冷灰暖黄色调统一」
→ 两张都出现轮廓光、立体感和方向一致的长投影，**融入感是量级差别**。

**写分镜的硬规则**：
1. 每段必带 `camera`（远景/中近景/特写/低机位/侧机位）、`shot`（人物占画面多大、偏左偏右）、
   `light`（光从哪来、轮廓光、接触阴影、色调统一）三段描述，不要用「同一机位同一景别」兜底。
2. **用定性词，别写精确数值**：「占画面高度约 1/3」实测出来约 1/2；
   「远景/中景/特写/仰拍」比数字可靠。
3. **同一个镜头内机位必须固定**，只在切镜时变；否则段与段拼起来会跳。
4. 机位变了但场景大类不变 → 复用同一张 `image_2`；换到完全不同的场景才换背景图。
5. 已见小瑕疵：`cam_low` 凭空多出一只手机（提示词没写），凭空道具仍要靠闸门/复检兜。

### 三个全局锁（写进模板，别每段手写）
`Q21_FIRST_TMPL` / `Q21_LAST_TMPL` 里固定带这三条，缺一个就会出事：

1. **视角锁**：人物在画面中的大小和位置与首帧/图1 一致，不靠近也不远离镜头；
2. **光线色调锁**：中性偏暖、均匀柔光、无硬阴影、低对比、白背心纯白、肤色不偏红绿；
3. **头发锁**：发长、分缝、发丝走向、垂落位置都不变，不飘散不变形不改遮挡关系。

第 3 条是"保持姿势"类尾帧翻车的直接原因——只写"姿势不动"，H3 会把头发演成飘动。

### 锐化过度 / 全身斑驳：四个根因，按这个顺序查

用户报"锐化过度"时，**不要先上后期负锐化**（做过，被否了——要源头修），回头查生成端：

1. **补白画布在污染**：`_fit_916` 旧版"末行拉伸 + 高斯模糊"补白，模型把模糊区当纹理先验，
   整张图跟着变毛糙 → 改 `_wall_grad()` 背景墙色平滑渐变，这是一处实打实的根因；
2. **LoRA 不是元凶，反而在磨皮**：实测去掉 8step LoRA 跑 20 步，拉普拉斯方差 239；
   加 LoRA 跑 8 步只有 122。觉得"脏"的时候应该把 LoRA 加回去，不是去掉；
3. **指令式提示词触发"高清复刻"**：写成"景别为…""以图2为准"这类命令句，模型会往高清重制走。
   改成叙述式（"她坐在桌前…"）就正常了。
4. **图2 锚图本身脏**（2026-11-04 新增，见上面「锚图卫生」）：锚上的微纹理/畸形/凭空道具
   被逐像素复刻放大。**这一条最容易误判**——因为它表现为"同一个提示词同一个种子时好时坏"，
   看起来像随机翻车，实际是锚在拉。判别方法：`hf_ratio` 看**生成帧**（不是锚），
   坏帧 37.7% vs 干净帧 8.5%；再看斑点是否与锚图高频纹理空间相关（坏帧 0.95 / 好帧 0.08）。
5. **"干净锚"也照样被复刻**（2026-11-05 新增，见上面「场景锚必须去纹理化」）：锚 hf 13.3%
   仍能出 58% 的帧，且换步数 / 换 image_1 / 改提示词 / 保边平滑 / 只处理地面全部无效。
   唯一有效的是把锚剥成色块（`quant_anchor()`，pipeline 已内置）。**这一条是全身景炸斑驳的
   主要原因**，比第 4 条更常见。

**两道闸门（pipeline 已内置，别再手工目视）**

| 指标 | 算法 | 阈值 | 抓什么 |
|---|---|---|---|
| `face_sharpness` | 脸框外扩 15% → 缩 256² → Laplacian 方差 | > 380 | 脸部被"高清复刻"（皮肤洒斑点） |
| `hf_ratio` | 全图 Sobel 模长 > 60 的像素占比 | > 20% | **四肢/躯干斑驳**（脸部指标可能仍达标） |

命中任一 → 种子 +1 重出（首尾帧重试环 + 质检都接了），实测重试必干净。

> 最终生产配置：关键帧 = **ray 模型 + 20 步、不带 LoRA**（`Qwen21-SceneRef916` / `Qwen21-SingleRef916`），
> 扩图 = **官方模型 + outpaint LoRA + 20 步**。上面第 2 条是排查时的对照结论，不是生产配置。

### 曝光

- `exposure_stats(path)` → (HSV 的 V 均值, 过曝像素占比)，过曝 = `V>248 且 S<40`（墙/高光死白）。
- **基准是本片第 1 段首帧**，不是场景锚。锚是扩图后的主参考，天然比出图亮一档，拿它当基准会把每一帧都判成偏暗。
- 闸门：`|ΔV| > 8` 报 FAIL；过曝阈值 = `max(1.5%, 锚过曝 + 1.5%)`。
- 2.1 每帧独立出图，同一提示词也会有 ±20 的亮度漂移，闸门只能拦不能修，所以配了
  `scripts/align_exposure.py`：**只动 LAB 的 L 通道做均值增益**，闭环收敛在 V 均值上，不做锐化/降噪/局部对比。
  用法见「命令」一节，跑完会把原图备份成 `*.prexpo.png`。

### 视频提示词会串段（最贵的一个坑）

每段的 `prompt_video` 是**独立存放的字符串**，改段 1 的文案**不会**同步到段 2/3。

症状：段 2/3 明明不该倒水，H3 却一直倒水，改多少遍关键帧都没用。
根因：段 2/3 的 `prompt_video` 里还留着段 1 那段
「倒水时水是透明的流动液体：从壶嘴连续流出，形成细长的半透明水柱…」——
**等于直接命令它去倒水**。

> 规则：`prompt_video` 里凡是"动作专属"的段落（倒水 / 走路 / 开门 / 吃饭），
> 复制到别的段时必须**整段替换**，不能只改前面那句动作描述。

### 场景道具的连续性

每段首帧都是独立生成的，桌上的道具会各画各的。两种做法：

| 做法 | 后果 |
|---|---|
| 段 2/3 把壶从提示词里去掉 | 段 1 尾有壶、段 2 首壶凭空消失，接缝直接崩 |
| ✅ 全程保留 + 提示词锁死 | 「壶静置在桌上，人物双手不触碰水壶，全程不出现任何倒水动作」 |

道具要写位置（"玻璃杯右后方、壶嘴朝左"），否则每段摆的地方都不一样。
尾帧还要单独强调"必须清晰可见，不能被遮挡、不能被裁到画外、不能消失"，否则模型会把道具挤掉。
朝向不一致（把手/壶嘴/靠背）是更隐蔽的一层，见「静物朝向必须首尾对齐」一节。

### 段间接缝

`segments[i].transition`（i 从 2 开始，对应 i-1 → i 那一刀）：

- `cut`（默认）：硬切，`-c copy` 拼接，时长精确；
- `dissolve` / `white` / `black` / `wipeleft` 等：走 xfade，**会吃掉 `xfade_seconds`（默认 0.4s）的总时长**。

选择标准：动作被省略（上一段停在半途、下一段已经做完）就用 `dissolve`，观众读成时间推移；
同一动作在继续就用 `cut`。

⚠️ **但叠化只在两段姿态接近时成立。** 实测「段 1 尾在低头倒水、段 2 首已端坐」这种
姿势差很大的接缝，0.4s dissolve 会出**双影**（倒水的胳膊和端坐的肩膀同时半透明叠在画面里），
比硬切难看得多。姿势差大就老老实实硬切——切镜本身是合法的叙事手段，观众读成换了台机位。

判断方法：把接缝前后各 4 帧抽出来横排看一遍（`ffmpeg -ss` 逐帧），
看得见两个脑袋就是不能叠化。

### 微调叠化接缝（2026-11-04 coffee 定稿，2.1 后端推荐）

**问题不是「帧剪得不够」，是相邻段首尾帧根本不是同一张图。** coffee 当时
`pipeline.py` 的 `frames` 里 `chain` 只在 2511 后端才 `shutil.copy` 上一段尾帧；
2.1 分支图2 恒定指场景锚、**不复制首帧**，所以 `segments[2].first` 是独立生成的。
**⚠ 2026-11-05 20:00 已修**：现在 2.1 后端 `chain` 段也复制（见本节末尾与
「首尾帧必须真链式」），下面这组数字是修之前的历史对照，别再拿它当现状：

| 接缝 | 灰度 MAE | 差 >10 的像素 |
|---|---:|---:|
| 段1尾 ↔ 段2首 | 8.25 | 18.9% |
| 段2尾 ↔ 段3首 | 9.46 | 18.5% |

姿态是同一动作的细微偏差（脸略偏、手/杯差一点），硬切就读成「顿一下」。
这时候**剪静止帧是治不好的**——实测把 86 帧定格全删（16.21s → 12.83s）反而
「去得太多」，动作链被压得太紧；用户最终选的是 **一帧不剪 + 接缝 0.25s 短叠化**。

**用法**（二选一，效果相同）：

```json
{ "join_dissolve_seconds": 0.25 }          // 写进分镜，之后每次 merge 自动生效
```
```bash
$PY $SK merge --storyboard s.json --join-dissolve 0.25   # 一次性覆盖
```

只对**没写 `transition` 且没有 `scene_change`** 的接缝自动加 `dissolve`，人工指定的
转场和跨场景段一律不碰；同时把 `xfade_seconds` 覆盖成该值。画面走 xfade、
音轨走 acrossfade，**动作帧零裁剪**。

**为什么是 0.25s 而不是 0.4s**：上面那条「倒水→端坐」的反例是 0.4s 叠化出的双影；
0.25s 只够跨过 6 帧，抹得掉细微姿态差，又来不及让两个姿势同时半透明显形。
coffee 两处接缝抽帧验收（前后各 3 帧横排）无重影、无跳变，成片 16.21s → 15.71s。

**什么时候不要用**：姿势差大（低头↔抬头、手在桌上↔手举杯）、跨场景、有明确
切镜意图 → 保持硬切。开工前按上面的判断方法抽帧看一眼，看得见两个脑袋就别叠。

> 仓库 `run_h3_long_video.py` 的 `static_compact` 是另一套流水线（Director/CLI）的
> 静止压缩，和这里的接缝问题是两码事；那边的 `static_compact_hold_seconds` 也调不到
> skill 的 `merge`。别拿剪帧去补「两张图不一样」的坑。

> **优先级变了（20:00 修）**：连续动作段现在一律用真链式（首帧 = 上段尾帧，
> 同一张图 byte-for-byte），0.25s 叠化退回成「切镜 / 姿势差大的接缝」的补救手段，
> 不再是默认解法。详见开头「首尾帧必须真链式」节。

## 用了哪些工作流

| 工序 | 工作流 | 关键点 |
|---|---|---|
| 首帧（2.1，默认） | `example_workflows/Qwen21-SingleRef916.json` | 单图 + 生成后补画幅；`Qwen21-SceneRef916.json` 是双图版（图1 人物 / 图2 场景锚） |
| 尾帧（2.1，默认） | 同上，以首帧为 image1 串行派生 | 视角锁 / 光线色调锁 / 头发锁写在模板里 |
| 扩图 9:16 | `example_workflows/Qwen21-Outpaint916.json` | 官方 int8 + 8step + outpaint LoRA，灰底 864×1536 |
| 首帧（2511，备用） | `example_workflows/Qwen2511-FirstFrame916.json` | 空 latent 720×1280，原生 9:16，不裁不补 |
| 尾帧（2511，备用） | `example_workflows/Qwen2511-Reference-Edit.json` | 以首帧为 image1 串行派生 |
| 视频 | `example_workflows/H3-Single-Clip-FirstLastRefs.json` | seconds/final 尺寸/megapixels 由分镜注入；**默认二采开、一采 4 步 + 二采 2 步**（2026-11-05 用户定；段级 `steps`/`two_pass_steps`/`two_pass` 可覆盖）；可改跑云端，见下一节 |
| **长视频（默认，2026-11-11 起）** | `user/default/workflows/H3-SelfLift_单图参考.json`；驱动副本 `skill/assets/H3-SelfLift_单图参考.base.json` | **ref2va 直出，不过首尾帧 / Director**；0.5MP / 24fps / 192 帧 = 8s；`first_frame` + `ref_images.ref_image_1` 做分段接续。见「分段接续链路」与手册 §13 |

> **别摘加速 LoRA 来救脸（2026-11-05 实测）**：identity 漂移疑似加速 LoRA 的锅时，做过对照——repo `config.json` 的 `lora_strength: 0`（Director 热读，不用重启）+ 段级 `steps:20, two_pass:false`，段 2 三种子 identity 0.258/0.215/0.120，**比加速档 4+2（最好 0.305）更差还更慢**（~3.7min vs ~2.3min）。根因是本地 H3 插值重绘脸，与 LoRA 无关；identity 闸门本地过不了，出路只有云端。完整数据在手册 §7.9。
| 质检 | `h3-video-qa` skill 的 `qa.py` | 10 项指标，失败码驱动重试 |
| 合并 | `imageio_ffmpeg` 自带 ffmpeg | 硬切走 `-c copy` 拼接，**保留 AAC 音轨**；有转场时走 xfade |

工作流文件在 `D:/Comfy-Desktop/ComfyUI-Installs/h3-long-video-repo/example_workflows/`（`WF_DIR`），
不在 skill 目录下。

## S4 可以改跑 RunningHub 云端（`video_backend`）

8 GB 显存下 H3 一段 5 秒要 4 分钟左右，`video` 是整条流水线最慢的一环。
想让这一段借云端的 24G/48G 卡，把它挪出去——**只有 S4 会离开本地**，
`check` / `frames` / `adopt` / 质检 / `merge` 全部不动。

```json
{
  "video_backend": "runninghub",
  "runninghub": {
    "host": "www.runninghub.ai",
    "api_key": "sk-...",
    "workflow_id": "1904136902449209346",
    "instance_type": "plus",
    "retries": 1,
    "node_map": {
      "first":   ["101", "image"],
      "last":    ["102", "image"],
      "ref":     ["111", "image"],
      "prompt":  {"node": "200", "field": "prompt"},
      "seconds": ["200", "seconds", "int"],
      "seed":    ["200", "seed", "int"]
    }
  }
}
```

命令行临时切换：`video --storyboard s.json --backend runninghub`（也认 `H3_VIDEO_BACKEND`）。

### 三条规矩

**① 别导我们本地那份工作流。** `H3-Single-Clip-FirstLastRefs.json` 除了 8 个 LoadImage
只有一个 `H3SingleClipDirector`——那是本地私有节点，而且 `director.py:31` 把模型名
硬编码成读 `ROOT/config.json`。云端既没这个节点也没这个文件，**请求管理员装也没用，
它一读配置就崩**。要跑的是平台自带的 MiniMax H3 首尾帧（`task_type=fl2v`）工作流，
节点编号和字段名跟本地完全不同，所以 `node_map` 必须显式指，不能猜。

**② `node_map` 照导出的 API 格式 JSON 填。** 在工作台把工作流跑通一次 → 导出
**API 格式**（不是 UI 格式）→ 里面每个节点是
`"101": {"class_type": "...", "inputs": {"image": ...}}`，`nodeId` 和 `inputs` 的键
就是 `node_map` 要的两样东西。键是「用途」，只认这几个：

| 用途 | 类型 | 说明 |
|---|---|---|
| `first` / `last` / `ref` | 图 | 先 upload 换成 fileName 再填；`ref` 可不映射 |
| `prompt` | 文本 | 走 `render_prompts()["video"]`，跟本地同一份模板 |
| `seconds` / `seed` / `steps` / `width` / `height` / `megapixels` | 数字 | 建议显式写第三位类型（`"int"` / `"float"`），平台要求跟原字段类型一致 |

**③ 云端重试是花钱的。** 本地 FAIL 重跑只花时间，云端每次都要扣费 + 排队，
所以 `retries` 默认 **1**，只在明确要重试时才调大。产物下载到
`Shared/output/h3-long-video/cloud/<分镜名>/`，`merge` 照样直接吃。

### 自检（不提交任务、不花钱）

```bash
$PY scripts/rh_backend.py --storyboard s.json --seg 1
```

打印生效的 host / workflowId / instanceType / 重试次数，以及每个用途准备覆盖到
哪个节点的哪个字段；缺图、缺映射会直接标出来。**先跑这个，再花第一笔钱。**

改完云端工作流、要重新验一遍接口形状：`$PY scripts/rh_selftest.py`（假服务器，不联网）。

### 云端渲染分辨率（`runninghub.megapixels`，2026-11-04 定）

云端的分辨率和本地是两回事：本地 `megapixels` 作用于工作流节点；云端只认 node_map 下发的
`width/height`（来自 `final_size`）。想让云端降分辨率省时间省额度，在 runninghub 块加一行：

```json
"runninghub": { "...": "...", "megapixels": 0.3 }
```

按 `final_size` 的宽高比换算宽高再吸附到 32 的倍数：0.2 → 320×608、**0.3 → 416×736**、
0.5 → 544×928、0.9 → 704×1280（= 旧默认，删掉这行即回原样）。
只压**渲染**分辨率；merge 仍按 `final_size` 输出（自动放大成片）。

实测（2026-11-04，6 秒段 / 8 步 / default 实例 / 同一对首尾帧同 seed）：

| 云端渲染分辨率 | 耗时 | 首帧锚定 MAE | 末帧锚定 MAE | 锐度（放大 832 后） |
|---|---|---|---|---|
| 704×1280（旧默认） | 未留档（像素是其 2.9 倍，应明显更慢） | 3.1~3.6 | 5.9~7.3 | 80 |
| 416×736（0.3MP） | **1.9 分钟/段**（含上传排队下载） | 3.7 | 8.7 | 43.3 |

抽帧目检通过（单人、动作单调、背景/杯柄朝向稳定）。**画质要求不高时 0.3MP 是性价比档；
要求高就删掉 `megapixels` 行回 0.9MP。** 云端的锚定对分辨率不像本地那么敏感
（0.3 锚定仍 3.7，比本地同分辨率好——云端权重强），所以云端放心降。

### ComfyUI Director 节点也有同一个开关（2026-11-04）

上方说的是 **skill 流水线**（`pipeline.py video --backend runninghub`）。ComfyUI 里的
`H3LongVideoDirector` 节点现在也有 `video_backend` 控件（`本地 ComfyUI（默认）` /
`RunningHub 云端`），走的是仓库里的 `cloud_backend.py`（从本目录 `rh_backend.py` 移植），
行为一致：只跑单采、`runninghub.megapixels` 控渲染分辨率、末帧用 `ffmpeg -sseof -1` 抽。

三个差异要知道：
- **关键帧永远在本机出**，云端只接视频阶段；
- 节点上的 `step` 走 `runninghub.steps`（默认 8），不跟本地 `megapixels` 联动；
- 云端帧数按 `h3_frame_count` 同一套「向上取 17n+5」规则（5s→124、6s→158），
  避免段段短一点、拼接时转场往前漂。

同一次改动还删掉了 `keyframe_steps`：关键帧和正片共用 `steps`。旧工作流打开时
前端会自动摆正 `widgets_values`（`web/director.js` 的 `migrateLegacyKeyframeSteps`）。

## 失败码 → 修法（自动重试用）

| 失败码 | 自动动作 |
|---|---|
| `mouth_open` | 换 seed 重跑；仍失败则把「双唇闭合」提到句首 |
| `cut_head` | 首帧改原生 9:16 重生成（不要裁切） |
| `identity_drift` | 换更清晰的参考脸；或降低步数改为 20 步质量档 |
| `motion_front_loaded` | 加大尾帧动作幅度，重做尾帧 |
| `motion_too_small` | 重做尾帧，动作差拉大 |
| `bad_ratio` | 首尾帧重做成精确 9:16 |
| `flicker` | 换 seed；必要时关 two_pass |
| **`anchor_first_off`** | 首帧没钉住（SSIM < 0.60）。查：UNET 是不是 hybrid/int8 量化、步数是不是压太低、LoRA 是不是 3 步蒸馏 |
| **`anchor_last_off`** | 尾帧没钉住。同上；另外检查尾帧和首帧比例是否一致（尾帧走 cover-crop，比例差会被裁） |
| **`frame_blotchy`**（帧级，生成时就拦） | 脸 Lap>380 或 全图高频>20% = 2.1 进"复刻模式"。换种子重出（自动，最多 3 次）；**反复触发就查锚图卫生**（`anchor_probe`），别改提示词 |

最多重试 3 次，仍不过就停下来交给人，不要无限重试。

`anchor_*` 两项是 2026-11-04 加的，只在 `video` 阶段算（要传 first/last 图）。
以前没有这两项，所以「首尾帧到底钉没钉住」是完全没有观测手段的——
换权重或压步数之前，先跑一段拿到基线，再改，否则换了也说不清变好还是变坏。

## 三道人工确认点必须真的停下来

- **① 分镜**：段数、每段秒数、动作、机位、**每段提示词**（用 `.prompts.html` 摊开给用户看，改完再跑），确认后才开始烧 GPU；
- **② 首尾帧**：每段给 2 张缩略图 + 质检结论，确认后才出视频；
- **③ 成片**：整片时长、音轨、总质检报告。

不要跳过 ①②，那是唯一能把"10 段全废"变成"1 段重做"的地方。

## 实测记录（2026-11-02，3 段 15 秒，720×1280，2.1 双图 + 扩图）

「倒水喝水」16 版关键帧 / 13 版视频打磨出来的版本，也是上面那一节结论的来源。

| 阶段 | 结果 | 耗时 |
|---|---|---|
| frames（每段：首帧 + 尾帧 + 各自扩图 9:16） | 每帧约 35 秒，一整段约 4 分钟 | 3 段 ≈ 12 分钟 |
| video（每段最多 3 次重试，失败码多为 `face_zoom`） | 段 1 第 2 次过、段 2 第 2 次过、段 3 第 1 次过 | 3 段 ≈ 13 分钟 |
| merge（全硬切） | 15.03 秒 / 6.1 MB | 10 秒 |

成本估算：**每 5 秒综合约 8 分钟**（首帧 1 + 尾帧 1 + 两次扩图 1.5 + 视频 4），比 2511 慢一倍，
但人物角度和身份稳得多。

## 实测记录（2026-10-31，2 段 10 秒，720×1280）

| 阶段 | 结果 | 耗时 |
|---|---|---|
| check | 通过 | 1 s |
| frames | 4 张图全部 PASS | 4 分 27 秒 |
| video | 段 1 第 1 次 WARN、段 2 第 1 次 WARN | 8 分 45 秒（含一次失败重试） |
| merge | 10.03 s / 4.4 MB / **AAC 音轨保留** / 段间无缝 | 10 s |

跑通过程中修掉的坑，都已在代码里兜住：

1. **Qwen 出图是 752×1392，不是精确 9:16**（FluxKontextImageScale 缩 1 MP 的取整），
   链式还会把它复制到下一段。已加 `normalize_916()` 自动裁到精确比例；
2. **H3 的 seed 在 `widgets_values[11]`**，不设它换 seed 无效——三次"重试"出来的视频帧号、峰值完全一样；
3. **参考脸别传相对路径**，`cv2` 读不到会让身份检查静默跳过（`id_sim_min` 为空）；
4. 首尾帧质检原来漏了嘴部，已补。

### 案例：三镜测试片暴露的三个首帧模板 bug

2026-10-31 做「全景 → 中景 → 特写」三镜测试时，三个首帧全部不符合预期：
都是**正面立正、脚尖朝镜头、视线看镜头**，`frame` 写了"膝盖以上"照样出全身。

查下来是三处硬编码：

| bug | 现象 | 修法 |
|---|---|---|
| `FIRST_TMPL` 硬编码「全身完整入镜，头顶和鞋底不被裁切」 | `frame` 字段任何值都失效，出不了中景/特写 | 改成「景别为 `{frame}`，构图严格按此景别裁切」 |
| `FIRST_TMPL` / `LAST_TMPL` 都没拼 `{gaze}` | 人物全部盯着镜头，`gaze` 字段无效 | 两处模板都补 `{gaze}` |
| `dir_lock` 用「躯干朝向：画面右方」这类元语言 | 生图模型不认，照样画正面站姿 | 翻译成可见特征：「右肩朝画面右方、左肩朝画面左方、镜头看到她的左侧身轮廓」+ 鞋尖指向 |

**根因**：空 latent 生图的先验是"女性全身正面像"。抽象的朝向词无法对抗这个先验，
必须落到"哪只肩在前、鞋尖指哪边、看到的是哪侧轮廓"这种**能被画出来的**描述。

同一次测试里，**同一个 `end_action` 文字在尾帧（编辑模式）生成正确、在首帧（生成模式）生成错误** ——
编辑模型的构图与姿态先验来自输入图，不受"正面像"先验支配。这是首帧难做、尾帧好做的根本原因。


### 案例：首尾帧"一模一样"是怎么回事（S1）

60 秒版的第一段，我写的是「重心从右腿缓慢移到左腿」。生成完一看，**首尾两帧的腿姿完全一致**，
整段等于复制了一张图。

量过之后才看清：

| 区域 | 明显变化像素 | 说明 |
|---|---:|---|
| 全图 | 43.5% | 看着"变化很大"，其实是被整图重绘骗了 |
| 头肩 | 57.7% | 背景和光影在变 |
| 躯干 | 44.6% | 同上 |
| **腿脚** | **28.2%** | 只有轮廓抖动，姿态没变 |

**根因不是参数，是动作落在了改不动的部位。** 改成「上身前倾 → 立直后仰 + 手臂从垂在身侧抬到身前」后：
首尾帧热力图集中在躯干和手臂，视频里手臂 5 秒内渐进抬起，质检 PASS、零警告。

这就是为什么 `check` 现在会：

- 把「换重心 / 换腿 / 换步 / 屈膝 / 抬脚 / 踏上 / 连走」从幅度计算里剔除——它们不产生可插值的姿态差；
- 只有头部 / 手臂 / 躯干的动作才计入 `amplitude`；
- 一段若一个有效动作都没有，直接判「无(必废)」并说明原因。

拿旧的 8 段分镜跑一遍，它会立刻指出段 1、4、7 是"只有走路"的废段——而这三段正是实际最差的。

## 推镜：这条链路最常见的废片原因

现象：分镜要的是中景全身，成片却在中段被推成一张脸部大特写，构图完全不对，而且脸会顺便被重绘。

H3 对"人脸在画面里变大"极度敏感。四种写法都会触发，实测倍数：

| 触发写法 | 实测 face_scale_drift | 说明 |
|---|---:|---|
| 首尾帧人物尺度变化（走近/迎面） | 1.13× 就废 | 见上节 |
| **段内"侧脸转正脸"** | **26.4×** | 正脸比侧脸视觉宽度大，模型当成"靠近了" |
| **提示词写半身、首帧却是全身** | **20.6×** | `frame` 字段与首帧景别冲突，模型为满足措辞而推近 |
| **低角度仰拍** | **7.7×** | 脸在画面上移变大，同样被当成靠近 |
| 正常全身横走段 | 1.13–1.31× | 这个区间是安全的 |

**检测**：`qa_lib` 的 `face_scale_drift` = 抽帧里 `face_ratio` 的 max ÷ min，
超过 `1.5` 直接判 `face_zoom` FAIL。这个指标比 `flicker` 准——推镜时 `flicker` 只是 WARN，
肉眼翻截图也容易漏，但 `face_scale_drift` 一算就出来。

**修法**：全身构图；`frame` 字段写什么，首帧就必须是什么景别；
避免"侧脸转正脸"的大角度转头（改成保持侧脸、只转 15–20°）；避免低角度仰拍。

## 已知边界

- 单人、固定或缓动镜头、一镜到底动线；多人交互和推拉摇移不可控；
- **段内不做画面位移**：H3 的插值只做"姿态 A → 姿态 B"，不会把人物从画面左边搬到右边。
  实测即使显式写「人物整体平移到画面左侧三分之一处」，出来的仍是"原地走 + 身体微微起伏"。
  "在走路"的观感靠**剪辑**：每段换背景与景别，配合步态、裙摆、头发的动，观众会自己补完"她一直在逛"；
- **视线默认飘向镜头**：H3 习惯把脸转过来看观众，必须在 `gaze` 字段写「全程不看镜头」才压得住；
- 侧面/背面镜头人脸检测不到，质检会报 `face_missing`，需人工判断；
- 合并要求所有分段同分辨率同帧率（本流水线固定 720×1280/24fps，天然满足）；
- 每 5 秒综合成本约 4 分钟（首帧 1 + 尾帧 1 + 视频 2）。

## 画幅铁律：每一帧必须扩成 9:16（2026-11-05 用户追问后补写）

H3 的 `first_frame` **不做等比适配**，非 9:16 的帧会被直接拉伸（3:4 帧进来人被拉长 35%）。
所以：

1. **出图阶段**：关键帧先按原图比例生成 → 末尾统一走 `outpaint_916` 扩成 768×1376；
2. **扩完必须复核画幅**（`_img_ratio_ok`）：不是 9:16 就退回 `normalize_916` 铺墙色补画幅，
   绝不把歪画幅留给下游；扩图的输出前缀必须唯一（`PFX_op_<hash>`），固定名字会撞车、
   捞到上一次甚至别的画幅的产物（实测出现过 9:16 帧变 1088×960）；
3. **出片阶段再守一道**：`cmd_video` 里 `_ensure_ratio_916`，非 9:16 自动补扩图，
   仍不合格直接拒绝出片；
4. 验收口径：`python -c "from PIL import Image; ..."` 把 20 张帧的宽高比打印一遍，
   全部应等于 0.5625（±0.01）。别只看一两张。

## 文生视频（用户没给参考图）：先造参考图，再走双图链路
（2026-11-07 晚用户纠正定稿）

用户原话：「对于这种没给参考图的，就是纯文本的，**后面首尾帧也要连续**，
另外利用这个工作流出的图更好 `image_krea2_turbo_t2i_int8`」；
随后纠正「**不是要先生成人物参考图吗**」。

### 一句话规则

`t2i` 只负责**造两张参考图**；造完之后关键帧一律走 `frames` 双图编辑。
别把「纯文本」理解成「整条片子不用参考图」——是**不用用户给的图**。

### 必须遵守的 8 条

1. **分流**：没给图 → 分镜写 `ref_person_prompt` / `ref_bg_prompt`，先跑 `refs`。
2. **`refs` 造两张原生 9:16 图**：人物设定图 + 无人场景空镜，各用独立 seed
   （禁 100000），自动回写分镜的 `reference_images` / `bg_ref` / `ref_pad_916:false`。
   已存在同名图默认跳过，换图用 `--retry`。
3. **人物参考图**：干净无缝背景、全身完整、正面、四肢不被裁、无道具/家具/文字；
   双人题材就并排站两个角色并写清「只有这两个人、没有第三个人」。
4. **场景空镜**：**完全没有人、人影或人体局部**，只含场景要素，从零生成；
   不能是抠图/透明底，也不能把生成结果喂回去（会代际累积锐化）。
5. **回写后 `check → frames`**：这时和「用户直接给了图」完全等价；
   首帧图2=空镜（原样、不量化），尾帧图2=本段首帧。
   （以上为**无原片链路**；复刻走 `source_frames`，见「一致性来源」。）
6. **段 2+ 首尾帧相连**：写 `chain:true`（文生视频不写也默认连续），
   段 N 首帧 byte-for-byte 复制段 N-1 尾帧；只有真换机位/换场景才
   `chain:false + transition:cut`。
7. **`push → resume`**：显式新建任务（`_new_director_job(sb)`）；job config 设
   `chain_segments=true` / `transition_mode=none` / `static_compact=false`
   （链式硬切、不叠化、不剪帧）。
8. **验收**：pipeline 端 + Director 端 md5 全相等、`continue_from_previous` 对齐；
   成片看链式接缝 MAE（<10）与 QA `static_ratio`。

### 分镜字段

| 字段 | | 说明 |
|---|---|---|
| `ref_person_prompt` | ✅ | 人物设定图提示词：干净棚拍背景、全身、正面、四肢完整 |
| `ref_bg_prompt` | ✅ | 无人场景空镜提示词，明写「没有任何人物/人影/人体局部」 |
| `ref_person_seed` / `ref_bg_seed` | 建议 | 两个不同 seed，禁 100000；不写则按分镜路径派生 |
| `ref_person_name` / `ref_bg_name` | 可选 | 输出文件名；不写用 `<prefix>_ref_person.png` / `_ref_bg.png` |
| `final_size` | ✅ | `[832,1472]`，9:16 硬锁 |
| `prefix` | ✅ | 帧文件名前缀，别和别的片子撞车 |
| `t2i_steps` / `t2i_unet` | 可选 | 造参考图的步数/权重覆盖，默认 8 步 + Aingmix Kroma v1.0 |
| `reference_images` / `bg_ref` | ⚠ 自动 | **由 `refs` 回写**，不用手写；跑完 refs 后自动出现 |
| `keyframe_backend: "krea2_t2i"` | ❌ 别写 | 已废弃的 t2i 直出关键帧链路，check 会告警 |
| 每段 `chain` | 建议 | 段 2+ 写 `true`；不写也默认连续 |
| 每段 `lighting_fx` | 建议 | 发光特效题材写 `true`：曝光门按 ±14、画质指标只存档（见下文坑清单） |

### 命令

```bash
$PY $SK check  --storyboard s.json    # 提醒先跑 refs
$PY $SK refs   --storyboard s.json    # Krea2 造两张参考图 + 回写分镜
$PY $SK check  --storyboard s.json    # 回写后应零错误
$PY $SK frames --storyboard s.json    # Qwen2.1 双图编辑出首尾帧
$PY $SK push   --storyboard s.json
$PY $SK resume --job <id>
```

### 档位（造参考图用，抄用户工作流真实设置）

`image_krea2_turbo_t2i_int8.json` 是子图结构、纯 t2i（顶层 6 节点，子图里
`EmptyLatentImage`，**没有任何图像输入**）。`pipeline.py` 用等价精简 API 图复刻，不导入子图：

- unet `krea2_aingmix_kroma_v10_int8_convrot.safetensors`（11-6 装的 Aingmix Kroma v1.0，
  **不是**官方 `krea2_turbo_int8_convrot`）
- clip `qwen3vl_4b_fp8_scaled.safetensors`（type `krea2`）/ vae `qwen_image_vae.safetensors`
- 8 步 / CFG 1.0 / euler + simple / `ConditioningZeroOut` 当负向（CFG=1 负向本来就无效）
- `prompt_enhance=false`、`enable_lora=false`（用户工作流就是关的，见手册 §4.1）
- 832×1472 实测 18–24s/张

### 为什么不能让 t2i 直出关键帧（2026-11-07 走过的弯路，别重复）

- t2i 没有图像输入，做不了「拿本段首帧当参考改尾帧」，身份只能靠静态锁 + 同 seed 硬撑；
  双图链路有 `image_1` 身份锚 + 尾帧图2=本段首帧，服装/机位/场景逐帧锁死，强得多。
- 逐帧 t2i 把身份漂移分散到 N+1 张图上；链式复制只解决段间，段内两帧仍可能换脸/换衣服。
- 双图链路有既定的画质闸门、曝光闸门、链式重出、Director 灌帧一整套配套；
  t2i 直出要另写一套，且已实测不如双图。
- 60s 纯文本链式版（任务 `20261107_191155_08f6f0f7`）作为**对照数据保留**：
  QA 零 flagged、接缝 MAE 0.4~6.5。它只证明「链式比独立切镜好」，
  **不证明「t2i 直出关键帧是对的」**。

### 链式验收与 Director 配置

`push` 后必须核对（文生视频是全链，**别沿用切镜版配置**）：

| job `config.json` 键 | 值 | 为什么 |
|---|---|---|
| `chain_segments` | `true` | 全链连续 |
| `transition_mode` | `"none"` | 只支持 `fade` / `generated` / `none`；首尾帧本就是同一张，硬切即可 |
| `static_compact` | `false` | 不剪静止帧，保住完整时长 |
| `final_width` / `final_height` | `832` / `1472` | 9:16 硬锁 |

两处 **md5** 都要验，缺一处都可能是「push 用独立 `start_manual` 顶掉链式」的老 bug：

```text
pipeline 端 : <prefix>_s01_last.png == <prefix>_s02_first.png
Director 端 : seg01_end_manual.png == seg02_start_manual.png
state 端   : keyframes[*].continue_from_previous 与分镜 chain 对齐
```

### 坑清单（只增不删，2026-11-07）

| # | 坑 | 后果 / 规避 |
|---|---|---|
| 1 | 没给图就直接跑 `frames` / `direct` | 缺 `reference_images` 直接 KeyError；先 `refs` |
| 2 | 用 `t2i` 直出全部关键帧 | 已废弃的错误链路，身份靠静态锁硬撑；正片用 `refs → frames` |
| 3 | 人物参考图里有道具/文字/裁断四肢/第三个人 | 瑕疵会被逐像素复刻放大；棚拍干净背景 + 全身完整 |
| 4 | 场景空镜里有人或人影 | 模型会画出第二个人；空镜必须完全无人 |
| 5 | `refs` 两张图用同一个 seed / 用 100000 | 构图撞车 / 已知坏种子；两张用不同干净 seed |
| 6 | 段间各自生成首帧 | 链式断：独立切镜版接缝 MAE 峰值 65.0、QA 报 `last_frame_drift`；必须 byte-for-byte 复制 |
| 7 | 16:9 出图再转 9:16 | 多余；**reflect pad 会复制出倒立人物**，正解见「9:16 是硬锁」节 |
| 8 | 发光特效题材不写 `lighting_fx` | 符文/光柱/光浪把段内曝光抬 8~26、人物区高频顶到 50%，会被曝光门与画质门误杀并重出 3 次；写 `lighting_fx:true` 后曝光按 ±14、画质指标降级为提示，靠拼图人工确认 |
| 9 | 「镜头全程固定」当成能锁住机位 | H3 仍偶发推近，已知边界，别拿尾帧 SSIM 当唯一口径 |
| 10 | ComfyUI 桌面版自动重启时不重试 | Qwen2.1 双图编辑每跑完 1~2 张就轮转日志、窗口期全 502；`comfy_run` 已加 `wait_service` + 重提交自愈 |
| 11 | `push` 复用上一个同段数的任务 | 会覆盖旧任务 config / 关键帧；用 `_new_director_job(sb)` 显式新建 |

### 实跑（2026-11-07 21:03–21:31，3 段 × 5s 端到端）

按 `refs → check → frames → push → resume` 完整跑通，任务 `20261107_212131_16c0f4f6`。

- **参考图**：`refs` 用 Krea2 出 `k2ref_person.png`（双人全身、干净棚拍背景）
  与 `k2ref_bg.png`（无人空镜），各 832×1472、独立 seed 913601 / 913602；
  `refs` 自动回写 `reference_images` / `bg_ref`，回写后 `check` 零错误。
- **关键帧**：`frames` 走 Qwen2.1 双图编辑，出 4 张 + 2 次链式复制；
  pipeline 端 `k2chain_s01_last == k2chain_s02_first`、
  `k2chain_s02_last == k2chain_s03_first` md5 全相等；Director 端
  `seg01_end == seg02_start`、`seg02_end == seg03_start` 同样相等，
  `continue_from_previous=[False, True, True]`。
- **配置**：`chain_segments=true` / `transition_mode=none` / `static_compact=false`。
- **成片**：`output/card-duel/星界卡牌对决_先造参考图_3段5s.mp4`，
  832×1472 / 24fps / 360 帧 / **15.0s** / 9.7MB，3/3 段 completed、8m44s。
  **QA 零 flagged**（首尾 SSIM 0.961–0.973、static_ratio 全 0）；
  全片相邻帧 MAE 中位 1.72，t≈10s 链式接缝峰值仅 6.5。
- **已知边界**：① t≈5.1s 有一帧 MAE 44.0 的景别跳变（段 1 全身远景 →
  段 2 中近景），首尾帧 md5 仍相等，是 H3 段内自行推近，属手册 §12.4 已记录坑，
  **不是链路断**；② 三段关键帧姿态闸门报 `disp 0.02~0.11`（双人正面站定、
  靠光效推进，估点器低估），但成片 static_ratio 全 0，没有定格。
- **服务自愈**：本次 `frames` 期间 ComfyUI 桌面版两次自动重启（502），
  加 `wait_service` + 重提交后续跑 4m14s 一次通过，见手册 §12.5。
- 样本：`k2ref_sheet.png`（两张参考图）、`k2chain_frames_sheet.png`（4 帧动作链）、
  `k2chain_final_video_sheet.png`（成片抽帧）、`sb_k2chain.json`（完整分镜，含 ref prompts）。

## 台词配音：voice 阶段（2026-11-07 用户定，「视频里得有配套语音」）

用户原话：「不是让你识别，我是说你**生成视频的时候要把配套的语音给生成进去**，
现在都是乱七八糟的声音」。

**根因**：H3 是音视频**联合生成**，音轨不是后期贴的，是模型跟画面一起"哼"出来的。
中文发音在 H3 上含混不可控；只写「环境音：低频嗡鸣…」它就吐一堆无意义嗡鸣＋
含混人声。提示词里点名台词只能让**口型对上**，救不出清晰可懂的语音。

**所以配音是出片流程的正式一步，不是 agent 手工贴一次**：分镜写 `dialogue` →
`voice` 阶段用中文配音引擎生成逐句语音 → 按全片时间轴 `adelay` 对齐 →
与压到很轻的 H3 原音轨混合（保留一点环境底噪）→ 替换音轨输出成片。

### 分镜怎么写

```json
{
  "voice_cast": {"male": "zh-CN-YunjianNeural", "female": "zh-CN-XiaoyiNeural",
                 "narrator": "zh-CN-YunyangNeural"},
  "voice_bg_gain": 0.10,
  "segments": [
    {"seconds": 5,
     "dialogue": [
       {"at": 0.6, "speaker": "male",   "text": "决斗开始！我的回合，抽卡！", "rate": "+10%"},
       {"at": 3.0, "speaker": "female", "text": "亮牌吧，让我看看你的手牌。", "rate": "+6%"}
     ],
     "soundscape": "与画面一致的低频能量嗡鸣、卡牌破空与光尘流动的细微环境声，音量平稳，不出现清晰说话声或哼唱",
     "music": "不加任何背景音乐"}
  ]
}
```

| 字段 | | 说明 |
|---|---|---|
| 每段 `dialogue` | ✅ | 台词列表；`at` 是**段内**秒，自动累加成全片时间轴 |
| `speaker` | ✅ | `male` / `female` / `narrator`，映射到 `voice_cast` 音色；也可单句写 `voice` 覆盖 |
| `rate` | 可选 | 语速，如 `+10%`；决斗喊话可快一点 |
| `voice_cast` | 可选 | 三种角色对应的 edge-tts 音色，默认 Yunjian / Xiaoyi / Yunyang |
| `voice_bg_gain` | 可选 | H3 原音轨保留的音量，默认 `0.10`（压成很轻的环境底噪）；设 `0` = 完全静音原轨 |
| `soundscape` / `music` | 建议 | 写清楚「不出现清晰说话声/哼唱」，别让 H3 再吐含混人声 |

### 命令

```bash
$PY $SK check  --storyboard s.json
$PY $SK refs   --storyboard s.json    # 文生视频才需要
$PY $SK frames --storyboard s.json
$PY $SK push   --storyboard s.json
$PY $SK resume --job <id>
$PY $SK voice  --storyboard s.json --src <成片.mp4> --out <配音版.mp4>
```

- `--src` 默认 `ComfyUI-Shared/output/h3_long_video/long_video.mp4`（Director 成片）；
  `--out` 默认 `<分镜名>_配音.mp4`。
- 只重编码音轨、`-c:v copy`，几秒完成，不重跑视频。
- 依赖 `edge-tts`（`pip install edge-tts`），走本机代理 `127.0.0.1:10808`；
  没网或没装会直接报错，不会静默出一条空音轨。
- `VIDEO_TMPL` 会把 `dialogue` 渲染成「对白（中文，人物按时间顺序开口，口型与台词一致）：
  0.6s 左侧男子开口说「…」」挂到视频提示词里；**手写的 `prompt_video` / `prompt_segment`
  也会自动追加这一行**，所以换任何链路口型都对得上。

### 验收

- `ffmpeg -i 成片` 确认有 `Audio: aac`，时长与视频一致。
- 逐秒 RMS 能量：每句台词的秒应有明显峰值（实跑 0.012→0.085），
  `max_volume` 不应到 0 dB（削波）。`alimiter=limit=0.95` 已兜底。
- 台词清单会打印出来核对时间点；要改词/改音色直接改分镜 `dialogue` 再跑 `voice`。
- ⚠ **别加 `-shortest`**：H3 音轨常比视频短几十毫秒，加了会把视频流截掉 4 帧
  （360→356）。`amix` 用 `duration=first`，音频略短无妨，视频流必须完整。

### 实跑（2026-11-07 21:40–22:15，3×5s 二采 + 配音）

- 分镜 `output/card-duel/sb_k2chain.json`：`two_pass:true / steps:4 / two_pass_steps:2`、
  6 句中文决斗台词（男 `zh-CN-YunjianNeural`、女 `zh-CN-XiaoyiNeural`）、
  `voice_bg_gain:0.10`。
- 新建任务 `20261107_215715_387640d2`，config 核对 `two_pass_default=true`、
  段级全 True、对白行已进提示词、链式 md5 全相等；`resume` **16m54s**（二采单段
  ~8m47s，单采整片 3 段才 8m44s）。
- 二采成片 QA **零 flagged**，首帧 SSIM **0.997~0.998**（单采是 0.961~0.973）、
  static_ratio 全 0；`voice` 出 6 句台词，只换音轨、`-c:v copy`。
- 最终成片 `output/card-duel/星界卡牌对决_3段5s_二采_配音版.mp4`：
  **832×1472 / 24fps / 360 帧 / 15.0s / 11.5MB**，视频 H.264 + AAC 48kHz stereo，
  mean -25.0dB / max -5.8dB 无削波，逐秒 RMS 峰值与 6 句台词时间点对齐。
- 抽帧 `k2chain_2pass_voiced_sheet.png`：全片同一机位同一双人，光效按亮牌 →
  巨兽虚影 → 光流对撞推进，二采后脸部/服装/符文光带更清晰、无斑驳噪点。

### 二采默认（2026-11-05 用户定，2026-11-07 补进 Director 链路）

- 出视频**不说就是二采开**：一采 `steps: 4` + 二采 `two_pass_steps: 2`。
- 分镜顶层 `two_pass: true` 即可，`_new_director_job` 会写
  `two_pass_default` 和段级 `two_pass`；段级同名可覆盖。
- ⚠ 之前 Director 任务模板里 `two_pass_default` 是 `false`，导致 skill 文档写"默认开"、
  实际跑出来全是单采（2026-11-07 21:57 修）。**新建任务后务必核对 config**。
- 二采是 `MinimaxH3LatentUpscaler3D` 放大 latent 再 2 步精修，耗时明显高于单采；
  8GB 卡 `two_pass_scale: 2.0` 若 OOM 就降到 1.5。

## 9:16 是硬锁，不是默认档（2026-11-07 卡牌战斗 60s 实跑）

- **现象**：想用 16:9 宽幅（1344×768）跑 Director，改 job `config.json` 的
  `final_width/final_height` 也不生效。
- **帧证据**：任务 `20261107_182638_17a464bc`，4×15s = 60.0s / 832×1472 / 1440 帧，
  成片 `output/card-duel/星界卡牌对决_60s.mp4`。
- **根因**：两道校验都在。`director.py` 建任务时 `final_width >= final_height` 直接 raise；
  `run_h3_long_video.py` controller 还有 `if "9:16" not in aspect_ratio: raise RuntimeError`。
  `resume` 虽然只读 config 不校验，但它拉起的是独立 controller 进程、照样校验。
  真要宽幅只能自己搭 API 图（`MiniMaxH3ReferenceToVideo` 本身支持 1344×768，实测提交成功）。
- **规避**：走 skill 就先把宽幅关键帧适配成 9:16。**别用 reflect / mirror pad**——
  实测会把人物倒影复制到上下，出现倒立的人；正解 = 原图 cover 缩放 + 中心裁切做
  大模糊铺底（GaussianBlur 34、亮度 0.62） + 清晰主画面按宽缩放居中 + 上下各 120px
  羽化过渡。H3 会把这种帧**重演绎成原生竖构图**，成片里看不到「横屏嵌竖屏」，实测有效。

### 长任务必须打进度心跳（同一次实跑）

- **现象**：用户报「卡住了」，但 ComfyUI 队列 `running=1`、模型已加载，其实在渲染。
- **根因**：自建脚本 `wait()` 轮询 30 分钟、一行输出都不打，看起来就是死锁。
- **规避**：所有等待循环每 15–30s 打一次（已耗时 + ComfyUI running/pending）；
  提交后立刻打印 `prompt_id`，并支持 `attach <seg> <prompt_id>` 接管已提交任务，别重复提交。
  Director 链路用 `/h3-long-video/status/<job>` 轮询，同样带心跳。
- **提示词写「镜头全程固定，不推近、不拉远、不切特写」锁不住 H3**：QA 段 1 报
  `last_frame_drift`（尾帧 SSIM 0.329），成片 t=6~14s 自己推成女子半身/近景特写。
  观感反而像剪辑过的电影片段，但尾帧对不上关键帧是事实；要严格锁机位得另想办法。

## 参考图必须只含人物，背景会被整套复刻（2026-11-05 实锤）

**症状**：每一帧的底部都长出一块木台面/桌面，提示词怎么改都去不掉。

**根因**：主参考图是一张**室内照**（她坐在木地板上，旁边有床头柜和床）。2.1 编辑模式
把参考图当底版，参考图里的木地板、床头柜就被一起复刻进每一帧。

**证据**：同一段、同提示词，只换参考图——

| 参考图 | 帧底部"暖棕高饱和"占比 |
|---|---|
| 整张室内照 | 0.48 ~ 0.94（明显台面） |
| 只裁头部+肩胸 | 0.03 ~ 0.04（干净） |

**规则**：
1. 喂给模型的主参考图**只保留人物**（头到胸或头到腰即可），背景一律裁掉；
   参考图只负责身份，场景由 `shot` / 场景锚负责；
2. 裁的时候把背景里的家具、桌面、地面这些**暖色大块**一并去掉，尤其木色地面；
3. `frames` 阶段现在会打印「底部木色占比 > 32%」的警告（`warm_bottom_ratio`），
   见到这条就先看图，别直接出片；
4. 这条和「锚图卫生」是一套：**参考图脏 → 每帧都脏；锚图脏 → 全片都脏**。

## 写提示词卡住了？翻外部案例库（2026-11-05 建立）

`ComfyUI-Installs/h3-long-video-repo/docs/模型手册/H3提示词案例库.md`
从 beatapi.io 的 H3 提示词画廊摘了 18 条高分提示词**原文**，每条后面带「本项目的用法」。
按问题直接跳：

| 症状 | 看哪条 | 抄哪一句 |
|---|---|---|
| 10 秒以上单镜里动作全挤在前半段 | 案例 2.6 雨夜站台 | `Shot 1 continues — 5.5s:` 逐点时间锚 |
| 画面里莫名多出家具/台面 | 案例 2.8 沙发动画 | `The room contains only A, B and C.` 正面清单 |
| 走位失控、人左右乱跑 | 案例 2.9 逃向石墙 | `runs about 1.5 meters in two steps, then shifts 0.7 meters to frame-left` |
| 动作一瞬间跳到位、没有过程 | 案例 2.9/2.10 | 身体受力因果链 `手腕转 -> 上身前倾 -> 肩膀跟上 -> 落点` |
| 多人/多物件串成一坨 | 案例 2.10 | `A = ... B = ...` 分区命名 + 方位锁 |
| 片子看起来"太干净不像真的" | 案例 2.13 书店精灵 | 手持微抖 + 对焦呼吸 + 曝光浮动 + 颗粒噪声 |
| 十段看起来都差不多 | 案例 2.14 辣妹 MV | `每段必须换一个不同的手部落点` |
| 不知道该写哪些负向词 | 案例库 §4 | 按 一致性/场景/镜头/光线/质感/文字 六类挑 8~12 条 |

**注意**：案例里的场景名词（桌面、书架、咖啡杯）是人家项目的，**照结构不照内容**——
这条和下面「提示词必须属于本项目」是同一件事的两面。

## 提示词必须属于本项目，不许照抄（2026-11-05 用户定）

**用户原话**：「你每个场景提示词要符合主参考图或者场景要求，不要照抄」。

模板里**不允许出现任何具体场景名词**（桌面、墙面、室内、白昼、杯子、咖啡……）。
每一句景物/服装描述都必须能追溯到本项目的某一个字段：

| 描述什么 | 必须来自 | 不允许来自 |
|---|---|---|
| 场景/环境/光线 | 主参考图、分镜 `shot`、`scene_text`、`outpaint_scene` | 模板里的固定句、上一个项目的分镜 |
| 服装款式与细节 | 主参考图、`wardrobe` / `wardrobe_items` | 模板默认句 |
| 人物外形 | `chars`、主参考图 | 别的项目的人物设定 |
| 动作 | 本段 `open_action` / `motion` / `end_action` | 抄别段的动作句 |

**已经渗进 pipeline 的历史残留（2026-11-05 全清）**：`原木色桌面`、`浅灰色墙面`、
`白色背心保持纯白`、`室内自然光`、`白昼自然光`、扩图补桌面。这些会让逛街分镜
下半截长出咖啡桌、把傍晚顶成白天。现在：

- 扩图默认只做「延续原有的地面/墙面/场景物件，光线方向、色温和时间感与原图一致」；
  要补什么由 `outpaint_scene` 明确写；
- 首帧无场景锚时的搭景句改成中性，项目有专属场景就写 `scene_text`；
- `pose_fix` 不再写"保持白昼自然光"，改成"白昼还是白昼、傍晚还是傍晚"。

**`check` 会自动 lint**（新增，2026-11-05）：
1. 没写 `wardrobe_items` → 提醒补（否则袖长/衣摆/配饰各编一套）；
2. 渲染出来的提示词里出现「不属于本项目」的词（`原木色桌面 / 浅灰色墙面 / 白色背心 /
   室内自然光 / 白昼自然光 / 咖啡 / 水壶 / 沙发 / 床 / 键盘 / 橱窗 / 步行街`，
   只要分镜里没出现过）→ 报「疑似照抄了别的项目」。见到这条就改提示词，不要忽略。

**新项目开工顺序**：拿到主参考图 → 先写 `shot`（场景）+ `wardrobe_items`（服装逐项）
+ `scene_items`（场景里允许有哪几样）+ `scene_text`/`outpaint_scene`（如需）
→ 再跑 `check` → 再出帧。

### 场景正面清单：说"只有这几样"，比说"不许有桌子"有效（2026-11-05）

来源：外部案例库 §2.8（`The room contains only the sofa, the blanket and the popcorn bowl.`）。

- 写在 `scene_items`（可写字符串或列表，段级覆盖根级）。
- 视频提示词会渲染成：
  > 场景里只有 <Picture1> 里已经存在的这些东西：{scene_items}；除此之外不加入任何家具、
  > 长椅、桌子、台面、栏杆或前景遮挡物，画面下方只有原有的地面，场景里的物件从头到尾不增不减。
- **为什么要正着写**：负向串（"不许有桌子/椅子/栏杆"）模型会漏，而且漏的时候你不知道；
  正面点名时，模型没有"自由发挥的空间"，画面上每一块地方都有归属。
- `check` 里第 3 条 lint：某段没写 `scene_items` → 提醒。
- 写完最好跟 `shot` 对一遍：`shot` 里提到的东西必须都在 `scene_items` 里，
  `scene_items` 里也不该出现 `shot` 里没有的东西（写了就会画出来）。

### 单镜时间锚：`beats` 会自动摊成时间点（2026-11-05）

来源：外部案例库 §2.6 的 `Shot 1 continues — 5.5s:` 写法。

`s0` 的 `beats` 现在会被摊到时间轴上，视频提示词自动多出一句：

> 时间锚（严格按这个节奏推进）：0.0s 保持首帧姿态；2.2s 上身前倾凑近完成；
> 4.5s 双手扶上玻璃边框完成；5s 才停在尾帧姿态。每个节拍只在自己那个时间点完成，
> 不许提前把后面的动作做完，也不许中途停住不动；最后一个节拍之后不要定格，
> 保留呼吸、眨眼和发丝衣料的细微变化直到结束。

- 规则：末尾留 10%（0.4~1.2 秒）收尾；其余时长按 `beats` 数量等分。
- **为什么要有**：15 秒单镜实测「三个动作全挤在前 50% 演完、后半段定格」
  （质检 `motion_front` 123%），根因就是只有"先…再…最后…"，一个时间点都没有。
- 所以 `beats` 要写**能看出完成状态**的短语（「上身前倾凑近」「双手扶上玻璃边框」），
  别写「自然地动一下」这种没法验收的。
- 4 秒以下的段不生成（放不下时间锚，写了反而乱）。

### 首尾帧装束一致：尾帧拿本段首帧当图2

- 尾帧的 图2 **是本段首帧**（不是全片场景锚）；本段首帧本身是在锚上生成的，场景连续性不丢，
  但袖长/衣摆/配饰有了同一镜头的硬基准。
- 手写的 `prompt_first` / `prompt_last` 也会自动追加「以图2为唯一标准、逐项照抄」的尾巴，
  自定义提示词绕不过这套规则。
- **实测边界**：长袖（泡泡袖/喇叭袖）+「双手抬到胸前」＝ 袖口必然上移，加物理句也压不住。
  给长袖设计动作时别用抬臂到胸前，改成指向、扶玻璃/桌面边缘、掖耳侧头发、手垂身侧。
  详见手册 §1.23。

## 分镜的可选字段

### 根字段（整片级）

| 字段 | 作用 | 默认 |
|---|---|---|
| `title` | 成片名，也是 `<name>.mp4` 的文件名 | 必填 |
| `prefix` | 关键帧文件名前缀（`<prefix>_s01_first.png`） | `h3lv` |
| `style` | 画风描述，拼进每条提示词 | 真实摄影 |
| `final_size` | 成片尺寸 `[720, 1280]` | `[720, 1280]` |
| `seconds_per_segment` | 每段默认秒数 | 5 |
| `megapixels` | H3 原生出图分辨率（0.3 → 约 416×736，再放大到 final_size） | 0.3 |
| `chars` | 人物设定（姓名/年龄/外形），拼进提示词 | 空 |
| `wardrobe` | 服装硬锁：颜色/款式写死，防跨段换衣服 | 空 |
| `wardrobe_items` | **服装逐项清单**（袖型/袖长/袖口结构/衣摆/腰头/配饰/鞋）。首尾帧和视频都会点名照抄，不填就只剩一句"服装一致"，袖长衣摆会各编一套。写法见「首尾帧装束一致」 | 空（会提醒） |
| `scene_text` | 首帧没有场景锚时的**搭景句**（本项目的场景，别写别的项目的） | 中性"光线方向/色温与图1一致" |
| `scene_items` | **场景正面清单**：这一段画面里"允许有"的几样东西，逐项点名（石板路/玻璃橱窗/暖黄灯箱/店铺门面）。视频提示词会写成「场景里只有 <Picture1> 里已经存在的这些：…；除此之外不加入任何家具、长椅、桌子、台面、栏杆」。**不填就只给中性句**，模型容易自己在画面下方加台面（踩过）。见「场景正面清单」 | 中性"地面/背景轮廓/光线"（会提醒） |
| `outpaint_scene` | 扩图到 9:16 时补的画面内容（默认只"延续原场景"，**不再写死桌面/墙面**） | 中性延续 |
| `soundscape` | 环境音描述（H3 自带音轨）。**不写就只给"与场景一致的稳定环境音、不加音乐"**，等于把音频交给模型乱编 | 中性环境音 |
| `music` | 配乐描述。不写 = 明确写"不加任何背景音乐" | 无音乐 |
| `stance` | 站姿/坐姿描述 | 空 |
| `reference_images` | 人物参考图列表，第一张是图1（`["ds_face.png"]`） | 必填 |
| `scene_ref` | **场景锚**：扩好的 9:16 主参考，全程当图2 用，恒定不链式。写了它就不再自动固化锚。**锚必须干净**，见「锚图卫生」 | 空 |
| `anchor_guard` | 生成锚（段1首帧）时自动追加的约束句；写自定义句覆盖，`false` 关闭。默认值见 `ANCHOR_GUARD` | 内置一句 |
| `face_sharp_max` | 脸部 Laplacian 闸门阈值，超了换种子重出 | 380 |
| `keyframe_seed` | 关键帧固定种子；**不填就每帧都变**，改一个字就换一张脸 | 随机 |
| `keyframe_lora` | 2.1 帧生成加速档：`"viggle6"`（Viggle unmerged 6 步，编辑模式实测最优） / `"lightning4"`（只适合抽构图，编辑模式会改服装漏动作） / 省略 = base20 质量基准 | 空（base20） |
| `xfade_seconds` | 转场时长，默认 0.4 | 0.4 |
| `join_dissolve_seconds` | **接缝微调叠化**（秒）：给没写 `transition` 的连续动作接缝自动加短 dissolve，2.1 后端实测 0.25 最优；`0` / 不填 = 关闭，姿势差大时别用。见「微调叠化接缝」 | 0（关闭） |

### 分段字段

除了 `open_action / end_action / motion / chain`，还可以用这些字段精确控镜：

| 字段 | 作用 | 默认 |
|---|---|---|
| `transition` | 本段与**上一段**之间的转场：`cut` / `dissolve` / `white` / `black` / `wipeleft` 等。第 1 段无效 | `cut` |
| `dir` | 方向契约 `{heading, body, face, toe}`，见文末「方向契约」。**走路段必填** | 从文字推断（低置信） |
| `head` | 头部朝向（尾帧派生时用） | 头部朝向与首帧保持一致 |
| `frame` | 入画范围：`人物全身，头顶和鞋底` / `头顶与双肩` / `整张脸` | 人物全身 |
| `gaze` | 视线落点，写「全程不看镜头」可抑制 H3 把脸转向观众 | 视线保持与首帧相同方向 |
| `move` | 画面内位移描述（实测效果有限，别指望它搬人） | 空 |
| `beats` | 动作节拍列表，如 `["躯干前后倾", "抬头"]`，优先于关键词自动识别 | 自动识别 |
| `amplitude` | 变化幅度：`small` / `medium` / `large`。**必须与 `seconds` 匹配**，见幅度对照表 | 按 `beats` 推算 |
| `prompt_first` / `prompt_last` / `prompt_pose_fix` / `prompt_video` / `prompt_segment` | 直接写死这一段送进工作流的提示词，**不再套模板**。分段写只影响该段，写到根上是全局默认。对应关系：`first`→首帧生成、`last`→尾帧编辑、`pose_fix`→首帧转身修正、`video`→H3 视频、`segment`→Director 整段（预览页第 5 栏） | 由模板渲染 |

`amplitude` 与 `seconds` 的匹配规则（`check` 会校验）：

| `amplitude` | 能配的最长时长 | 含义 |
|---|---|---|
| `small` | 5 秒 | 单部位小幅：转头 20°、抬手 20 cm |
| `medium` | 6 秒 | 单部位大幅或两部位：后仰 30°、手抬到身前、转头 50° |
| `large` | 8 秒（不推荐） | 多部位大幅：后仰 40° + 双手大位移 + 转头 |

**写 8 秒却只给 `medium` 幅度的段，出来必然是"站着晃动"或"走出去又回来"。**

---

## 方向契约：脚、身、脸必须自洽

### 为什么单列一节

H3 不会推理"这个人在往前走"。它只把首帧插到尾帧。如果首尾帧里脚尖朝一处、脸朝另一处，
它只能挑一个方向插，结果就是**倒着走、原地扭、或者走出去再回来**。

实测事故（2026-10-31，段 1）：`end_action` 写"身体侧向画面**左**方"，而整段行进方向是画面**右**方；
`head` 默认值"头部仍然朝向镜头"把背对镜头的人强行掰回头。出来的首帧：
**脚尖朝右下、脸朝右、躯干背对镜头**——三者互相差 90°，H3 拿到后按脸的方向倒退走。

### 结构

```json
"dir": { "heading": "right", "body": "away", "face": "right", "toe": "right" }
```

坐标系按画面 2D，0° = 画面右方，逆时针为正：

| 值 | 角度 | 含义 |
|---|---|---|
| `right` | 0° | 朝画面右方 |
| `away` | 90° | 朝画面纵深（背影、走远） |
| `left` | 180° | 朝画面左方 |
| `toward` | 270° | 朝镜头走来（**会触发推镜换脸，慎用**） |
| `still` | — | 本段不位移，只改上半身姿态 |

- `heading` 行进方向；`body` 躯干朝向；`face` 脸的实际朝向；`toe` 脚尖朝向
- `toe` 缺省 = `heading`。**脚尖必须踩在行进方向上**，这是唯一不允许偏离的量

### 硬规则（`check` 会拦）

| 条件 | 判定 | 原因 |
|---|---|---|
| `angle(heading, body) > 90°` | 错误 | 身体朝反向，人在倒着走 |
| `angle(heading, toe) > 45°` | 错误 | H3 会顺着脚尖走，不按 heading |
| 段内含走动 且 `angle(heading, face) > 90°` | 错误 | 边走边回头，H3 拆成两个方向 |
| `angle(body, face) > 100°` | 错误 | 颈部转不了这么多，模型只能扭腰扭脚凑 |
| `45° < angle(heading, face) ≤ 90°` | 提醒 | 半侧脸前进，风险中等 |

**回头（over-shoulder）的合法写法**：`body=away, face=left|right`（差 90°），配合 `heading=away`。
`body=away, face=toward`（差 180°）解剖上不存在，生成结果必然是脚尖与脸各朝一边。

### 首尾帧自动识别

`frames` 阶段用 insightface 算**有符号 yaw**（>0 脸朝画面右，<0 朝画面左，|yaw|>0.8 接近侧脸/背面），
自动判三件事：

1. 躯干背对镜头但 `|yaw| < 0.20`（脸几乎正对镜头）→ 报错，H3 会按脸的方向把人翻过来
2. 首尾帧 yaw 符号翻转 → 报错，H3 会跟着扭头
3. 段内 `|Δyaw| > 0.35`（约 30°）→ 提醒，H3 可能把转头演成转身

**脚尖朝向没有可用的自动检测**（缺 pose 模型），必须目视确认。出图后看一眼：
两只鞋尖是否指向同一侧、是否等于 `heading`、是否与脸同向。

### 提示词里已锁定的三句话

模板（`pipeline.py`）已把方向写成硬约束，不需要每段重复写：

1. 首帧/尾帧都拼 `{dir_lock}`：躯干朝向 + 脚尖朝向 + 脸朝向 + 行进方向，逐条锁死
2. `VIDEO_TMPL`：**"全程朝双脚脚尖所指的方向移动，Picture1 里脚尖指向哪边就只朝哪边走"**
   —— 旧版写的是"朝人物面朝的方向"，回头姿势会直接把 H3 带向反面
3. `VIDEO_TMPL`：**"头部和视线可以转向别处，但转身不带动脚步"**

---

## 自动闸门（2026-10-31 下午迭代）

frames 阶段现在全闭环，不再靠人眼把关朝向：

### 1. 首帧方向闸门
生成先验是"正面全身像"，侧身/背面段落先测 yaw，不符就进编辑模式转身；
转完再测，**不对就换种子重转（最多 3 次），仍不行就水平镜像兜底**
（镜像方向百分百正确，代价是挎包/发缝等不对称细节左右互换，state 里记 `mirrored: true`）。
判定标准：`body=left` 要求 yaw < -0.25，`body=right` 要求 yaw > +0.25（`direction.yaw_matches`）。
闸门前先 `count_faces` 数人脸，>1 = 参考图泄漏（悬浮脸）或路人入镜，记 `face_leak`。

### 2. 尾帧转头门控
end_action 写"视线移向远处"这类词，编辑模式必然演成转头（实测 3 个种子 Δyaw 0.55–0.86）。
尾帧生成后测 `|Δyaw| ≤ 0.35`，超了换种子重出 3 次；仍超限只警告不拦截，
**因为尾帧不能镜像**（会把背景服装一起翻，破坏与首帧的场景连续性）。
例外：`heading=still` 的静止段，缓慢转头本身就是设计动作，Δyaw 超限降级为提示不算 FAIL。

### 3. 服装/光线硬锁（wardrobe 字段）
编辑模式两次把上衣改成露脐、光线改成黄昏逆光。根因：只写"不改变服装"没用，要点名。
分镜 JSON 里写 `wardrobe`（全局或分段），模板注入成：
`服装一件都不许改：{清单}，衣长、裙长、颜色、款式与图中完全一致`，
并追加"上衣下摆必须盖过腰头、不许露出腰腹"和"保持白昼自然光，不要黄昏/橙色暖调"。
首帧、尾帧、姿态修正三个模板都吃这个锁。

### 4. 跨场景 QA 豁免
分段 JSON 写 `scene_change: true`：视频质检里 backtrack / flicker 降级为 WARN 不拦截
（背景整体变化时人脸框摆动会被计成往返行程，实测假阳性 1.78~3.71），
身份、尺度漂移、运动量照常查。

### 5. 已证死路（别再试）
- 提示词控制景别：中英文"medium shot / 特写构图"全部无效，工作流只出全身竖构图。景别只能后期裁切。
- "镜头看到她的左侧面"这类视角翻转描述：模型会理解成"人朝左"（实测转反）。只用
  "鼻尖、下巴、胸口、肚脐、两只脚尖全部指向画面右边缘"这种多锚点同向写法。
- 编辑模式改背景时锁不住服装光线，除非按第 3 条点名锁死。

### 6. 动作变化量门控（body_motion，2026-10-31 傍晚新增）

转头门控曾经把唯一的变化（转头）摁掉，出来两张一模一样的图——教训：
**头要稳 ≠ 身体不动，两个门控必须同时成立**。

- `direction.body_motion(first, last)`：YOLOv8n-pose（ONNX，13MB，onnxruntime CPU）出 17 个身体关键点，
  位移按躯干长归一化，与人物远近无关。返回 disp（加权平均位移）、arm/leg（变化落在哪）、max_kpt、x_drift、zoom。
- 阈值：`disp < 0.12` 判 FAIL"首尾帧几乎同姿态"。标定：同姿态 0.08 / 抬手扶包 0.15（右腕 0.81）/ 别头发 0.36（右腕 1.32）。
- 权重路径：`ComfyUI-Shared/models/detection/yolov8n-pose.onnx`（hf-mirror 下载，GitHub 太慢）。
- 设计分镜的推论：**end_action 必须落在手臂上**（提包带、扶包带、别头发），腿改不动、纯转头会被门控拦。
  一个 5 秒镜头给两个微动作（手 + 头微转 ≤15°）。

---

## 7. 强动作镜头豁免（2026-10-31 晚）

讨论「女生被霸王龙追、疯狂逃跑」时发现：这类镜头的难点和静态段完全相反，
但**模板里那几句为静态镜头写的硬约束会把它直接摁死**。新增 `action_locks()` 逐项放行。

### 分段字段

| 字段 | 替换掉的原句 | 怎么写 |
|---|---|---|
| `camera` | 「固定机位，摄影机不推进…位置从头到尾保持不变」 | 跟拍/推拉/允许人物变大变小 |
| `mouth` | 「双唇自然闭合、不露牙、不张嘴」（首帧尾帧视频三处） | 喘气/喊叫/张嘴的表情描述 |
| `feet` | 「双脚完整着地…不踮脚」 | 写 `"sprint"` 走内置冲刺文案，或写整句 |
| `pace` | VIDEO_TMPL 里的「缓慢过渡到尾帧姿态」 | 「爆发式、急剧地」 |
| `extra` | 「画面中只有一个人」 | 允许出现的第二个东西（巨大阴影、尘土） |

缺省返回值与原硬编码完全一致，老分镜行为不变（已用三镜测试.json 回归）。

###「逃跑」到底更难还是更容易

**更容易的三件事**
1. **动作变化量天然达标**。静态段最难的一关是 `disp ≥ 0.12`，只能靠提包带、别头发这类微动作去凑（0.15/0.36）。双臂大幅反向对摆轻松超过阈值。
2. **方向契约最简单**。逃跑是 `heading/body/face/toe` 全同向，不存在"走向右但脸朝左"这类反向构图；背对拍摄还顺带消灭了"盯着镜头"这个最脏的失败模式。
3. **不怕后继无力**。静态段被判 `motion_front_loaded`（前 50% 完成 818% 的画面变化），持续运动不会出现前重后轻。

**更难的三件事**
1. **腿改不动**。编辑模型实测会把腿照抄首帧（`LEG_MAIN` 规则已把「屈膝/换腿/换步」判成无效动作量）。跑步的核心差异件恰恰是唯一改不动的部位——所以要把首尾差异全部转移到**手臂相位 + 躯干前倾 + 头部转向 + 画面位置**上。
2. **周期性动作会被折叠**。跑步是循环动作，H3 拿到两帧容易演成"跑出去再跑回来"。解法是让首尾帧有**单向、可累积**的差异：横向跑用镜头跟拍（人物位置不变、背景流动），纵深跑远用人物变小。**不要用「朝镜头跑来」**（`BAD_SCALE`：H3 当成推镜并重绘脸部，实测第 4 秒整张脸换人）。
3. **第二个主体（恐龙）不能入画**。多主体会出现多余肢体、身份串味。用 `extra` 写巨大阴影 + 尘土 + 构图留白，画面里的主体始终只有一个人。

### 已落地
`assets/追击测试.json`：3 段 15 秒（横向跟拍向右跑 → 背影沿路跑远 → 奔跑中回头看 + 巨大阴影），
`check` 通过。段 2 是**纵深缩小是否安全**的测试项（目前只验证过放大是危险的）。
