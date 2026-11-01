# Qwen 改图（2511 与 2.1）

两条编辑线并存，**可以互相替换**：同一张原图、同一套参考图，换张工作流就行。
在 Director 面板里用「关键帧来源」这个全局配置选哪条线（见 `面板交互设计.md` D11）。
两条线在 Director 里都是**代码现搭图、自动出帧**，不依赖这里的工作流文件——
下面的 json 只用于手动调提示词和参数，运行时不读它们。

| 工作流 | 模型 | 步数 | 定位 |
| --- | --- | --- | --- |
| `example_workflows/Qwen2511-Reference-Edit.json` | Qwen-Image-Edit 2511（20B） | 8（Lightning LoRA） | **构图服从**优先：能转身、回头、看侧面；代价是脸会往「标准美型」漂 |
| `example_workflows/Qwen2511-MultiAngle-Edit.json` | 同上 + 多角度 LoRA | 8 | 手动调视角用；代码路径里不带这个角度 LoRA |
| `example_workflows/Qwen2511-FirstFrame916.json` | 同上 | 8 | 单张起帧的极简版，试提示词最快 |
| `example_workflows/Qwen21-Reference-Edit.json` | Qwen-Image-2.1（7B） | 8（LR-8step LoRA） | **身份保持**优先：又快又像，但角度跟着参考图走 |

都是扁平结构，主画布上直接能改提示词、步数、seed，没有子图。

阅读顺序：先「Qwen-Image-Edit 2511 版」，再「Qwen-Image-2.1 版」，最后三章
（角度执行力 / 身份相似度 / 两步换脸）是两代放一起测出来的结论——**只想知道该用哪个，
看这三章就够**。

## Qwen-Image-Edit 2511 版

### 2511 的两个工作流

| 文件 | 用途 |
| --- | --- |
| `example_workflows/Qwen2511-Reference-Edit.json` | 锁构图换脸（默认就是你要的“只换脸、不动角度”） |
| `example_workflows/Qwen2511-MultiAngle-Edit.json` | 换脸 + 转视角（多串了一个多角度 LoRA） |

都是扁平结构，主画布上直接能改提示词、步数、seed，没有子图。

### 先澄清版本

`Qwen-Image-2512` 确实存在，但它是**文生图**（T2I）模型，不是编辑模型。编辑线的编号是
**2509 → 2511**，官方文档把 Edit-2511 标为 “current editing recommendation”，
`Qwen/Qwen-Image-Edit-2512` 这个仓库不存在（ModelScope 直接返回 record not found）。

判别方法：打开任意号称“2512 编辑”的工作流，看 `UNETLoader` 里的文件名——
`qwen_image_2512_fp8_e4m3fn` 是文生图，`qwen_image_edit_2511_*` 才是编辑。

2511 相对 2509 的主要提升恰好对口你的需求：

- 编辑时画面漂移更少；
- 单人/多人一致性明显更强（多张人物图能融成一张合理合照）；
- 内置了一批热门社区 LoRA（含多角度类），角度控制比 2.1 好；
- 原生支持 ControlNet。

### 输入

| 输入 | 作用 |
| --- | --- |
| `image 1` | 要改的原图，决定姿势、构图、背景；输出尺寸也跟随它（自动缩放到约 100 万像素） |
| `image 2` | 身份参考，决定脸型、五官、发型 |
| `image 3` | 可选。接同一人的侧脸/半侧脸，用来给模型补“另一个角度”的信息；不接也能跑 |

提示词里用 **图1 / 图2** 指代（英文写 image 1 / image 2 也认）。正向提示词节点已经预填了一段
锁构图换身份的模板，直接改就行。

### 采样参数

默认走 8 步 Lightning LoRA（同画布同 seed 实测后改的默认）：

| 参数 | 值 |
| --- | --- |
| steps | 8 |
| cfg | 1.0 |
| sampler | euler |
| scheduler | simple |
| denoise | 1.0 |
| LoRA 权重 | 1.0 |

