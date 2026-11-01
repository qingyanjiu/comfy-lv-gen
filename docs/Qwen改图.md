# Qwen 改图（2511 与 2.1）

两条编辑线并存，**可以互相替换**：同一张原图、同一套参考图，换张工作流就行。
在 Director 面板里用「关键帧来源」这个全局配置选哪条线（见 `面板交互设计.md` D11）。

| 工作流 | 模型 | 步数 | 什么时候用 |
| --- | --- | --- | --- |
| `example_workflows/Qwen2511-Reference-Edit.json` | Qwen-Image-Edit 2511（20B） | 4（Lightning LoRA） | 默认。生态最稳、有大量角度 LoRA、Apache 许可可商用 |
| `example_workflows/Qwen2511-MultiAngle-Edit.json` | 同上 + 多角度 LoRA | 4 | 要转视角 |
| `example_workflows/Qwen21-Reference-Edit.json` | Qwen-Image-2.1（7B） | 8（Turbo LoRA） | 要喂 4～10 张参考图、要 mask 局部改、或显存吃紧 |

都是扁平结构，主画布上直接能改提示词、步数、seed，没有子图。

下面先讲 2511（主线），2.1 见文末。

## 2511 的两个工作流

| 文件 | 用途 |
| --- | --- |
| `example_workflows/Qwen2511-Reference-Edit.json` | 锁构图换脸（默认就是你要的“只换脸、不动角度”） |
| `example_workflows/Qwen2511-MultiAngle-Edit.json` | 换脸 + 转视角（多串了一个多角度 LoRA） |

都是扁平结构，主画布上直接能改提示词、步数、seed，没有子图。

## 先澄清版本

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

## 输入

| 输入 | 作用 |
| --- | --- |
| `image 1` | 要改的原图，决定姿势、构图、背景；输出尺寸也跟随它（自动缩放到约 100 万像素） |
| `image 2` | 身份参考，决定脸型、五官、发型 |
| `image 3` | 可选。接同一人的侧脸/半侧脸，用来给模型补“另一个角度”的信息；不接也能跑 |

提示词里用 **图1 / 图2** 指代（英文写 image 1 / image 2 也认）。正向提示词节点已经预填了一段
锁构图换身份的模板，直接改就行。

## 采样参数

默认走 4 步 Lightning LoRA：

| 参数 | 值 |
| --- | --- |
| steps | 4 |
| cfg | 1.0 |
| sampler | euler |
| scheduler | simple |
| denoise | 1.0 |
| LoRA 权重 | 1.0 |

要质量档：选中 `4 步加速 LoRA` 节点按 `Ctrl+B` 绕过，然后 steps 改 **20**、cfg 改 **4.0**。
官方参考值是 Qwen 原生 40 步 / Comfy 20 步，cfg 4.0。

为什么选 4 步而不是 8 步：Lightning 两档都出了（4steps / 8steps V1.0），但在这张 8 GB 卡上
8 步只是把时间翻倍，细节提升很有限；真要质量就绕过 LoRA 走 20 步 / cfg 4.0，收益比 8 步大。

想试 8 步：把 LoRA 节点的文件名换成 `Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors`，
steps 改 8，其余不动（cfg 仍是 1.0）。这个文件也在下面的下载清单里，两个 LoRA 都下好了，直接切换即可。

## 模型

| 文件 | 大小 | 放到（本机实际路径） |
| --- | ---: | --- |
| `qwen_image_edit_2511_int8_convrot.safetensors` | 19.09 GB | `D:\Comfy-Desktop\ComfyUI-Shared\models\diffusion_models\` |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 8.74 GB | `D:\Comfy-Desktop\ComfyUI-Shared\models\text_encoders\` |
| `qwen_image_vae.safetensors` | 242 MB | `D:\Comfy-Desktop\ComfyUI-Shared\models\vae\` |
| `Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors` | 810 MB | `D:\Comfy-Desktop\ComfyUI-Shared\models\loras\` |
| `Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors` | 810 MB | 同上（可选，想试 8 步才用） |
| `Qwen-Edit-2509-Multiple-angles.safetensors` | 226 MB | 同上（多角度版本才用到） |

当前运行的 ComfyUI 已经用 `--lowvram` 启动，并映射了这个 Shared 目录（krea2、z_image 就在里面），
所以直接放进上面四个文件夹即可；`ComfyUI-Installs\ComfyUI\ComfyUI\models` 那份也会被扫描到，放哪边都行，
但别只放一半。

国内优先走 ModelScope：

- `https://modelscope.cn/models/Comfy-Org/Qwen-Image-Edit_ComfyUI`（主模型）
- `https://modelscope.cn/models/Comfy-Org/HunyuanVideo_1.5_repackaged`（文本编码器，官方就是放在这个仓库里）
- `https://modelscope.cn/models/Comfy-Org/Qwen-Image_ComfyUI`（VAE）
- `https://modelscope.cn/models/lightx2v/Qwen-Image-Edit-2511-Lightning`（4 步 LoRA）

