# 配置参考

故事板中每个字段的完整说明见：

`docs/STORYBOARD_JSON.zh-CN.md`

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
| `megapixels` | 一采基础生成像素量 | `0.2` | 覆盖 `megapixels` |
| `final_width` | 最终视频宽度 | `704` | 覆盖 `final_width` |
| `final_height` | 最终视频高度 | `1216` | 覆盖 `final_height` |
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
      "prompt": "这一段的动作和运动提示词",
      "end_frame_prompt": "这一段结束时静态画面的描述",
      "start_frame_prompt": "新分镜开始时的静态画面",
      "hold_composition": false,
      "continue_from_previous": true,
      "transition_seconds": 1.0,
      "transition_prompt": "从上一镜头自然过渡到新镜头"
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
| `end_frame_prompt` | 字符串 | 否 | 用于生成该段结束关键帧；省略时使用 `prompt` |
| `start_frame_prompt` | 字符串 | 否 | 分镜断开时用于生成独立开始关键帧；省略时使用 `prompt` |
| `hold_composition` | 布尔值 | 否 | `true` 不生成新结束帧，直接使用开始帧；`false` 生成独立结束关键帧 |
| `continue_from_previous` | 布尔值 | 否 | 覆盖全局 `chain_segments`；`false` 表示启用分镜断开 |
| `transition_seconds` | 数字 | 否 | 分镜断开时自动生成转场视频的时长 |
| `transition_prompt` | 字符串 | 否 | 分镜转场的动作、镜头和声音提示词 |

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
Picture 1（参考图1）是<女主角>的主要身份、脸型、五官、发型和身高比例基准。
只有剧情已经换好最终服装后才参考它的服装；试穿和换装阶段严格跟随每段提示词。
Picture 2（参考图2）只用于稳定中性表情、眼型、鼻型、脸型和闭嘴嘴宽，不复制其白色上衣和背景。
Picture 3（参考图3）只用于参考裙摆和鞋子细节，不覆盖图1的人物身份，不复制图3的表情。

所有段落必须保持与上述图片一致的<女主角>，严格参考脸型、黑色长发和身体比例。
不得混入其他人物的脸、发型或年龄感；衣服随剧情变化，不能把同一套衣服强加到所有段落。
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
| `keyframe_draft_seconds` | `2.0` | 生成结束关键帧时使用的 H3 短期草案长度 |
| `keyframe_megapixels` | `0.6` | 关键帧单独的生成像素量；提高可改善脸部和服装清晰度 |
| `keyframe_steps` | `6` | 关键帧单独的采样步数 |
| `keyframe_ref_image_size` | `max` | 关键帧参考图使用较高分辨率身份参考，提高五官细节 |
| `chain_segments` | `true` | 全局默认是否把上一段结束帧作为下一段开始帧 |
| `transition_default_seconds` | `1.0` | 分镜断开时转场视频的默认时长 |
| `transition_two_pass` | `false` | 是否对自动生成的转场视频执行二采 |
| `stabilize_full_body` | `true` | 作为段尾锚定和首段自动构图草案的总默认值；段内可用 `hold_composition` 覆盖 |

`hold_composition` 的优先级高于全局 `stabilize_full_body`。换衣服、上车、下车、换座位等会产生真实状态变化的段落应设为 `false`；纯站姿、短距离行走等需要锁定构图且不改变状态的段落可设为 `true`。

`hold_composition=true` 时：

```text
本段开始关键帧 → 本段 first_frame
本段开始关键帧 → 本段 last_frame
```

不生成新的结束关键帧，首尾被同一张图约束。适合站定、稳定构图镜头，不适合换衣服或明显改变位置。

`hold_composition=false` 时：

```text
本段开始关键帧 → 本段 first_frame
end_frame_prompt → 新结束关键帧 → 本段 last_frame
```

先生成独立的结束关键帧，再用两张不同的图约束完整视频，允许换装、上车、下车和场景状态持续推进。

### 分镜断开与转场

默认 `chain_segments=true`，上一段结束关键帧会直接作为下一段开始关键帧。

当某段需要新的镜头角度、地点或时间状态时，在该段填写：

```json
{
  "continue_from_previous": false,
  "transition_seconds": 1.0,
  "start_frame_prompt": "新镜头开始时的静态画面",
  "transition_prompt": "从上一镜头平滑转入新镜头"
}
```

此时流程会：

1. 不使用上一段尾帧作为新段首帧。
2. 根据 `start_frame_prompt` 和参考图生成独立开始关键帧。
3. 生成一段从上一段结束帧到新段开始帧的 H3 转场视频。
4. 输出顺序为“上一段视频 → 转场 → 新段视频”。

分镜断开处会额外增加 `transition_seconds`。最终视频总时长等于所有段时长加上所有转场时长。

第一段开始关键帧：

```text
第一段提示词 + 参考图
→ H3 短草案
→ 提取草案第一帧
```

不会把参考图文件直接作为第一段的固定首帧。

之后按段链式生成结束关键帧：

```text
开始关键帧 + 本段 end_frame_prompt
→ H3 短草案
→ 提取草案最后一帧作为结束关键帧
→ 下一段直接复用该结束关键帧作为开始帧
```

全部关键帧完成后才生成正式视频：

```text
开始关键帧 → H3 first_frame
结束关键帧 → H3 last_frame
→ 生成正式段视频
→ 按需执行二采
```

第一段如果没有单独填写不同的 `end_frame_prompt`，会直接使用开始关键帧草案
中已经生成的最后一帧作为第一段结束帧，不会为第一段再提交一次结束帧草案。
只有第一段填写了独立结束描述时，才会额外生成第二个关键帧草案。

关键帧默认使用 `keyframe_megapixels=0.6`、`keyframe_steps=6` 和
`keyframe_ref_image_size=max`，正式视频仍使用较轻量的 `megapixels=0.2`。
这样只在关键帧阶段增加计算，避免每一段视频一采都变慢。

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