4 步 vs 8 步实测（608×1056、同画布同 seed，8 GB 卡）：

| 档位 | 耗时 | 表现 |
| --- | --- | --- |
| 4 步 | 39 秒 | 脸部偏糊、皮肤过度平滑，手指含糊 |
| 8 步（默认） | 63 秒 | 脸部锐利（睫毛、眼睛可辨）、手指清晰、纹理干净 |

8 步只贵 24 秒，质量提升肉眼可见；2511 后端本来就是质量档（求快选 2.1，18–24 秒），
所以默认给 8 步。想回 4 步：`config.json` 设 `"keyframe_qwen_lora": "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"`、
`"keyframe_qwen_steps": 4`（LoRA 和步数必须配套换，蒸馏 LoRA 只在自家步数上成立）。

要质量档：选中 Lightning LoRA 节点按 `Ctrl+B` 绕过，然后 steps 改 **20**、cfg 改 **4.0**。
官方参考值是 Qwen 原生 40 步 / Comfy 20 步，cfg 4.0。

### 模型

| 文件 | 大小 | 放到（本机实际路径） |
| --- | ---: | --- |
| `qwen_image_edit_2511_int8_convrot.safetensors` | 19.09 GB | `D:\Comfy-Desktop\ComfyUI-Shared\models\diffusion_models\` |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 8.74 GB | `D:\Comfy-Desktop\ComfyUI-Shared\models\text_encoders\` |
| `qwen_image_vae.safetensors` | 242 MB | `D:\Comfy-Desktop\ComfyUI-Shared\models\vae\` |
| `Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors` | 810 MB | `D:\Comfy-Desktop\ComfyUI-Shared\models\loras\`（默认 8 步用这个） |
| `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | 810 MB | 同上（求快才用，配 `keyframe_qwen_steps: 4`） |
| `Qwen-Edit-2509-Multiple-angles.safetensors` | 226 MB | 同上（多角度版本才用到） |

当前运行的 ComfyUI 已经用 `--lowvram` 启动，并映射了这个 Shared 目录（krea2、z_image 就在里面），
所以直接放进上面四个文件夹即可；`ComfyUI-Installs\ComfyUI\ComfyUI\models` 那份也会被扫描到，放哪边都行，
但别只放一半。

国内优先走 ModelScope：

- `https://modelscope.cn/models/Comfy-Org/Qwen-Image-Edit_ComfyUI`（主模型）
- `https://modelscope.cn/models/Comfy-Org/HunyuanVideo_1.5_repackaged`（文本编码器，官方就是放在这个仓库里）
- `https://modelscope.cn/models/Comfy-Org/Qwen-Image_ComfyUI`（VAE）
- `https://modelscope.cn/models/lightx2v/Qwen-Image-Edit-2511-Lightning`（4 步 / 8 步两个档位，默认用 8 步）

Hugging Face 备用路径（同一份文件）：

```text
https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_edit_2511_int8_convrot.safetensors
https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors
https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors
https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/main/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors
```

下载完**完全重启 ComfyUI**，否则模型下拉框里看不到。

### 这台机器的性能预期

RTX 5060 Ti 8 GB 显存 + 25 GB 内存，19 GB 的主模型走内存换页。

**实测**（720×1456 竖图，`--lowvram`）：4 步约 60 秒 / 张，**8 步（现在的默认）约 85 秒 / 张**，
在 608×1056 的关键帧尺寸下则是 8 步 39–87 秒。没有 OOM，也不需要关其他程序。

真的要更快再考虑 GGUF Q4（约 12 GB，需装 ComfyUI-GGUF），但按现在这个速度已经没必要了。

对比一下你现在用的 Qwen 2.1 Turbo（6.76 GB 权重）：2511 是 20B 模型，画质和一致性上一档，
但体积是 3 倍，慢是必然的。

### 使用

