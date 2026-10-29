# 完整视频参考 + 二采工作流

工作流文件：

`example_workflows/H3-FullVideo-2Pass.json`

该工作流基于当前的
`up自用二采minimax h3工作流（改自黑鹤大佬）.json` 复制并修改，不覆盖原工作流。

## 管线

```text
上传完整视频
├── 完整帧序列 → H3 ref_video_0 → <Video 1>
└── 原视频音轨 → H3 ref_video_audio_0

参考图片 → H3 ref_image_0..8 → <Picture 1..9>
提示词 → H3 条件

一采
→ 拆分音视频 Latent
→ 2 倍 Latent 放大
→ 合并原音轨
→ MMH3 Ultimate Upscale 二采
→ 时间分块
→ 视频/音频解码
→ 裁回源视频帧数
→ 输出完整 MP4
```

## 使用方法

1. 打开 `H3-FullVideo-2Pass.json`。
2. 在“上传完整视频（原片）”中选择视频。
3. `force_rate` 保持 `24`。
4. `frame_load_cap=0`，表示读取整段视频。
5. 在参考图1~3中放入人物身份、脸型和角度参考；已连接的隐藏输入支持最多9张。
6. 编辑 H3 节点的提示词，明确使用 `<Picture 1>`、`<Video 1>`。
7. 点击运行。

推荐提示词结构：

```text
<Subject 1> is the same adult woman shown in <Picture 1>, <Picture 2>,
and <Picture 3>.

<Picture 1> is the primary identity, face, hairstyle, body proportion,
and outfit reference.
<Picture 2> and <Picture 3> supplement facial identity and viewing angles.

<Video 1> is the complete source video. Preserve its motion, action timing,
camera movement, framing rhythm, and scene continuity. Replace the source
subject with <Subject 1>.

Keep identity, hairstyle, facial proportions, clothing and body proportions
consistent. Preserve natural motion. No cuts, text, subtitle, logo, or watermark.
```

## 自动处理

- `VHS_LoadVideo.frame_count` 自动驱动 H3 的 `length`。
- 帧数自动向上对齐到 H3 要求的 `% 17 == 5`。
- 最终使用 `ImageFromBatch` 裁回原视频的实际帧数。
- 二采 `MinimaxH3LatentUpscaler3D` 已启用，默认 2 倍和时间分块。
- `MMH3UltimateUpscale` 已启用。
- 二采时间分块参数：

```text
chunk_length = 153
temporal_overlap = 17
anchor_strength = 0.99
```

## 输出

默认输出前缀：

```text
H3_FullVideo_2Pass/H3_fullvideo_
```

输出节点为：

```text
完整视频二采输出
```

## 重要限制

这是“H3 根据完整视频参考重新生成”，不是把原视频逐像素放大。

- 人物动作、镜头和节奏由 `<Video 1>` 引导。
- 人物身份、脸型、发型和服装由图片参考及提示词约束。
- 输出不是原视频的像素级替换，生成式模型仍可能产生身份或动作偏差。
- H3 的常用训练范围约为 5~15 秒；节点允许最多 3600 帧，但非常长的输入会显著增加显存和耗时。
- 如果目标只是把现有视频无损或近无损放大，应使用视频超分工作流，而不是 H3 二采。

