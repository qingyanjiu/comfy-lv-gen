# 优质样本：Ref2VA 六段式（健身房 DV 自拍 vlog）

> 用户提供，2026-11-22。教科书级 Ref2VA 范本，六段式区段名/顺序/写法全部命中
> 官方 `h3-prompt-writing` skill 的 Ref2VA 规范。保留原文供对照学习，下面是拆解笔记。

## 原文

```
r34l1sm
subject_definitions:
<Subject 1> is CHASE, the young woman in <Picture 1>: pale porcelain skin, large light blue-gray eyes, long platinum-blonde hair with a soft side part and a low thick braid draped over one shoulder, slim athletic build, teal scoop-neck sports bra under an open black lightweight long-sleeve hooded zip-up with thumbhole cuffs, dark gray high-waisted mid-calf leggings, white crew socks, and black thick-sole athletic sneakers. In the scene she has a light post-workout glow, a towel around her neck, no jewelry, and may briefly remove shoes while stretching.

summary:
[reference generation] An approximately 15-second 16:9 calm post-workout gym vlog filmed as DV 16mm tape camcorder POV. <Subject 1> films herself through six short cuts during cool-down in a quiet evening gym, speaking casually in English while stretching, drinking an already-opened protein shake with the cap off the bottle, rating the workout, and leaving.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3], [Shot 4], [Shot 5], [Shot 6]): fully_preserved - face, platinum braid, teal sports bra, open black hoodie, dark gray capri leggings, white socks, black sneakers, and slim athletic body from <Picture 1> stay locked; only pose, towel placement, shoe on/off, and natural post-workout expressions change.

detailed_description:
The target video is live-action DV 16mm tape camcorder POV: soft blurry tape aesthetic, faint tape noise, bloomed gym lights, flickering auto-exposure, muted contrast, realistic skin, natural handheld shake, imperfect framing, delayed focus, clumsy zooms, face occasionally cut off at the edge. The camcorder itself is never visible in frame. Calm post-workout gym vlog tone: relaxed, reflective, slightly tired but happy, slow pacing, natural soft laughs. Quiet evening gym with stretching mat, mirror wall, gym bag, protein shake bottle with condensation, soft lighting, mostly empty floor.
[Shot 1] The camera is propped on a wall or gym bag for a slightly crooked medium shot of <Subject 1> (S1) sitting on a stretching mat. She exhales, rolls her shoulders, towel around her neck, and says in clear natural American English with accurate lip sync, <d>[English] Okay, cool-down time.</d> Soft auto-exposure flicker as gym fluorescents bloom.
[Shot 2] At 00:02.500, handheld POV resumes with imperfect framing as <Subject 1> (S1) leans into a hamstring stretch on the mat, face briefly half-cut by the frame. She laughs softly and says, <d>[English] Yeah, that was a lot today.</d> Delayed focus racks once onto her face then softens.
[Shot 3] At 00:05.000, a clumsy handheld macro pushes toward her hand as <Subject 1> picks up a cold protein shake bottle that is already open: the screw cap is fully off and set aside on the mat or gym bag, not on the bottle. Condensation beads are clearly visible on the plastic; the bottle mouth is open and empty of any lid. Ambient gym room tone and tape hiss only; no dialogue. A brief over-zoom then corrects.
[Shot 4] At 00:07.000, medium handheld shot of <Subject 1> (S1) drinking from the open bottle with no cap on it—the lid remains off-camera on the mat/bag, bottle mouth clearly uncapped as she tips it to drink—then smiles with a tired happy expression and says, <d>[English] Okay, that's actually really good.</d> Soft plastic bottle sound and a small gulp are audible. Negative continuity: never show a sealed or capped bottle while she drinks.
[Shot 5] At 00:10.000, close handheld framing as <Subject 1> (S1) reaches into an overhead stretch, rating the session casually, <d>[English] I'd give today like an eight out of ten.</d> Natural breathing and fabric rustle from the open hoodie.
[Shot 6] At 00:12.500, selfie-angle handheld as <Subject 1> (S1) grabs her gym bag and the still-uncapped shake bottle (cap still off, not screwed on), waves toward the lens with a relaxed smile, and says, <d>[English] Alright, I'm heading out—see you guys.</d> She stands and turns toward the exit while the shaky camera follows for one beat, then ends at about 00:15.000. Continuity: same person, outfit, gym, lighting direction, and tape look throughout; no jewelry; camcorder never appears in frame; whenever the shake is in hand for drinking or leaving, the bottle cap is off the bottle.

overall_soundscape:
Quiet evening gym room tone with soft HVAC hum, distant intermittent machine beeps, fabric rustle, mat contact, open protein-shake bottle handling, and continuous faint DV tape hiss. Natural soft laughs under dialogue. No crowd, no laugh track, no narration.

non_diegetic_music:
N/A
```