Hugging Face 备用路径（同一份文件）：

```text
https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_edit_2511_int8_convrot.safetensors
https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors
https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors
https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/main/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors
```

下载完**完全重启 ComfyUI**，否则模型下拉框里看不到。

## 这台机器的性能预期

RTX 5060 Ti 8 GB 显存 + 25 GB 内存，19 GB 的主模型走内存换页。

**实测**（2026-10-31，720×1456 竖图，4 步，`--lowvram`）：首次加载 + 出图合计 **约 60 秒 / 张**，
没有 OOM，也不需要关其他程序。分辨率越高越慢，1 MP 以内基本都在一分钟级。

真的要更快再考虑 GGUF Q4（约 12 GB，需装 ComfyUI-GGUF），但按现在这个速度已经没必要了。

对比一下你现在用的 Qwen 2.1 Turbo（6.76 GB 权重）：2511 是 20B 模型，画质和一致性上一档，
但体积是 3 倍，慢是必然的。

## 使用

1. 四个模型放好，完全重启 ComfyUI。
2. 打开 `Qwen2511-Reference-Edit.json`（只换脸）或 `Qwen2511-MultiAngle-Edit.json`（换脸 + 转视角）。
3. `image 1` 选要改的那一帧，`image 2` 选目标人物，`image 3` 可选。
4. 改正向提示词，明确“保留什么、只改什么”；要转角度就加一句相机指令。
5. 运行。

## 提示词写法

锁构图换身份（默认模板就是这个思路）：

```text
把图1中人物的脸和发型替换成图2中的人物。
严格保留图1的人物姿势、身体比例、服装、背景、镜头角度和构图，不改变人物位置和大小。
只改变身份、脸型、五官和发型，不要复制图2的背景和服装。
双唇自然闭合、嘴角平直，不微笑、不露牙、不张嘴。
真实摄影，自然皮肤纹理，不新增人物，无文字水印。
```

## 转视角（多角度 LoRA）

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


## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 模型下拉框里没有 2511 | 没重启 ComfyUI，或文件没放对目录 |
| 脸换了但完全不像 | 提高参考图分辨率与清晰度；补 `image 3` 同角度图；把 cfg 从 1.0 提到 1.5–2.0 试 |
| 背景/服装被抄过来 | 提示词里加“不要复制图2的背景和服装”，并明确只改脸和头发 |
| 明明写了“不笑”，出图还是带笑 | 身份图本身是露牙笑，表情会跟着参考图走。把 `image 3` 接一张同人的**中性闭嘴脸**压住，或换一张不笑的参考图 |
| 4 步细节糊 | 绕过 LoRA，改 20 步 / cfg 4.0 |
| 加了相机指令角度还是不动 | 确认打开的是 `MultiAngle` 那个文件、多角度 LoRA 节点没被 Ctrl+B 绕过；4 步下 LoRA 力度偏弱时可绕过 Lightning 走 20 步 |
| 转角度后脸变陌生 | 多角度 LoRA 权重降到 0.7–0.8；接 `image 3` 同角度参考；或分两步做（先转角度，再单独换脸） |

以上“带笑”这条是实跑验证过的：用一张露牙笑的头像当唯一身份参考时，4 步档的成图嘴角会轻微上扬，
接第三张中性脸后明显收敛。

---

# Qwen-Image-2.1 版（8 步 Turbo）

`example_workflows/Qwen21-Reference-Edit.json`。2026-09-20 开源，**文生图和编辑合在一个模型里**，
视觉部分 7B（比 2511 的 20B 小一半多），官方定位是「小模强效」。ComfyUI 0.42+ 已内置
`TextEncodeQwenImage21` / `QwenImage21Cache`，**不需要装任何自定义节点**。

## 需要的三个模型文件

放到 `ComfyUI-Shared/models/` 对应子目录（都在 `Comfy-Org/Qwen-Image-2.1` 这个重打包仓库里）：

| 文件 | 目录 | 大小 |
| --- | --- | --- |
| `qwen_image_2.1_int8_convrot.safetensors` | `diffusion_models/` | 6.8 GB |
| `qwen3vl_8b_int8_convrot.safetensors` | `text_encoders/` | 8.8 GB |
| `qwen_image_2.1_vae_bf16.safetensors` | `vae/` | 645 MB |
| `qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors` | `loras/` | 872 MB（8 步加速） |

⚠️ **老的 `qwen_image_vae.safetensors` 不能用**：它是 3 通道，解不了 2.1 的 4 通道输出。
⚠️ `CLIPLoader` 的 `type` 必须选 `qwen_image`，编码器选 **qwen3vl 8b**（不是 qwen3.5 的 prompt enhancer，
选错会出纯噪声）。