1. 四个模型放好，完全重启 ComfyUI。
2. 打开 `Qwen2511-Reference-Edit.json`（只换脸）或 `Qwen2511-MultiAngle-Edit.json`（换脸 + 转视角）。
3. `image 1` 选要改的那一帧，`image 2` 选目标人物，`image 3` 可选。
4. 改正向提示词，明确“保留什么、只改什么”；要转角度就加一句相机指令。
5. 运行。

### 提示词写法

锁构图换身份（默认模板就是这个思路）：

```text
把图1中人物的脸和发型替换成图2中的人物。
严格保留图1的人物姿势、身体比例、服装、背景、镜头角度和构图，不改变人物位置和大小。
只改变身份、脸型、五官和发型，不要复制图2的背景和服装。
双唇自然闭合、嘴角平直，不微笑、不露牙、不张嘴。
真实摄影，自然皮肤纹理，不新增人物，无文字水印。
```

### 转视角（多角度 LoRA）

换脸时想顺便把头的朝向也改掉，靠提示词硬写是没用的：编辑模型会默认“保留未提到的区域”，
角度一动脸就得整块重画。正确做法是叠一个相机控制 LoRA。

用的是 `Qwen-Edit-2509-Multiple-angles.safetensors`（dx8152，Apache-2.0，226 MB），
串在 Lightning LoRA 后面，权重 1.0。**不需要触发词**，直接用英文相机指令：

| 效果 | 指令 |
| --- | --- |
| 左转 45° | `Rotate the camera 45 degrees to the left.` |
| 右转 45° | `Rotate the camera 45 degrees to the right.` |
| 纯侧脸 | `Rotate the camera 90 degrees to the left, showing a clear side profile of the face.` |
| 俯视 | `Turn the camera to a top-down view.` |
| 广角 | `Turn the camera to a wide-angle lens.` |
| 特写 | `Turn the camera to a close-up.` |
| 推近 | `Move the camera forward.` |
| 上移 | `Move the camera up.` |

**一次只写一条指令最稳**；组合指令（"先转 45° 再俯视"）容易漂。

实跑验证（2026-10-31，同一张正脸竖图 + 同一个露牙笑头像当身份图，4 步，8 GB 卡）：

| 指令 | 结果 | 耗时 |
| --- | --- | ---: |
| 左转 45° | 3/4 侧脸，耳朵和下颌线出来，身份保持 | 60 s |
| 右转 45° | 另一侧 3/4，对称正常 | 73 s |
| 左转 90° | 接近纯侧脸，鼻梁轮廓清晰，**没有崩脸** | 60 s |

也就是说：**正脸 → 90° 侧脸这一步，2.1 Turbo 和 2511 裸模型都做不到，加了 LoRA 就能做到。**
之前“脸的角度永远不变”的问题到此解决。

使用要点：

- 背景会跟着视角一起轻微变化，这是正常的（镜头动了）；但它不会把场景换掉。
- 超过 90°（正脸 → 全背面）会超纲，需要模型凭空补后脑勺，成功率明显下降。
- 角度转得太猛、脸开始不像时：把多角度 LoRA 权重从 1.0 降到 0.7–0.8。
- 只是轻微转（15–30°）：可以只写“3/4 侧脸”，LoRA 权重给 0.6，比纯提示词稳。

如果要“转到目标角度 + 严格保住脸”，再叠 `image 3`（同人该角度的照片）效果最好。


### 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 模型下拉框里没有 2511 | 没重启 ComfyUI，或文件没放对目录 |
| 脸换了但完全不像 | 提高参考图分辨率与清晰度；补 `image 3` 同角度图；把 cfg 从 1.0 提到 1.5–2.0 试 |
| 背景/服装被抄过来 | 提示词里加“不要复制图2的背景和服装”，并明确只改脸和头发 |
| 明明写了“不笑”，出图还是带笑 | 身份图本身是露牙笑，表情会跟着参考图走。把 `image 3` 接一张同人的**中性闭嘴脸**压住，或换一张不笑的参考图 |
| 8 步细节仍不够 | 绕过 LoRA（`keyframe_qwen_lora: "none"`），改 20 步 / cfg 4.0 |
| 加了相机指令角度还是不动 | 确认打开的是 `MultiAngle` 那个文件、多角度 LoRA 节点没被 Ctrl+B 绕过；LoRA 力度偏弱时可绕过 Lightning 走 20 步 / cfg 4.0 |