## 拆解笔记（真正值钱的地方）

1. **触发词置顶**：`r34l1sm` 单独一行放全文最前（fal 写实 LoRA 触发词）。本工作台 LoRA 由 base json 决定，不写触发词；但拿去线上/其他引擎时这一行不能丢。
2. **物件状态连续性钉死**（最关键）：蛋白粉瓶盖"已拧开、放在垫子/包上、不在瓶上"这件事，在 summary / Shot 3 / Shot 4 / Shot 6 / 段尾 Continuity 共 **5 处**重复，还专门写了 `Negative continuity: never show a sealed or capped bottle while she drinks.`。H3 会把"正常的瓶子"脑补成拧好盖的，这类反直觉状态必须正面反复 + 反面禁止双管齐下。这正是我们小熊项目踩过的坑（完整故事梗概注入每段→后续剧情提前发生）的通用版解法。
3. **retention_analysis 写清"变 vs 不变"边界**：`fully_preserved - ... stay locked; only pose, towel placement, shoe on/off, and expressions change.` 不是只喊"保留"，而是把允许自由变化的部分也列出来——既锁身份又给运动留出空间。规范 §2a 只要求保留强度词，这段多了"only X change"这半句，值得学。
4. **风格用可观察的物理缺陷表达**：DV 质感不是写"复古风格"，而是列 11 个具体缺陷（soft blurry / tape noise / bloomed lights / flickering exposure / muted contrast / handheld shake / imperfect framing / delayed focus / clumsy zooms / face cut off / camcorder never in frame）。符合规范 §4「不写空泛形容词，改成可观察细节」的极致版。
5. **A/V 分离干净**：对白只在 detailed_description 的 `<d>[English]…</d>` 出现；overall_soundscape 只写环境声 + 物理动作声，明写 `No crowd, no laugh track, no narration`；non_diegetic_music 写 N/A。规范 §6/§2a 要求对白不在声音区重复，这段是范例。
6. **说话人 ID 稳定**：`<Subject 1> (S1)` 全程一致；Shot 3 无对白就不挂 ID。唇同步明写 `with accurate lip sync`，但按官方规则 `<d>` 内只有语言标签 + 原话。
7. **时间码严格**：Shot 1 不带时间戳，Shot 2–6 用 00:02.500 / 00:05.000 / 00:07.000 / 00:10.000 / 00:12.500，递增且落在 15s 内，严格三位小数（规范 §4 要求两位小数秒，第三段式对齐行才严格两位；这里 detailed_description 用毫秒更精细，不冲突）。

## 与本工作台的差异（照搬会踩坑）

- 本文是**英文**写成；按用户约定，对话里生成的提示词正文用中文，对白/画内文字保留原语言。学结构，不学语言。
- 本文用 `<Subject 1>` / `<Picture 1>` 实体标签；工作台 runner 不识别 `<Subject N>`，人物身份写在全局 head，正文用角色名；参考图写 `<Picture {pic:N}>` 占位符。直接投喂本文给 `seg` 命令不可行，要按官方 skill 在对话里改写后粘回工作台。
- 本文 `r34l1sm` 触发词对本工作台无效（LoRA 由 base json 节点 240/241 决定）。

## 中文对照（与上方原文按区段一一对应）

> 区段名与标签保留英文原样（它们是工程结构符号），其余内容逐句译出。对白标签 `<d>[English]…</d>` 内的语言标记与台词也译为中文，仅作理解用；实际送片须换回目标语言标签。

**r34l1sm**（LoRA 触发词，置顶保留）

**subject_definitions（主体定义）:**
`<Subject 1>` 是 CHASE，`<Picture 1>` 里的年轻女性：瓷白皮肤，大大的浅蓝灰色眼睛，铂金色长发、柔和侧分，一条粗辫子低低垂在一侧肩头，瘦而结实的运动体型；青绿色 U 领运动内衣，外面敞穿一件黑色轻薄长袖连帽拉链衫、袖口带拇指孔；深灰高腰及小腿打底裤，白色船袜，黑色厚底运动鞋。场景里她带着运动后微红的气色，脖子搭毛巾，不戴首饰，拉伸时可能短暂脱鞋。

