# H3 长视频工作台

ComfyUI 自定义节点 + 两个网页面板，用来把「一段分镜」跑成「一条成片」。

核心不是「能不能生成 60 秒」，而是**每段的通过率**。所以整条链路的重点在
「每道工序后都过一遍量化质检」，而不是把节点连起来就完事。

---

## 两个面板，一个服务

2026-11-17 起合并成**一个进程、一个端口**（`tools/serve_app.py`，默认 8900）：

| 地址 | 面板 | 干什么 |
|---|---|---|
| `/` | 入口页 | 两个面板二选一 |
| `/remake` | **视频复刻导演台** | 上传参考视频 → 切段 → 逐段跑片 → 贴回原音 → 合成成片 |
| `/h3-long-video/app` | **SelfLift 出片台** | 参考图条带 · 全片提示词 · 分段卡片 · 尾帧接首帧 |

两者定位不同，别混用：

- **复刻台**：你手上有原片，想复刻它的分镜节奏。走黑鹤二采链路（一采 0.2MP、
  二采 2×），用「前段尾帧 → 后段首帧」做链式承接。
- **出片台**：你从零写分镜。走 SelfLift 单图参考 ref2va 直出（默认链路），
  用「尾帧接首帧 + 全片唯一无人空镜板」做接续，比首尾帧链路快、接缝更自然。

合并前它们是两个独立服务（8900 / 8901），要开两个窗口。合并不是把代码抄一遍，
而是加了一层 `tools/remake_bridge.py` —— 详见下文「单端口合并」。

---

## 快速启动

```bat
:: 1) 启动 ComfyUI（只当算力后端，可以在别的机器）
::    默认地址 http://127.0.0.1:8188

:: 2) 启动工作台
tools\serve_app.bat
::    等价于 <ComfyUI venv>\python.exe tools\serve_app.py --port 8900 --host 0.0.0.0 --open
```

`serve_app.bat` 会自动找 ComfyUI 的 venv（两个常见位置），找不到时用
`set H3_PY=C:/path/to/python.exe` 指定。

手动启动：

```bash
<ComfyUI venv>/python.exe tools/serve_app.py                # 127.0.0.1:8900
<ComfyUI venv>/python.exe tools/serve_app.py --port 8900 --host 0.0.0.0 --comfy 192.168.1.9:8188
```

自检：`curl http://127.0.0.1:8900/health` —— 返回 `remake: true` 说明两个面板都挂上了，
`remake_error` 会告诉你复刻台挂载失败的原因（出片台不受影响，会照常服务）。

依赖只有两个，装在 ComfyUI 那个 venv 里：

```bash
pip install -r requirements.txt      # requests, imageio-ffmpeg
```

---

## 目录结构

```
h3-long-video-repo/
├── __init__.py                  ComfyUI 自定义节点注册入口（WEB_DIRECTORY = ./web）
├── director.py                  Director 节点：故事板 → 关键帧 → 视频 → 合并
├── run_h3_long_video.py         CLI 流水线（Director 节点内部也调它）
├── selflift_panel.py            出片台后端（aiohttp 路由 + 出片调度）
├── remake_panel.py              复刻台后端（标准库 http.server 的 H 类）
├── selflift_schema.py           分镜 JSON 的校验与规整
├── selflift_lint.py             分镜体检（python selflift_lint.py --cfg <segments.json>）
├── cloud_backend.py             云端出片后端适配
├── config.json                  全片默认参数（采样器 / 二采 / 画幅 / 转场 …）
├── assets/
│   └── remake_prompt.txt        复刻台的全片公用提示词模板
├── web/
│   ├── selflift_app.html        出片台前端（单文件，无构建步骤）
│   ├── remake_app.html          复刻台前端（单文件，无构建步骤）
│   ├── director.js              Director 节点的画布侧脚本
│   ├── single_clip.js           单段出片脚本
│   └── manual.html              节点内嵌说明
├── tools/
│   ├── serve_app.py             ★ 工作台服务唯一入口（一个端口两个面板）
│   ├── serve_app.bat            双击启动（纯 ASCII + CRLF，cmd 的 GBK 坑）
│   ├── remake_bridge.py         ★ 复刻台 → aiohttp 的伪 handler 适配层
│   ├── build_director_workflow.py  生成 Director 示例工作流
│   ├── cleanup_comfy_assets.py  清理 ComfyUI input/output 里的中间产物
│   └── sync_manual.py           同步 manual.html
├── skill/
│   ├── SKILL.md                 流水线作业手册（agent 侧读这份）
│   ├── assets/                  ★ 运行必需资产（见下节「工作流在哪」）
│   │   ├── H3-SelfLift_单图参考.base.json   ★ 默认链路 base 工作流（22 节点）
│   │   ├── H3-SelfLift_黑鹤二采.base.json   ★ 复刻台链路 base 工作流（37 节点）
│   │   ├── selflift.global.json           ★ 唯一一份全局约束（head/tail/参数）
│   │   ├── remake_prompt.txt              复刻台默认提示词模板
│   │   └── segments.selflift.example*.json 分镜样例（单人 / 2 人 / 3 参考图）
│   └── scripts/
│       ├── run_selflift.py      ★ 两个面板共用的出片驱动
│       ├── video_remake.py      复刻流水线 CLI（切段 + 抽帧 + 建 cfg）
│       ├── sync_repo.py         把活跃 skill 同步进本仓库（含 assets 白名单）
│       ├── pipeline.py          提交 prompt / 轮询 history
│       ├── consistency.py       接缝 MAE、逐秒亮度 / 色偏
│       ├── align_exposure.py    CIE Lab 对齐（喂下游前必做）
│       ├── frame_exact.py       帧级精确切段
│       └── …                    探针与实验脚本
├── api_workflows/               API 格式工作流（一采 / 二采）
├── example_workflows/           可直接拖进 ComfyUI 的示例
│   ├── H3-Long-Video-Director.json   Director 节点用的示例
│   └── lab/                         实验性工作流（`lab_posefix.py` 仍在用）
├── docs/                        规范文档（见文末索引）
├── tests/                       pytest（图结构 / 面板单元）
└── _trash/                      手动清理出来的旧备份（已 gitignore，可随时挪回）
```

