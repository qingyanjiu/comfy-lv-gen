# 故事板 JSON 字段说明

本文档只说明 Director 节点 `storyboard` 输入框中的 JSON。参考图片、模型、
采样器、分辨率等配置由节点控件或 `config.json` 管理，不要写进这里。

## 1. 顶层结构

推荐格式：

```json
{
  "segments": [
    {
      "seconds": 5,
      "two_pass": false,
      "prompt": "第一段动作提示词"
    }
  ]
}
```

`segments` 是必需的非空数组。数组里有多少个对象，就自动生成多少段视频。

也允许直接传数组：

```json
[
  {
    "seconds": 5,
    "two_pass": false,
    "prompt": "第一段动作提示词"
  }
]
```

为了可读性和后续扩展，建议始终保留 `segments` 外层对象。

## 2. 字段总览

| 字段 | 类型 | 必填 | 默认行为 |
|---|---|---:|---|
| `seconds` | 数字 | 是 | 无默认值，必须大于 0 |
| `prompt` | 字符串 | 是 | 无默认值，不能为空 |
| `end_frame_prompt` | 字符串 | 否 | 使用 `prompt` |
| `start_frame_prompt` | 字符串 | 否 | 使用 `prompt` |
| `two_pass` | 布尔值 | 否 | 默认 `false`，并受全局二采模式影响 |
| `hold_composition` | 布尔值 | 否 | 跟随全局 `stabilize_full_body` |
| `continue_from_previous` | 布尔值 | 否 | 跟随全局 `chain_segments` |
| `transition_seconds` | 数字 | 否 | 跟随全局 `transition_default_seconds` |
| `transition_prompt` | 字符串 | 否 | 使用内置的分镜转场提示 |

## 3. seconds

```json
{
  "seconds": 5.0
}
```

作用：该段正式视频的目标时长。

规则：

- 必须是大于 0 的数字。
- 单位是秒，不是毫秒或帧数。
- 输出帧数按 24 fps 计算并裁剪到该时长。
- H3 内部帧数会自动向上对齐到 `% 17 == 5`。
- 例：`5` 表示约 5 秒，不是 5 帧。

常见错误：

```json
"seconds": "5s"
"seconds": "5000ms"
"seconds": 0
```

以上都是无效或错误的写法。

## 4. prompt

```json
{
  "prompt": "<女主角>从宿舍门口走向走廊，步伐自然，摄影机平稳跟随"
}
```

作用：正式视频中的动作、表演、镜头运动、环境声音和禁止项。

规则：

- 必填，不能是空字符串。
- 描述“从本段开始状态变化到结束状态”的连续运动。
- 不需要重复参考图身份说明；`reference_notes` 会自动放在它前面。
- 不需要重复上一段已经明确说明的服装，除非本段发生换装。

`prompt` 不是结束关键帧的静态描述。如果当前段结束时有明确的目标状态，
应额外填写 `end_frame_prompt`。

## 5. start_frame_prompt

```json
{
  "start_frame_prompt": "<女主角>已经站在商业街入口，9:16完整全身镜头，正面看向街道"
}
```

作用：当本段与上一段断开时，用于生成独立的新镜头开始关键帧。

规则：

- 可选字段。
- 省略时使用本段 `prompt`。
- 只在以下情况生效：
  - 本段 `continue_from_previous: false`
  - 或全局 `chain_segments: false`
- 如果本段继续上一段，不会生成新的开始帧，此字段不生效。

建议描述稳定的静态构图，而不是复杂运动。

## 6. end_frame_prompt

```json
{
  "end_frame_prompt": "<女主角>已经走到公交站牌旁并停稳，9:16完整全身镜头"
}
```

作用：生成本段结束关键帧。

规则：

- 可选字段。
- 省略时使用本段 `prompt`。
- 如果该字段与 `prompt` 相同，系统会尽量复用生成开始帧时同一份关键帧草案
  的最后一帧，减少一次生成。