**summary（摘要）:**
[参考生成] 一段约 15 秒、16:9 的平静运动后健身房 vlog，DV 16mm 磁带摄像机主观视角拍摄。`<Subject 1>` 在安静的傍晚健身房里用六个短镜头拍下自己的放松过程：一边拉伸一边随口说英语，喝一瓶**已经拧开、盖子不在瓶上**的蛋白奶昔，给训练打分，然后离开。

**retention_analysis（保留度分析）:**
`<Subject 1>`（出现在 [Shot 1] 至 [Shot 6] 全部六镜）：完全保留 —— 脸、铂金辫子、青绿运动内衣、敞开的黑色连帽衫、深灰七分打底裤、白袜、黑鞋，以及 `<Picture 1>` 里瘦而结实的身材全部锁死；只有姿势、毛巾搭法、鞋的穿脱、以及运动后自然表情是允许变化的。

**detailed_description（详细描述）:**
目标视频为真实拍摄的 DV 16mm 磁带摄像机主观视角：柔软模糊的磁带质感、微弱磁带噪点、健身房灯光泛光、自动曝光闪烁、低饱和对比、真实皮肤、自然手持抖动、不完美构图、对焦延迟、笨拙变焦、脸偶尔被画面边缘切掉。摄像机本身**永不入画**。基调是平静的运动后健身房 vlog：放松、若有所思、略累但开心，节奏缓慢，自然轻笑。安静的傍晚健身房，有拉伸垫、镜面墙、健身包、挂着水珠的蛋白奶昔瓶，灯光柔和，地面基本空着。

- **[Shot 1]** 摄像机支在墙上或健身包上，形成一个略微歪斜的中景，`<Subject 1>`（S1）坐在拉伸垫上。她呼气、转肩，脖子搭毛巾，用清晰自然的美式英语、口型精准地说：`<d>[英语] 好了，开始放松拉伸吧。</d>` 健身房荧光灯泛光，自动曝光轻微闪烁。
- **[Shot 2]** 00:02.500，手持主观视角恢复、构图不完美，`<Subject 1>`（S1）在垫子上做腿筋拉伸，脸一度被画面切掉一半。她轻轻笑出声，说：`<d>[英语] 是啊，今天练得够呛。</d>` 焦点延迟地落到她脸上一次，然后又虚下去。
- **[Shot 3]** 00:05.000，笨拙的手持微距推向她的手 —— `<Subject 1>` 拿起一瓶**本来就已打开**的蛋白奶昔：螺旋盖已完全拧下、放在垫子或健身包上，不在瓶子上。塑料瓶身水珠清晰可见；瓶口敞开、没有任何盖子。只有健身房环境底噪和磁带嘶嘶声，没有对白。先过度推近，再拉回修正。
- **[Shot 4]** 00:07.000，中景手持，`<Subject 1>`（S1）对着**没盖盖子**的瓶子喝 —— 盖子始终在画面外的垫子/包上，她仰头喝时瓶口明显敞开 —— 然后露出疲惫而开心的笑，说：`<d>[英语] 嗯，这还真挺好喝的。</d>` 能听到塑料瓶轻响和一小口吞咽。**负面连续性：她喝的时候绝不能出现密封或拧好盖子的瓶子。**
- **[Shot 5]** 00:10.000，手持近景，`<Subject 1>`（S1）做举臂伸展，随口给这次训练打分：`<d>[英语] 今天我大概给八分吧，满分十分。</d>` 自然的呼吸声和敞开连帽衫的布料摩擦声。
- **[Shot 6]** 00:12.500，自拍角度手持，`<Subject 1>`（S1）抓起健身包和那瓶**依旧没盖盖子**的奶昔（盖子还是拧下的，没拧回去），对镜头轻松一笑挥手，说：`<d>[英语] 好啦，我先走咯 —— 回头见，各位。</d>` 她起身朝出口走去，抖动的镜头跟了一拍，约 00:15.000 结束。**连续性：全程同一人物、同一套服装、同一健身房、同一光向、同一磁带质感；不戴首饰；摄像机永不入画；只要奶昔拿在手里喝或带走，盖子就必须不在瓶上。**

**overall_soundscape（整体声景）:**
安静的傍晚健身房环境底噪，有柔和的空调嗡鸣、远处偶尔的器械提示音、布料摩擦、垫子接触、拿放敞口蛋白奶昔瓶的声音，以及贯穿始终的微弱的 DV 磁带嘶嘶声。对白下方是自然轻笑。没有人群、没有罐头笑声、没有旁白。

**non_diegetic_music（非叙事配乐）:**
无（N/A）