---

## 工作流在哪（`skill/assets/*.base.json`）

**两条生成链路各有一份 base 工作流**，它们不是示例，是每次出片都要 `json.load`
打开的模板 —— `run_selflift.build_wf()` 读进来，改几个节点的 `widgets_values`，
再提交给 ComfyUI。

| 链路 | base 工作流 | 节点数 | 谁在用 |
|---|---|---|---|
| `selflift` | `H3-SelfLift_单图参考.base.json` | 22 | 出片台默认、`cfg["workflow"]` 缺省值 |
| `blackcrane` | `H3-SelfLift_黑鹤二采.base.json` | 37 | 复刻台（`remake_panel.BLACKCRANE_BASE`） |

选哪份由 `cfg["workflow"]` 决定，面板上通过 `#engineSel` → `/set --engine` 切换；
链路名靠文件名里的「黑鹤」二字识别（`run_selflift.profile()`）。

### 为什么以前没入库，现在怎么保证不再漏

`skill/scripts/sync_repo.py` 本来是「消除两份源码」的同步器，但它**只同步
`scripts/*.py` 和 `SKILL.md`，`assets/` 从未纳入** —— 结果工作流只活在本机
`.workbuddy/skills/h3-long-video/assets/` 下，clone 仓库跑不起来，也看不出该用哪份。

2026-11-17 给 `sync_repo.py` 加了 `ASSET_GLOBS` 白名单，把「运行必需」的资产
纳入同步：`*.base.json` / `selflift.global.json` / `remake_prompt.txt` /
`segments.selflift.example*.json`。**用白名单而不是整个 `assets/`** —— 那个目录里
混着 mp4、`*.prompts.html`、`*.state.json`、`*.bak*`，都是本机产物。

### 提交时自动校验（pre-commit hook）

同步器是单向覆盖，靠人记着跑就一定会漏。所以加了 hook：**提交前自动跑
`--check`，不同步就拦下提交**。

```bash
# 每个 clone 只需启用一次（core.hooksPath 存在 .git/config，不随仓库走）
git config core.hooksPath .githooks
```

拦下时会打印该跑的命令。三种不同步状态：

| 输出 | 含义 | 处理 |
|---|---|---|
| `[DRIFT]` | 两边内容不同 | `--apply` 覆盖过去 |
| `[MISSING]` | 仓库里没有 | `--apply` 补上 |
| `[ORPHAN]` | 仓库有、活跃目录没有 | **同步器不替你删**，人工确认后 `git rm` |

`[ORPHAN]` 是 2026-11-17 才补的检测。此前只做单向覆盖，「活跃目录删了、仓库里
还留着」永远发现不了 —— `gen169.py` / `gen_neon_clean.py` / `gen_scenes_169.py` /
`gen_walk_169.py` 四个零引用脚本就是这么在仓库里躺了很久，活跃目录磁盘上压根没有。