- 如果填写了与 `prompt` 不同的独立描述，会单独生成结束关键帧。
- 如果 `hold_composition: true`，结束帧直接使用开始帧，此字段不生效。

建议描述：

- 人物最终位置
- 最终朝向
- 镜头景别
- 服装最终状态
- 表情和手部位置
- 背景关键物体

不要在这里写很长的连续运动。

## 7. two_pass

```json
{
  "two_pass": false
}
```

作用：控制该段视频是否执行二采。

- `false`：只做正式视频一采。
- `true`：一采完成后再执行 Latent 放大和 MMH3 二采。
- 必须是布尔值，不能写成字符串 `"false"` 或 `"true"`。

最终结果还受 Director 的 `two_pass_mode` 控制：

| Director 模式 | 最终结果 |
|---|---|
| `跟随每段 JSON` | 使用该段的 `two_pass` |
| `全部一采` | 所有段强制 `false` |
| `全部二采` | 所有段强制 `true` |

## 8. hold_composition

```json
{
  "hold_composition": false
}
```

作用：是否让本段开始帧和结束帧相同。

### true

```text
开始关键帧 → first_frame
开始关键帧 → last_frame
```

- 不单独生成结束关键帧。
- 构图最稳定。
- 适合站定、短距离走动、轻微摇摆。
- 不适合换装、上下车、明显转身或位置变化。

### false

```text
开始关键帧 → first_frame
新生成的结束关键帧 → last_frame
```

- 允许本段状态向前推进。
- 适合换装、进门、上车、下车、场景变化。
- 消耗更多关键帧生成时间。

省略该字段时，使用全局 `stabilize_full_body`。

## 9. continue_from_previous

```json
{
  "continue_from_previous": false
}
```

作用：决定本段是否直接使用上一段结束帧作为开始帧。

### true

```text
上一段结束关键帧 → 本段开始关键帧
```

- 不生成独立开始帧。
- 不生成转场。
- 两段动作直接连续。

### false

```text
独立生成本段开始关键帧
上一段结束帧 → 淡出淡入转场 → 本段开始帧
```

- 本段是新分镜，不再强制首尾相连。
- 默认自动添加 FFmpeg 淡出淡入；只有 `transition_mode=generated` 才生成 H3 转场视频。
- 使用 `start_frame_prompt` 描述新分镜开始画面。

省略该字段时，使用全局 `chain_segments`。

第一个分镜没有上一段，因此该字段对第一段没有实际影响。

## 10. transition_seconds

```json
{
  "transition_seconds": 1.0
}
```

作用：`continue_from_previous: false` 时，分镜转场的时长。

- 单位是秒。
- 只在当前段启用分镜断开时有效。
- 默认跟随全局 `transition_default_seconds`。
- 第一个分镜没有转场，可以设为 `0`。
- 第二个分镜以后如果 `continue_from_previous:false`，必须大于 `0`。
- 默认 `transition_mode=fade`：时长是边界两侧淡出和淡入的总时长，不增加总时长。
- `transition_mode=generated`：会生成额外 H3 转场视频，并增加成片总时长。

例如：

```text
默认 fade 模式：12段 × 5秒 = 60秒，转场复用相邻片段边界，不增加时长。
```

## 11. transition_prompt

```json
{
  "transition_prompt": "从走廊镜头平滑转为宿舍楼外的新镜头，人物和动作自然衔接"
}
```

作用：`transition_mode=generated` 时，描述“上一段结束帧 → 本段新开始帧”之间的转场过程。

规则：

- 只在 `continue_from_previous: false` 且 `transition_mode=generated` 时有效。
- 省略时使用内置的电影感平滑转场提示。
- 建议说明空间、光线、镜头和动作如何变化。
- 不要写黑场、闪白或硬切，除非确实需要。

## 12. 字段组合示例

### 连续镜头