## 两代模型的角度执行力实测（同一张正面参考照）

同一张正面半身照当身份参考、同一个 seed，四组角度描述的实测结论：

| 描述 | 2.1 | 2511 |
| --- | --- | --- |
| 正面看向镜头 | ✅ 正面 | ✅ |
| 头转 45° 四分之三侧脸 | ❌ 基本没转，还是正面 | ✅ 真转（略过头，接近 90° 全侧脸） |
| 背对镜头回头看 | ❌ 完全无视，还是正面站立 | ✅ 完美：背身 + 回头露四分之三脸，五官仍是同一人 |
| 低头看手机 | ✅ 执行（参考角度锥内的小幅动作） | ✅ |

**2.1 的头部/身体角度被参考图强锁死**：参考图什么角度，出图就什么角度；
只有低头这类小幅动作能做，大幅改角度的描述基本不执行。它把全部容量花在身份还原上，
所以「同机位走动、姿态变化」的关键帧链它又快又稳。

**实操选型**：

- 关键帧只需要走位、肢体动作，角度基本不变 → 2.1（18–24 秒/张）
- 镜头里人物要**转身、回头、看侧面** → 该段关键帧换 2511 后端（角度指令真执行，
  但侧脸的五官是模型推断的，正面像不保证，且 63–72 秒/张）
- 想让 2.1 也能转角度 → 参考图里加一张同人目标角度的照片（它有得抄才会抄）

## 身份相似度实测：2.1 明显更像，2511 的脸漂是模型固有行为

同一张正面半身照、同一个 seed、同一段正面提示词的对比：

| 后端 | 发型 | 脸 | 结论 |
| --- | --- | --- | --- |
| 2.1 | 披肩长发保持 | 脸型、眉眼、气质与参考一致 | **最像** |
| 2511 4 步 | 披肩发画成了马尾 | 脸变宽、眼睛变大变双 | 像同类型人，不是同一人 |
| 2511 8 步 | 同样画成马尾 | 偏差同上，只是细节更清楚 | 8 步**不是**漂的原因 |

2511 会把人脸向「标准美型」方向平均化（大眼、瘦脸、换发型），这是模型行为，
不是采样配置问题：

- 4 步 / 8 步都漂，8 步只是把固有偏差画得更清楚（该用还用，细节收益是真的）；
- 提示词明确锁「保持发型不变、不要马尾」能救回发型，但五官照样漂，
  还可能把「保持」理解成拍脸部特写、把全身构图带偏；
- 想提高 2511 的相似度，正解是**给一张同人高清晰度正脸特写当参考**（它有 3 个图槽），
  而不是调参数。

所以 2511 的定位是「**构图服从**优先」（转角度、大改动），2.1 的定位是「**身份保持**
优先」——两者正好互补，关键帧默认 2.1，要转角度的镜头手动用 2511 出图再替换。
| 转角度后脸变陌生 | 多角度 LoRA 权重降到 0.7–0.8；接 `image 3` 同角度参考；或分两步做（先转角度，再单独换脸） |

以上“带笑”这条是实跑验证过的：用一张露牙笑的头像当唯一身份参考时，4 步档的成图嘴角会轻微上扬，
接第三张中性脸后明显收敛。

## 先出角度图、再换回原人脸：两步混合路线实测

2.1 转不了角度，但有个自然的想法：**先用 2511 画一张目标角度的底图（plate），
再让模型照着正面参考照把脸换回去**。2026-11-01 两轮实测（2511 出 20° 偏头 plate，
同 seed、同参考）：