本次提交与 skill 无关时可跳过：`git commit --no-verify`。
活跃目录不存在时（别的机器 clone）hook 自动放行。

```
# 改完活跃目录后，同步并校验
python skill/scripts/sync_repo.py --apply
python skill/scripts/sync_repo.py --check    # 应输出 ALL SYNCED
```

**注意活跃目录不是本仓库的 checkout**，它是个独立的本地 git 仓库（分支 `master`，
历史与 `main` 无 merge-base）。在那里 `git commit` **永远进不了 `main`**，同步只
靠上面的 `sync_repo.py` 搬文件。它原先挂过指向本仓库的 `origin`、推 `master` 分支，
但 `origin/master` 与 `main/skill/` 32 个文件逐字节零差异，纯冗余 —— 2026-11-17 已
`git remote remove origin`，从根上杜绝「push 成功 = 已同步」的错觉。

### 三条纪律

1. **改配置只改活跃目录**（`.workbuddy/skills/h3-long-video/assets/`）。运行时读的是
   那一份；仓库里的 `skill/assets/` 是入库副本，改它不影响正在跑的片子。
   反过来，`load_global_cfg()` 有回退：活跃目录缺失时才读本仓库这份。
2. **改工作流要另存新文件名**，别覆盖同名文件。ComfyUI 界面里打开过的同名 json，
   保存会静默覆盖磁盘版本。
3. **`selflift.global.json` 的 `workflow` 字段是本机绝对路径**（整个仓库都是
   `D:/Comfy-Desktop/...` 硬编码，属本机专用工程）。换机器要连带改这一处，
   否则全局约束会指向不存在的文件。

---

## 单端口合并：`tools/remake_bridge.py`

### 为什么需要这一层

`remake_panel.py` 是一个纯标准库 `http.server` 服务，它的 `H.do_GET / do_POST`
里写死了 `self.send_response / send_header / end_headers / wfile.write / headers /
rfile.read / path`。而工作台跑在 aiohttp 上。

两个事实让合并变得很便宜：

1. **路由命名空间不冲突** —— 复刻台全走 `/api/*`，出片台全走 `/h3-long-video/*`，
   一条都不重叠。
2. **`H` 对 socket 的依赖极小** —— 只用上面那 7 个成员，不读 `client_address`、
   不读 `request_version`、不自己写 chunked 编码。

所以 `remake_bridge.py` 做了一个**伪 handler**：把 aiohttp 的 `request` 适配成
`H` 期望的形状，把 `H` 输出的字节收进内存再交给 aiohttp 返回。业务代码一行没改。

### 三条铁律（改 `remake_panel.py` 前必读）

- **不调 `H.__init__`** —— 那是 `BaseHTTPRequestHandler`，构造时会去读 socket。
  bridge 直接 `panel.H.do_GET(h)`，传自己的实例。
- **`H` 里不能引用 `_FakeHandler` 之外的实例状态** —— 只允许 `path / headers /
  rfile / wfile / send_response / send_header / end_headers / _body`。新增 handler
  方法时一旦碰了别的属性（比如 `self.server`），合并模式下会 `AttributeError`。
- **大文件不进伪 handler** —— 成片、源视频几十上百 MB，攒内存会炸。`/api/media`
  和 `/api/src-video` 单独用 `web.FileResponse` 实现（自带 Range，时间轴 seek 靠它），
  而且**必须注册在通配 `/api/{tail:.*}` 之前** —— aiohttp 按注册顺序匹配。

### 两个容易踩的细节

- **`parse_qs` 不是 `MultiDict`**：`H` 里 `q.get("film")[0]`，aiohttp 的
  `request.query.get()` 直接返回字符串。照抄 `[0]` 会把 `复刻_1117` 截成 `复`。
  `_resolve_media()` 统一走 `parse_qs(request.query_string)`。
- **`/remake/api/x` 别名**：`H` 自己 `urlparse(self.path)`，所以 `_dispatch` 里
  先把 `/remake` 前缀剥掉再传。

### 独立调试

只想调复刻台时，加 `--standalone` 单开（此时用 8901）：

```bash
python remake_panel.py --standalone
```

不加参数直接跑只会打印提示并退出 —— 防止有人起了两个服务、改了一个发现没生效。

---

## 两条出片链路的区别

