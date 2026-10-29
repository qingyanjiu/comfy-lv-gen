# 配置参考

本文档解释仓库中所有用户可配置项。配置文件分为四层：

1. ComfyUI Director 节点控件
2. Director 节点故事板 JSON
3. 仓库根目录 `config.json`
4. `advanced_overrides` 本次任务覆盖 JSON

## Director 节点控件

| 控件 | 作用 | 默认值 | 何时覆盖其他配置 |
|---|---|---:|---|
| `storyboard` | 填写每段提示词、秒数和局部二采设置 | 当前示例故事板 | 控制每段内容 |
| `reference_notes` | 说明每张参考图负责身份、脸型、发型、服装等哪些特征 | 图1/图2说明模板 | 自动加到每一段提示词最前面 |
| `steps` | 一采步数 | `6` | 覆盖全局和高级覆盖里的 `steps` |
| `two_pass_steps` | 二采步数 | `5` | 覆盖全局和高级覆盖里的 `two_pass_steps` |
| `sampler` | 一采和二采共用的采样器 | `res_multistep` | 覆盖 `sampler` |
| `scheduler` | 一采和二采共用的调度器 | `simple` | 同时覆盖 `scheduler` 和 `two_pass_scheduler` |
| `two_pass_mode` | 全局二采模式 | `跟随每段 JSON` | 可强制所有段一采或二采 |
| `force` | CLI 缓存强制重跑开关 | `false` | Director 每个任务目录都是新的，通常无效 |
| `reference_1` | 必选参考图 | 无 | 必须连接 IMAGE |
| `reference_2` 到 `reference_9` | 可选参考图 | 未连接 | 每张都是 IMAGE，最多共 9 张 |
| `advanced_overrides` | 本次任务的高级 JSON 覆盖 | `{}` | 节点专用采样控件随后覆盖同名值 |

## 故事板 JSON

`storyboard` 接受以下形式：

```json
{
  "segments": [
    {
      "seconds": 5.0,
      "two_pass": false,
      "prompt": "这一段的提示词"
    }
  ]
}
```

也可以直接传数组，但推荐保留 `segments` 外层对象。

| 字段 | 类型 | 必填 | 作用 |
|---|---|---:|---|
| `seconds` | 数字，必须大于 0 | 是 | 该段视频时长。实际输出帧数按 24 fps 计算并截断 |
| `two_pass` | 布尔值 | 否，默认 `false` | `false` 一采，`true` 二采；最终还受全局 `two_pass_mode` 控制 |
| `prompt` | 字符串 | 是，不能为空 | 该段独立的动作、场景、镜头和声音描述 |

画面有 N 个 `segments` 元素，就会自动循环生成 N 段，不要求固定段数。

`two_pass_mode` 的优先级高于每段 `two_pass`：

| 模式 | 结果 |
|---|---|
| `跟随每段 JSON` | 使用每段的 `two_pass` |
| `全部一采` | 所有段强制 `false` |
| `全部二采` | 所有段强制 `true` |

## 参考图说明 reference_notes

参考图会作为 IMAGE 传给 H3，但“图1代表什么、图2代表什么”需要写在 `reference_notes`。这段文字会自动加在每一段 `prompt` 前面，例如：

```text
Picture 1（参考图1）是主要人物身份、脸型、五官、发型、身高比例和最终服装基准。
Picture 2（参考图2）只用于稳定中性表情、眼型、鼻型、脸型和闭嘴嘴宽，不复制其白色上衣和背景。
Picture 3（参考图3）只用于参考裙摆和鞋子细节，不覆盖图1的人物身份，不复制图3的表情。

所有段落必须使用同一位成年女性，严格参考上述图片中的脸型、黑色长发和身体比例。
不得混入其他人物的脸、发型、年龄感或服装。
```

如果连接了 3 张以上图片，必须继续写清楚 `Picture 3`、`Picture 4` 各自负责什么。没有用途的图片不要连接，因为互相冲突的参考图会让 H3 混合不同人物的特征。

连接顺序会影响 `Picture N` 的编号：

```text
reference_1 → Picture 1 → ref_image_0
reference_2 → Picture 2 → ref_image_1
reference_3 → Picture 3 → ref_image_2
```

建议参考图组合：

1. 一张清晰的主要身份图，最好能看清脸、发型或服装。
2. 一张清晰的正脸中性表情图，用于稳定五官和闭嘴嘴宽。
3. 其他图片只承担单一用途，例如侧面、背面、鞋袜或特定服装细节。

不要同时连接多张来自不同人物、不同年龄段或明显不同服装的图片。

参考图约束是生成式引导，不是像素级换脸。如果人物仍然不像，应优先：