**第一轮：谁当换脸模型**（plate 清晰画布 + 正面参考）

| 换脸模型 | 角度 | 脸/发型 | 耗时 | 结论 |
| --- | --- | --- | --- | --- |
| 2.1 | ✅ 完全保持 20° | 换脸幅度很小，马尾改不回披肩 | 27s | 对清晰画布太忠实，参考只当「点缀」 |
| **2511** | ✅ 完全保持 20° | **发型换回披肩，脸型明显更接近参考** | 87s | 换得最彻底；副作用：景别会漂（半身变近景） |

**第二轮：2.1 能不能逼出换脸**（三组对照，同 plate、同强措辞）

| 变体 | 耗时 | 结果 |
| --- | --- | --- |
| 强措辞 8 步 LoRA | 27s | ❌ 还是 2511 的脸，马尾依旧 |
| 强措辞 + 原生 20 步 cfg 4.0 | 51s | ❌ 脸型略柔但仍偏 2511 |
| **糊画布**（canvas 先压到 0.12MP 再放大） | **24s** | ✅ **单眼皮细长眼、薄唇、披肩散发都向参考靠拢**，角度照旧 |

**糊画布是 2.1 换脸的开关**：清晰画布下 2.1 忠实到把参考当装饰；画布一糊它就得
重新想象细节，参考图的影响力立刻上来——而且姿势、角度、构图仍然跟着画布走。
原生 20 步帮助不大，措辞也救不了，起作用的就是「给画布降质」。

三条换脸路线排序（快 → 慢）：

1. **2.1 + 糊画布**（24s）：中等换脸力度，最适合量大的关键帧链；
2. 2.1 + 清晰画布（27s）：只轻微调脸，等于没换，不推荐用于换脸；
3. **2511 + 清晰画布**（87s）：换得最彻底（发型都能改），转角大镜头精修用。

**实操（代码已支持）**：任务 `config.json` 设

```json
"keyframe_qwen_canvas_refs": true,
"keyframe_qwen_canvas_blur_mp": 0.12
```

`keyframe_qwen_canvas_refs` 允许派生帧（有画布）同时喂参考图；
`keyframe_qwen_canvas_blur_mp` 把画布先压到这么多 MP 再放大回目标尺寸
（608×1056 ≈ 0.64MP，压到 0.12MP 约等于缩小 1/5）。两个键都是默认关闭，
不开时派生帧行为与之前完全一致。

⚠️ 换脸提示词必须明确「替换」语义，参考下面第一行；模糊的「延续构图」措辞
在有画布 + 参考时会把参考画成第二个人。

| 症状 | 处理 |
| --- | --- |
| 换脸后景别变近/变远 | 提示词里补「半身景别、人物在画面中的大小与图一完全一致」 |
| 2511 把「转 20 度」执行成 90 度 | 措辞改成「轻微偏转，脸部大部分仍朝向镜头，双眼仍能看到」 |
| 画布 + 参考出现两个人 | 提示词明确「把图一中的人物替换成图二的人」「画面中只有一个人」 |
| 2.1 换脸没效果 | 确认开了 `keyframe_qwen_canvas_blur_mp`；清晰画布下 2.1 不吃参考 |
| 糊画布后背景细节被重绘 | 正常现象；嫌糊就降到 0.2–0.3，或在换完脸后再跑一次普通精修 |

---

## Qwen-Image-2.1 版（8 步 Turbo）

`example_workflows/Qwen21-Reference-Edit.json`。2026-09-20 开源，**文生图和编辑合在一个模型里**，
视觉部分 7B（比 2511 的 20B 小一半多），官方定位是「小模强效」。ComfyUI 0.42+ 已内置
`TextEncodeQwenImage21` / `QwenImage21Cache`，**不需要装任何自定义节点**。

### 需要的三个模型文件