| | 复刻台（黑鹤二采） | 出片台（SelfLift） |
|---|---|---|
| 工作流 | `H3-SelfLift_黑鹤二采.base.json` | `H3-SelfLift_单图参考.base.json` |
| 首帧承接 | 核心 `MiniMaxH3AddGuide` 在 ref2va conditioning 追加 `frame_idx=0` | 尾帧图直接当下一段首帧输入 |
| 一采 / 二采 | 固定 0.2MP / 2× | 按片配置 |
| 接缝 | MAE ≈ 19.8（软参考，非像素硬锚） | MAE ≈ 2.5（像素硬锚） |
| 适用 | 复刻已有原片的分镜 | 从零写分镜 |

**别在同一片里混用两条链路** —— merge 时统一缩放，而且会让全片 `stale`。

模型侧的能力边界（没有 face embedding / ID adapter、`<Picture N>` 标签规则、
音频四模式、24fps 帧数对齐）见 `docs/模型手册/`。

---

## 出片纪律

- **`all` 只出段不拼接**。跑完全片必须再点 `merge`，否则 `final/` 里还是旧版。
- **`seed 100000` 是坏种子**，别拿它做基准。对照实验至少两个随机 seed。
- **改磁盘上的工作流要另存新文件名** —— 在 ComfyUI 界面里打开过的同名 json，
  保存时会静默覆盖磁盘版本。
- **交付「工作流」= 能拖进 ComfyUI 的 json**，参数按 8GB 显存调过。
- **删文件走回收站**。仓库内的临时备份统一进 `_trash/`（已 gitignore），保留相对
  路径结构，随时可以挪回来。

---

## 文档索引

写分镜、写提示词、调参数之前，先读对应那一份 —— 这些文档之间有强依赖，
跳过会写出跑不通的东西。

| 文档 | 谁该读 | 内容 |
|---|---|---|
| [`docs/segment编写规范.md`](docs/segment编写规范.md) | 写分镜的人 / 模型 | **§十三 = 电视剧式蒙太奇全规范**：180° 机位、镜头内调度、起幅收幅、6cut+1dissolve、4 节拍、执行力白名单、链路 A/B |
| [`docs/导演工作台配置与分段提示词规范.md`](docs/导演工作台配置与分段提示词规范.md) | 给「其他大模型」看的 | 内容层（片名 / 故事 / 分段正文）的产出格式，以及**不要碰约束层** |
| [`docs/视频生成提示词规范.md`](docs/视频生成提示词规范.md) | 维护者 | `head` / `tail` 全片唯一公用约束怎么写、怎么优化 |
| [`docs/参数配置.md`](docs/参数配置.md) | 调参的人 | 四层配置（节点控件 / 故事板 JSON / `config.json` / `advanced_overrides`）的优先级 |
| [`docs/模型手册/`](docs/模型手册/) | 维护者 | 模型提示词与配置手册、采样器组合指南、H3 提示词案例库 |
| [`skill/SKILL.md`](skill/SKILL.md) | agent | 流水线作业手册：三层结构、三道人工确认点、失败码与修法 |
| [`docs/工作台服务架构.md`](docs/工作台服务架构.md) | 改面板的人 | 单端口三入口、资产双份、同步机制 |
| [`docs/experiments/`](docs/experiments/) | 维护者 | 未验证的探索性规范（**不要当定论用**） |

分镜体检：

```bash
python selflift_lint.py --cfg output/<片名>/segments.json
```

---

## 测试

```bash
<ComfyUI venv>/python.exe -m pytest tests/ -q
```

`tests/test_graph.py` 校验 Director 工作流图结构，`tests/test_selflift_panel.py`
覆盖出片台的分段与状态推导。

---

## 常见故障

| 症状 | 原因 | 修法 |
|---|---|---|
| `/health` 里 `remake: false` | 复刻台挂载失败 | 看 `remake_error` 和启动时的 traceback；出片台仍可用 |
| 页面 404 但服务在跑 | 只起了 `remake_panel.py` 没走 `serve_app.py` | 用 `tools/serve_app.bat` |
| 时间轴拖不动 | `FileResponse` 注册在通配路由之后了 | 见上文「三条铁律」 |
| `/api/media?film=中文` 报 400 | Git Bash 下 curl 的中文 query 编码问题 | 用 Python `urlencode` 验，不是服务故障 |
| 改完 `director.py` / `selflift_panel.py` 没生效 | 桌面版 ComfyUI 内嵌服务 | 重启 ComfyUI（8188）或独立服务（8900） |
| 全片都标 `stale` | ComfyUI 重启刷新了 `*_updated_at` | 校正到最早一段 mp4 的 mtime 之前 |