- 减少相互冲突的参考图
- 把最可靠的身份图放在 `reference_1`
- 在 `reference_notes` 中逐图说明用途
- 删除参考图里不需要被复制的背景和服装描述
- 将 `ref_image_size` 在 `advanced_overrides` 中改为 `max`

## 全局 config.json

ComfyUI Director 每次启动任务时读取仓库根目录的 `config.json`，然后在任务目录 `jobs/<job_id>/config.json` 保存本次实际配置。

### 连接和目录

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `comfy_url` | `http://127.0.0.1:8188` | 本地 ComfyUI HTTP API 地址；不是 MiniMax 云 API |
| `comfy_input_dir` | `D:/Comfy-Desktop/ComfyUI-Shared/input` | CLI 模式下相对参考图文件的查找目录，也是默认参考图位置 |
| `chain_input_dir` | `ComfyUI/input` | 历史兼容路径；当前接尾帧使用绝对路径，不依赖复制到此目录 |
| `comfy_output_dir` | `D:/Comfy-Desktop/ComfyUI-Shared/output` | ComfyUI 分段视频和尾帧所在输出根目录 |
| `final_output_dir` | `ComfyUI/output` | 完整成片和自动二采修复文件的输出根目录 |
| `output_prefix` | `h3_long_video` | 中间文件子目录名，也决定最终目录名 |

最终文件固定为：

```text
<final_output_dir>/<output_prefix>/long_video.mp4
```

### 画幅和长度

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `aspect_ratio` | `9:16 (Portrait Widescreen)` | H3 的分辨率选择预设，必须保持竖屏 9:16 |
| `megapixels` | `0.2` | 一采基础像素量；会按 32 像素网格对齐 |
| `fps` | `24` | 当前代码只允许 24 fps |
| `final_width` | `704` | 最终成片宽度 |
| `final_height` | `1216` | 最终成片高度 |

当前 `9:16` 预设经过 32 像素对齐后，一采实际尺寸为 `352×608`，二采和最终成片为 `704×1216`。

每段请求时长会换算为帧数，并向上对齐到 H3 要求的 `帧数 % 17 == 5`。输出仍会裁剪为该段配置的精确秒数，避免编码多出几十毫秒。

### 一采

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `steps` | `6` | 一采采样步数；Director 的 `steps` 控件优先 |
| `sampler` | `res_multistep` | 一采和二采使用的采样器；Director 的 `sampler` 控件优先 |
| `scheduler` | `simple` | 一采调度器；Director 的 `scheduler` 控件优先 |
| `base_seed` | `795140483442838` | 基础种子，每段按索引递增；生成首帧草稿时额外加 `900000` |

### 二采

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `two_pass_default` | `false` | CLI 配置中某段省略 `two_pass` 时的默认值 |
| `two_pass_steps` | `5` | 二采步数；Director 控件优先 |
| `two_pass_denoise` | `0.2` | 二采重绘幅度，越大变化越明显 |
| `two_pass_scheduler` | `simple` | 二采调度器；Director 中会与 `scheduler` 同步 |
| `two_pass_scale` | `2.0` | Latent 空间放大倍率，配合基础尺寸得到最终尺寸 |
| `two_pass_fix_opening_frames` | `2` | 二采完成后，用首帧锚点替换的放大器异常帧数 |
| `latent_upscaler_name` | `minimax_h3_latent_upscaler_3d_fp16.safetensors` | 3D Latent 放大模型 |

二采时间分块在代码中是默认开启的，不受故事板字段控制：

- `enable_temporal_chunking = true`
- `chunk_length = 153`
- `temporal_overlap = 17`
- `anchor_strength = 0.99`
- `align = 32`
- `precision = fp16`
- `device = cuda`

二采实际路径：

```text
一采 AV Latent
→ 拆分 video/audio latent
→ 2x Latent Upscale
→ 合并原音轨
→ MMH3 Ultimate Upscale
→ 时间分块重采样
```

### 模型

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `unet_name` | `DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors` | MiniMax H3 UNET |
| `clip_name` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | Qwen3-VL 文本/图像编码器 |
| `video_vae_name` | `minimax_h3_video_vae_fp16.safetensors` | 视频 VAE |
| `audio_vae_name` | `minimax_h3_audio_vae_fp32.safetensors` | 音频 VAE |
| `lora_name` | `TaoMate-H3-3step.safetensors` | H3 LoRA |
| `lora_strength` | `1.0` | LoRA 强度 |
| `ref_image_size` | `match` | H3 参考图尺寸模式；可选 `match` 或 `max` |

模型文件名必须与 ComfyUI 的模型列表一致。仓库不包含模型文件。