放到 `ComfyUI-Shared/models/` 对应子目录（都在 `Comfy-Org/Qwen-Image-2.1` 这个重打包仓库里）：

| 文件 | 目录 | 大小 |
| --- | --- | --- |
| `qwen_image_2.1_int8_convrot.safetensors` | `diffusion_models/` | 6.8 GB |
| `qwen3vl_8b_int8_convrot.safetensors` | `text_encoders/` | 8.8 GB |
| `qwen_image_2.1_vae_bf16.safetensors` | `vae/` | 645 MB |
| `qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors` | `loras/` | 872 MB（官方 Turbo，8 步） |
| `loras/Qwen-功能2.1/Qwen-Image-2.1-LR-8step-lora-r128.safetensors` | `loras/` | 343 MB（**8 步首选**，见下文 A/B） |

同一个画布、同一个 seed 对三个 8 步候选各跑一次（2026-11-01 实测，画布是 2511 出的
608×1056 街景全身图）：

| LoRA | 耗时 | 表现 |
| --- | --- | --- |
| `Qwen-功能2.1/Qwen-Image-2.1-LR-8step-lora-r128` | **18 秒** | 服装、鞋、构图保持最好，五官清晰 → **默认用这个** |
| `qwen_image_2.1_turbo_lora_avg_rank_178` | 30 秒 | 清晰，但服装漂移：西装丢了两只袖子变马甲 |
| `Qwen-功能2.1/p_qwen_image_2.1_8step_v0.1` | 18 秒 | 把人画成两个，连「不新增人物」后缀都压不住，淘汰 |

不用加速 LoRA（原生权重 20 步 / cfg 4.0）也支持：任务 `config.json` 设
`"keyframe_qwen_lora": "none"` 并配 `"keyframe_qwen_steps": 20`、`"keyframe_qwen_cfg": 4.0`
（原生步数必须一起给，否则会拿蒸馏档的 8 步跑原生权重，出图又糊又花）。实测对比
（同参考同 seed，2.1）：原生 20 步 45 秒，皮肤质感和发丝细节略好；LR-8step 15 秒，
肤色更「美颜」。质量差距不大，**任务链默认仍是 LoRA 8 步**，单张精修才值得切原生。

#### UNET 下拉框里那一堆 Qwen 模型，用哪个？

`UNETLoader` 下拉框里 qwen 相关候选的实际身份（2026-11-01 盘点）：

| 下拉框显示 | 是什么 | 结论 |
| --- | --- | --- |
| `qwen_image_2.1_int8_convrot.safetensors`（根目录） | 官方 7B int8 量化 → **关键帧默认用这个** | 稳 |
| `Qwen\qwen_image_2.1_int8_convrot.safetensors` | 和上面**同一文件的两份拷贝**（字节数完全一致），可删一份省 7.3 GB | 重复 |
| `Qwen\qwen_image_2.1_bf16.safetensors`（14.2 GB） | 官方全精度 | 8 GB 显存放不下，别选 |
| `qwen_image_2.1_turbo_int8_convrot.safetensors` | 社区蒸馏版 | 实测 15s vs 官方 18s、出图同级，省 3 秒不值得放弃官方权重 |
| `Qwen\Qwen21-noctQUncensoredRealism…` | 社区写实微调 | 实测 15s：能跑、像同类型人，但脸更尖、五官更锐，相似度低于官方 |
| `Qwen\Qwen21-rayQwen21…` | 社区微调 | 实测 15s：脸轮廓硬化、有网红感，徽章颜色发暗，不如官方 |
| `Qwen\Qwen21-redqw21UNLOCKEDV2…` | 社区去审查微调 | 实测 15s：**脸部大面积糊掉**，崩坏，不能用 |

编码器同理别混：2.1 配 `qwen3vl_8b_int8_convrot`；`qwen_2.5_vl_7b` 是 2511 的、
`qwen3vl_32b_minimax_h3` 是 H3 的、两个 4B 是老一代 Qwen-Image 的；
VAE 必须用 `qwen_image_2.1_vae_bf16`（`qwen_image_vae` 是 1.0/2511 的，混用会糊）。