```json
{
  "seconds": 5,
  "two_pass": false,
  "hold_composition": true,
  "continue_from_previous": true,
  "prompt": "<女主角>沿人行道继续向前走，摄影机保持全身景别"
}
```

结果：

```text
上一段结束帧 → 本段开始帧
没有新开始帧
没有转场
```

### 有状态变化的连续镜头

```json
{
  "seconds": 5,
  "two_pass": false,
  "hold_composition": false,
  "continue_from_previous": true,
  "prompt": "<女主角>从宿舍门边走到走廊尽头",
  "end_frame_prompt": "<女主角>在走廊尽头停稳，9:16全身镜头"
}
```

结果：

```text
上一段结束帧 → 本段开始帧
生成新的本段结束帧
没有转场
下一段可继续使用本段结束帧
```

### 分镜切换

```json
{
  "seconds": 5,
  "two_pass": false,
  "hold_composition": false,
  "continue_from_previous": false,
  "start_frame_prompt": "<女主角>已经站在商业街入口，9:16全身镜头",
  "transition_seconds": 1.0,
  "transition_prompt": "从公交站镜头平滑转入商业街新镜头",
  "prompt": "<女主角>沿商业街向前逛",
  "end_frame_prompt": "<女主角>在店铺橱窗前停稳"
}
```

结果：

```text
独立生成新分镜开始帧
上一段结束帧 → 淡出淡入转场 → 新分镜
新分镜结束帧可继续供下一段使用
```

## 13. 完整 JSON 示例

```json
{
  "segments": [
    {
      "seconds": 5,
      "two_pass": false,
      "hold_composition": false,
      "prompt": "<女主角>在宿舍里拿起衣服并走向屏风",
      "end_frame_prompt": "<女主角>走到屏风后，身体被屏风遮挡"
    },
    {
      "seconds": 5,
      "two_pass": false,
      "hold_composition": false,
      "continue_from_previous": true,
      "prompt": "<女主角>从屏风后走出并在镜前整理衣服",
      "end_frame_prompt": "<女主角>在全身镜前停稳"
    },
    {
      "seconds": 5,
      "two_pass": false,
      "hold_composition": false,
      "continue_from_previous": false,
      "start_frame_prompt": "<女主角>已经站在宿舍楼外，9:16完整全身镜头",
      "transition_seconds": 1.0,
      "transition_prompt": "从宿舍内部平滑转向宿舍楼外的新镜头",
      "prompt": "<女主角>沿校园道路走向公交站",
      "end_frame_prompt": "<女主角>在公交站牌旁停稳"
    }
  ]
}
```

## 14. 不要放进故事板 JSON 的配置

以下内容不属于 `segments`：

| 配置 | 正确位置 |
|---|---|
| 参考图片 | Director 的 `reference_1` 到 `reference_9` |
| 参考图用途说明 | Director 的 `reference_notes` |
| 模型、VAE、LoRA | `config.json` 或 `advanced_overrides` |
| 一采步数 | Director 的 `steps` |
| 二采步数 | Director 的 `two_pass_steps` |
| 采样器 | Director 的 `sampler` |
| 调度器 | Director 的 `scheduler` |
| 基础像素量 | Director 的 `megapixels` |
| 最终宽高 | Director 的 `final_width/final_height` |

## 15. 常见错误

### JSON 语法错误

- 最后一个字段后面多了逗号。
- 使用中文引号 `“”`。
- 少写逗号或括号。
- 在字符串里放了未转义的英文双引号。

### 类型错误

```json
"seconds": "5"
"two_pass": "false"
```

应写成：

```json
"seconds": 5
"two_pass": false
```

### 字段作用写错

- `prompt` 描述连续动作。
- `start_frame_prompt` 描述新分镜开始时的静态画面。
- `end_frame_prompt` 描述本段结束时的静态画面。
- `transition_prompt` 描述两个分镜之间如何过渡。

不要把这四种描述写成同一份长篇连续动作，否则关键帧模型难以稳定生成。