### 参考图、首帧和构图

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `reference_images` | `["image.png", "image (5).png"]` | CLI 模式的参考图数组，最多 9 张 |
| `opening_frame` | 可选，不存在时自动生成 | 显式指定第一段构图锚点；正常使用建议删除此项 |
| `opening_draft_seconds` | `2.0` | 自动生成第一段构图首帧的 H3 草稿时长 |
| `stabilize_full_body` | `true` | 将段首帧同时作为尾帧锚点，减少镜头自行推进和人物变成半身 |

第一段：

```text
提示词 + 参考图
→ 生成短草案
→ 提取草案第一帧作为新构图锚点
→ 生成正式第一段
```

不会把参考图文件直接作为第一段的固定首帧。

第二段开始：

```text
上一段实际保存尾帧 = 下一段 first_frame
上一段实际保存尾帧 = 下一段 last_frame（stabilize_full_body=true 时）
```

参考图最多 9 张，全部传递到 H3 的 `ref_images.ref_image_0..8`。

### 段落

`config.json` 中的 `segments` 与故事板格式相同：

```json
{
  "seconds": 5.0,
  "two_pass": false,
  "prompt": "提示词"
}
```

ComfyUI Director 会使用节点当前故事板，并覆盖任务副本中的这个数组。

## advanced_overrides

正常保持：

```json
{}
```

示例：

```json
{
  "two_pass_denoise": 0.2,
  "two_pass_scale": 2.0,
  "megapixels": 0.2,
  "final_width": 704,
  "final_height": 1216,
  "lora_strength": 1.0,
  "stabilize_full_body": true
}
```

覆盖优先级：

```text
读取 config.json
→ 应用 advanced_overrides
→ 应用故事板和参考图
→ 应用节点 steps/two_pass_steps/sampler/scheduler
→ 应用全局 two_pass_mode
```

因此，`advanced_overrides` 不能覆盖：

- `segments`
- `reference_images`

在其中写 `steps`、`two_pass_steps`、`sampler`、`scheduler` 也不会生效，因为节点上的专用控件最后应用。

## 采样器和调度器选项

采样器：

```text
euler, euler_cfg_pp, euler_ancestral, euler_ancestral_cfg_pp,
heun, heunpp2, exp_heun_2_x0, exp_heun_2_x0_sde,
dpm_2, dpm_2_ancestral, lms, dpm_fast, dpm_adaptive,
dpmpp_2s_ancestral, dpmpp_2s_ancestral_cfg_pp,
dpmpp_sde, dpmpp_sde_gpu, dpmpp_2m, dpmpp_2m_cfg_pp,
dpmpp_2m_sde, dpmpp_2m_sde_gpu, dpmpp_2m_sde_heun,
dpmpp_2m_sde_heun_gpu, dpmpp_3m_sde, dpmpp_3m_sde_gpu,
ddpm, lcm, ipndm, ipndm_v, deis, cfgpp_ud10_ab,
res_multistep, res_multistep_cfg_pp,
res_multistep_ancestral, res_multistep_ancestral_cfg_pp,
gradient_estimation, gradient_estimation_cfg_pp, er_sde,
seeds_2, seeds_3, sa_solver, sa_solver_pece, ddim,
uni_pc, uni_pc_bh2
```

调度器：

```text
simple, sgm_uniform, karras, exponential, ddim_uniform,
beta, normal, linear_quadratic, kl_optimal
```

## 状态、缓存和输出

每次在 ComfyUI Director 中运行都会创建：

```text
jobs/<job_id>/
├── config.json
├── status.json
├── run.log
├── reference_*.png
└── .state.json
```

`status.json` 状态：

- `scheduled`
- `running`
- `completed`
- `failed`

CLI 模式使用 `.state.json` 判断复用。只有以下内容都匹配时才复用旧段：

- 提示词
- 时长
- 二采开关
- 模型、参考图路径、分辨率、采样器等全局参数
- 上一段结果 ID

修改任意前序段后，后续旧段自动失效并重跑。

分段视频和尾帧输出到 `comfy_output_dir/output_prefix/`，完整成片输出到：

```text
<final_output_dir>/<output_prefix>/long_video.mp4
```

## CLI 参数

| 参数 | 作用 |
|---|---|
| `--config PATH` | 使用指定配置文件 |
| `--limit N` | 本次最多补跑 N 个缺失段 |
| `--no-stitch` | 不合成完整视频 |
| `--force` | 忽略缓存并重跑全部段 |
| `--dry-run` | 只生成第一段 API 图，不提交 |
| `--dump-graphs DIR` | 输出一采和二采 API 图示例 |
| `--anchor-image PATH` | 生成二采 API 图示例时使用的锚点图 |

## 固定限制

- 只接受 24 fps。
- `aspect_ratio` 必须包含 `9:16`。
- `final_width < final_height`，并保持竖屏比例。
- 参考图至少 1 张，最多 9 张。
- 每段时长必须大于 0。
- 每段提示词不能为空。
- 完整链未跑完时不会生成误导性的最终视频。