⚠️ **老的 `qwen_image_vae.safetensors` 不能用**：它是 3 通道，解不了 2.1 的 4 通道输出。
⚠️ `CLIPLoader` 的 `type` 必须选 `qwen_image`，编码器选 **qwen3vl 8b**（不是 qwen3.5 的 prompt enhancer，
选错会出纯噪声）。

### 输入约定（和 2511 不一样）

| 输入 | 作用 |
| --- | --- |
| `images.image_1` | **画布**：输出尺寸、构图、主体姿势跟着它走 |
| `images.image_2` … `image_10` | 参考素材：服装、鞋包、另一角度、道具 |
| `resolution` | 约等于总像素预算，默认 1024（≈1MP），按 image_1 的比例算边长 |

想加第 4 张参考图：在 `TextEncodeQwenImage21` 的 `images` 上点「+」再连一根线。

### 参数

| 项 | 值 | 说明 |
| --- | --- | --- |
| `steps` | **8** | 配 Turbo LoRA；不挂 LoRA 时 25（官方 pipeline 40） |
| `cfg` | **1.0** | 官方路径。**cfg=1 时 negative 完全不生效** |
| `sampler` / `scheduler` | `euler` / `simple` | 官方推荐组合 |
| `denoise` | 1.0 | 编辑不是重绘，别降 |
| `resolution` | 1024 | **别填 0**：0 = 保持 image_1 原尺寸，12MP 的图会让速度从 0.3s/it 掉到 6s/it |
| `QwenImage21Cache` | `auto` / `default` | 显存紧就 `device=cpu`、`dtype=int8`（缓存减半，精度基本无损） |

### 提示词写法（官方三段式）

跟 2511 那种「图1/图2」自由写法不同，2.1 按这个顺序写最稳：

1. **先说不变**：身份、脸型、五官、发型、身体比例、姿势、镜头角度；
2. **再说改什么**：明确来自第几张图（「参考图2的服装」）；
3. **最后说融合**：光照、褶皱、材质、背景怎么自然接上。

模板已经预填在工作流的 `TextEncodeQwenImage21` 里，直接改。

### 和 2511 的四个区别

| | 2511 | 2.1 |
| --- | --- | --- |
| 参考图上限 | 3 张 | 10 张 |
| 局部编辑 | 只能靠提示词描述 | 圈选 / 画笔 / 独立 mask |
| 输出尺寸由谁决定 | `EmptyLatentImage` 的宽高 | `TextEncodeQwenImage21` 输出的 latent（跟着 image_1） |
| 许可 | Apache 系，可商用 | **Qwen Research License，非商用** |

### 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 加载报 `Unknown model architecture` | 那是 GGUF 节点的问题，本工作流用 safetensors 直读，不涉及 |
| 出图全是噪点 | 文本编码器选错了，必须是 `qwen3vl_8b_*`，`type` 必须 `qwen_image` |
| 解码报通道数不对 | VAE 用了老的 `qwen_image_vae.safetensors`，换 `qwen_image_2.1_vae_bf16` |
| 尺寸不对 / 变成正方形 | 检查 image_1 是不是 9:16；`resolution` 填 0 时会严格跟原图尺寸 |
| 很慢 | 先看 `resolution` 是不是 0，再确认 Turbo LoRA 有没有生效（steps=8） |
| 8 步细节糊 | 换 25 步（去掉 Turbo LoRA），或把 `resolution` 提到 1536 |

### 本机实测（2026-11-01，RTX 5060 Ti 8GB）

| 项 | 实测值 |
| --- | --- |
| 单张耗时 | **39 秒**（8 步，1MP，含首次加载 6.8G 生成器 + 8.7G 编码器） |
| 输出尺寸 | 736×1440（image_1 是 200×394 时按 1MP 换算，比例跟住 image_1） |
| 输出格式 | **RGBA**，但 alpha 全 255（没有透明像素），下游读图不受影响 |
| 对比 2511 | 2511 是 8 步约 63 秒/张，2.1 反而更快——模型小 3 倍，不用反复换页 |