## 输入约定（和 2511 不一样）

| 输入 | 作用 |
| --- | --- |
| `images.image_1` | **画布**：输出尺寸、构图、主体姿势跟着它走 |
| `images.image_2` … `image_10` | 参考素材：服装、鞋包、另一角度、道具 |
| `resolution` | 约等于总像素预算，默认 1024（≈1MP），按 image_1 的比例算边长 |

想加第 4 张参考图：在 `TextEncodeQwenImage21` 的 `images` 上点「+」再连一根线。

## 参数

| 项 | 值 | 说明 |
| --- | --- | --- |
| `steps` | **8** | 配 Turbo LoRA；不挂 LoRA 时 25（官方 pipeline 40） |
| `cfg` | **1.0** | 官方路径。**cfg=1 时 negative 完全不生效** |
| `sampler` / `scheduler` | `euler` / `simple` | 官方推荐组合 |
| `denoise` | 1.0 | 编辑不是重绘，别降 |
| `resolution` | 1024 | **别填 0**：0 = 保持 image_1 原尺寸，12MP 的图会让速度从 0.3s/it 掉到 6s/it |
| `QwenImage21Cache` | `auto` / `default` | 显存紧就 `device=cpu`、`dtype=int8`（缓存减半，精度基本无损） |

## 提示词写法（官方三段式）

跟 2511 那种「图1/图2」自由写法不同，2.1 按这个顺序写最稳：

1. **先说不变**：身份、脸型、五官、发型、身体比例、姿势、镜头角度；
2. **再说改什么**：明确来自第几张图（「参考图2的服装」）；
3. **最后说融合**：光照、褶皱、材质、背景怎么自然接上。

模板已经预填在工作流的 `TextEncodeQwenImage21` 里，直接改。

## 和 2511 的四个区别

| | 2511 | 2.1 |
| --- | --- | --- |
| 参考图上限 | 3 张 | 10 张 |
| 局部编辑 | 只能靠提示词描述 | 圈选 / 画笔 / 独立 mask |
| 输出尺寸由谁决定 | `EmptyLatentImage` 的宽高 | `TextEncodeQwenImage21` 输出的 latent（跟着 image_1） |
| 许可 | Apache 系，可商用 | **Qwen Research License，非商用** |

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 加载报 `Unknown model architecture` | 那是 GGUF 节点的问题，本工作流用 safetensors 直读，不涉及 |
| 出图全是噪点 | 文本编码器选错了，必须是 `qwen3vl_8b_*`，`type` 必须 `qwen_image` |
| 解码报通道数不对 | VAE 用了老的 `qwen_image_vae.safetensors`，换 `qwen_image_2.1_vae_bf16` |
| 尺寸不对 / 变成正方形 | 检查 image_1 是不是 9:16；`resolution` 填 0 时会严格跟原图尺寸 |
| 很慢 | 先看 `resolution` 是不是 0，再确认 Turbo LoRA 有没有生效（steps=8） |
| 8 步细节糊 | 换 25 步（去掉 Turbo LoRA），或把 `resolution` 提到 1536 |

## 本机实测（2026-11-01，RTX 5060 Ti 8GB）

| 项 | 实测值 |
| --- | --- |
| 单张耗时 | **39 秒**（8 步，1MP，含首次加载 6.8G 生成器 + 8.7G 编码器） |
| 输出尺寸 | 736×1440（image_1 是 200×394 时按 1MP 换算，比例跟住 image_1） |
| 输出格式 | **RGBA**，但 alpha 全 255（没有透明像素），下游读图不受影响 |
| 对比 2511 | 2511 是 4 步约 60 秒/张，2.1 反而更快——模型小 3 倍，不用反复换页 |

验证用的是 API 直提（`/prompt`），说明 `images.image_1` 这套 autogrow 键名和
`TextEncodeQwenImage21` 的输出接法是通的。

## 怎么 A/B 对比两代

同一张原图、同一条提示词、同样的 9:16，各跑一次：

1. 打开 `Qwen2511-Reference-Edit.json`：4 步 / cfg 1.0，记下耗时；
2. 打开 `Qwen21-Reference-Edit.json`：8 步 Turbo / cfg 1.0 / resolution 1024，记下耗时；
3. 并排比四件事：**脸像不像**（最重要）、服装褶皱细节、构图有没有跑（头顶/鞋底还在不在）、耗时。

两个工作流的 SaveImage 前缀不同（`Qwen2511_reference_edit` vs `qwen21_edit`），输出不会混。

比完再决定 Director 里的 `keyframe_backend` 常驻选哪个——它只是个记录 + 跳过 H3 关键帧的开关，
不会自动帮你调模型，所以**先手动试，再定默认值**。