验证用的是 API 直提（`/prompt`），说明 `images.image_1` 这套 autogrow 键名和
`TextEncodeQwenImage21` 的输出接法是通的。

### 怎么 A/B 对比两代

同一张原图、同一条提示词、同样的 9:16，各跑一次：

1. 打开 `Qwen2511-Reference-Edit.json`：8 步 Lightning / cfg 1.0，记下耗时；
2. 打开 `Qwen21-Reference-Edit.json`：8 步 LR-8step / cfg 1.0 / resolution 0，记下耗时；
3. 并排比四件事：**脸像不像**（最重要）、服装褶皱细节、构图有没有跑（头顶/鞋底还在不在）、耗时。

两个工作流的 SaveImage 前缀不同（`Qwen2511_reference_edit` vs `qwen21_edit`），输出不会混。

比完再决定 Director 里的 `keyframe_backend` 常驻选哪个。

### Director 自动出图（2026-11-01 起）

`keyframe_backend` 选 2511 或 2.1 时**不再需要手动开工作流**：`run_h3_long_video.py` 会用
`build_qwen_keyframe_graph()` 现搭图自动出全部首尾帧，和 H3 后端平级。上面的手动工作流保留，
用来试提示词、调参数、单张出图。

自动链路的关键设定：

| 项 | 值 | 原因 |
|---|---|---|
| 输出分辨率 | 写死 608×1056（`keyframe_width/height`） | 和 H3 草稿同尺寸；不用 `FluxKontextImageScale`，它会把 720×1280 缩成 752×1392，H3 拒收 |
| 首帧（无上一帧） | 2511 用 `EmptySD3LatentImage` 原生构图；2.1 把第一张参考图裁到 608×1056 当 image_1 | 2.1 的 latent 由编码器从 image_1 生成，必须先钉死尺寸 |
| 尾帧（有上一帧） | 上一帧缩到精确尺寸当画布：2511 `VAEEncode`，2.1 直接当 image_1、`resolution=0` | 锁构图、机位、人物大小，只改动作 |
| 尾帧的参考图 | **一张都不喂**（`reference_images` 只作用于首帧） | 实测（同画布同 seed）：画布单独出图是单人，画布 + 1 或 2 张身份参考都会出**两个人**——模型把"参考里同一人的照片"当成画面里的第二个主体，防多人后缀也压不住；画布本身已带身份、服装、构图 |
| 首帧的参考图 | 1 张就够；2 张也稳定单人（正脸清晰度最好的一张放第一） | 无画布时参考图是唯一身份来源，代码强制至少 1 张 |
| 其余参考图 | 2511 编码器内部自动压到 1MP；2.1 走 `ImageScaleToTotalPixels` 压到 0.4MP | 控制显存 |
| 提示词 | 自动追加「画面中只有同一个人物，不新增人物……」 | 2.1 实测会把两张身份参考画成多个人（首测出过"三胞胎"） |
| 耗时（本机实测） | 2.1 约 18–27 秒/张（LR-8step LoRA），2511 约 39–63 秒/张（8 步 Lightning） | 608×1056 比手测的 720×1456 小，更快 |
| 2.1 的 LoRA | 默认 `Qwen-功能2.1\Qwen-Image-2.1-LR-8step-lora-r128.safetensors`（8 步） | 换 LoRA 用 `keyframe_qwen_lora`，步数用 `keyframe_qwen_steps`，两者要配套 |

想改步数/尺寸：`config.json` 的 `keyframe_qwen_steps`、`keyframe_width/height`（必须是 32 的倍数）；
模型文件名在 `run_h3_long_video.py` 的 `QWEN_KEYFRAME_PRESETS`。
