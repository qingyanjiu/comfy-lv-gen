"""把各种分镜输入归一化成 SelfLift 的 segments 结构。

支持三种输入（网页「导入 JSON」和 CLI `import` 共用同一套规则）：

1. 已经是 SelfLift 结构：`{"segments":[{"body": "...", ...}], ...}`
2. 旧 Director rich storyboard：`segments[].prompt_segment / prompt_video / shot + open_action ...`
3. 只有一整段长文本（`prompt` / `text` / `content` / 纯字符串）→ 按空行或句号自动切分

输出是纯数据，不碰文件系统，方便单测。
"""
from __future__ import annotations

import re

SEG_TEXT_KEYS = (
    "body", "prompt", "prompt_segment", "prompt_video",
    "text", "content", "description", "desc", "action",
)
SEG_TITLE_KEYS = ("title", "name", "shot_name", "label")
COMPOSE_KEYS = ("shot", "camera", "light", "open_action", "motion", "end_action", "frame")
SEGMENT_LIST_KEYS = ("segments", "scenes", "shots", "clips", "items", "parts")
# 段级字段原样透传（seed / 接缝 / 段级 head / 首尾帧锁定）
SEG_PASSTHROUGH_KEYS = ("seed", "link", "head",
                        "first_frame_override", "last_frame_override")

DEFAULT_HEAD = (
    "<Picture 1> 是人物参考，完整还原其中人物的脸型、五官、发型、服装与身材比例，"
    "全程不换人、不换装。\n\n"
    "<Picture 2> 是全片唯一的场景空镜参考，环境结构、陈设与光源方向以它为准，保持不变。"
)
DEFAULT_TAIL = (
    "写实电影摄影，35mm 镜头，柔和浅景深，真实光学散景，细腻胶片颗粒。"
    "画面中只有剧情里的人物，不出现文字、水印、字幕。\n\n"
    "[衔接] 段首约 0.5 秒动作与镜头保持稳定，段尾约 0.5 秒动作收住。"
)


def _clean(text) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())


def _pick_segment_text(item) -> str:
    if isinstance(item, str):
        return _clean(item)
    if not isinstance(item, dict):
        return ""
    for key in SEG_TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _clean(value)
    parts = []
    for key in COMPOSE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_clean(value))
    return "\n\n".join(parts)


def _pick_title(item, index: int) -> str:
    if isinstance(item, dict):
        for key in SEG_TITLE_KEYS:
            value = item.get(key)
            if value and str(value).strip():
                return str(value).strip()[:24]
    text = _pick_segment_text(item)
    head = re.sub(r"\s+", " ", text)[:14].strip()
    return head or f"第 {index} 段"


def split_text(text: str, target_chars: int = 130, max_segments: int = 16) -> list[str]:
    """把一整段长文本切成多段：先按空行/分隔线，再按句号聚合到目标字数。"""
    text = _clean(text)
    if not text:
        return []
    blocks = [
        block.strip()
        for block in re.split(
            r"\n\s*\n|(?:^|\n)\s*(?:-{3,}|={3,}|\*{3,})\s*(?:\n|$)"
            r"|(?:^|\n)\s*\[?\s*第\s*[0-9一二三四五六七八九十]+\s*[段镜]\s*\]?\s*(?:\n|$)",
            text,
        )
        if block.strip()
    ]
    if len(blocks) > 1:
        return blocks[:max_segments]

    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if s.strip()]
    if len(sentences) <= 1:
        return [text]

    segments, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target_chars:
            segments.append(current.strip())
            current = sentence
        else:
            current += sentence
        if len(segments) >= max_segments:
            break
    if current.strip():
        segments.append(current.strip())
    return segments or [text]


def _collect_raw_segments(raw) -> tuple[list, dict]:
    """抽出段列表和顶层元数据。"""
    if isinstance(raw, str):
        return [raw], {}
    if isinstance(raw, list):
        return raw, {}
    if isinstance(raw, dict):
        for key in SEGMENT_LIST_KEYS:
            value = raw.get(key)
            if isinstance(value, list) and value:
                return value, raw
        return [raw], raw
    return [], {}


def normalize(raw, *, title=None, seconds=None, fps=24, megapixels=0.5,
              target_chars=130, split_single=True, max_segments=16) -> dict:
    """把任意分镜输入归一化成 SelfLift 结构（不写文件）。"""
    items, meta = _collect_raw_segments(raw)

    bodies = []
    for item in items:
        text = _pick_segment_text(item)
        if not text:
            continue
        # 只有一段但很长 → 自动切分
        if split_single and len(items) == 1:
            bodies.extend(split_text(text, target_chars=target_chars,
                                     max_segments=max_segments))
        else:
            bodies.append(text)
    if not bodies and isinstance(raw, str):
        bodies = split_text(raw, target_chars=target_chars, max_segments=max_segments)
    if not bodies:
        raise ValueError("没解析出任何段落提示词")

    seconds = float(seconds or meta.get("seconds") or meta.get("seconds_per_segment") or 8.0)
    fps = int(meta.get("fps") or fps)
    megapixels = float(meta.get("megapixels") or megapixels)

    segments = []
    for index, body in enumerate(bodies, 1):
        source = items[index - 1] if index - 1 < len(items) else {}
        item = {
            "id": f"s{index:02d}",
            "title": _pick_title(source, index) if source else
                     (re.sub(r"\s+", " ", body)[:14] or f"第 {index} 段"),
            "body": body,
        }
        # 段级 seed / link / head / 首尾帧锁定：模板里写了就照着用
        if isinstance(source, dict):
            for key in SEG_PASSTHROUGH_KEYS:
                value = source.get(key)
                if value in (None, "", {}, []):
                    continue
                if key == "link" and not isinstance(value, dict):
                    continue
                if key == "link" and index == 1:
                    continue        # 首段没有接缝，忽略
                item[key] = value
        segments.append(item)

    return {
        "title": str(title or meta.get("title") or "未命名长片"),
        "head": _clean(meta.get("head") or meta.get("style") or "") or DEFAULT_HEAD,
        "tail": _clean(meta.get("tail") or "") or DEFAULT_TAIL,
        "seconds": seconds,
        "fps": fps,
        "megapixels": megapixels,
        "ref_image": str(meta.get("ref_image") or ""),
        "ref_images": ref_list(meta.get("ref_images")),
        "bg_plate_path": str(meta.get("bg_plate_path") or ""),
        "segments": segments,
    }


def ref_list(raw):
    """规整 ref_images：既接受 ["a.png", …]，也接受 [{"path","comfy_name","label"}, …]。

    返回顺序就是 Picture 编号顺序（第 1 个是 P1）。空镜板不在这里，由 bg_plate 单独管。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            path, name, label = item, "", ""
        elif isinstance(item, dict):
            path = item.get("path") or item.get("image") or ""
            name = item.get("comfy_name") or item.get("name") or ""
            label = item.get("label") or ""
        else:
            continue
        path = str(path).strip()
        if path:
            out.append({"path": path, "comfy_name": str(name),
                        "label": str(label)})
    return out


# ---------------------------------------------------------------- 模板（供网页下载）

FIELD_DOC = {
    "title": "片名。也是成片文件名与建片目录名的前缀。",
    "seconds": "每段时长（秒），「全片统一」。会被量化成 17n+5 帧：5s→124 帧、8s→209 帧、10s→260 帧。改这里等于改所有段。",
    "fps": "帧率，默认 24。非必要不要改，模型按 24fps 训练。",
    "megapixels": "每段画幅，按总像素算。0.5 ≈ 960×544（16:9），0.3 ≈ 736×416，0.8 ≈ 1216×688。8GB 显存建议 ≤0.5。",
    "head": "全片公共前置提示词，拼在每段 body 之前。用来锁定人物身份、空镜板环境、画风。"
            "「图片编号请写 {pic:1} / {pic:plate} 这种占位符，不要写死 <Picture 1>」——"
            "工具会按每段实际接线自动换算成真实编号（见 _规则说明 的 picture 条）。",
    "tail": "全片公共后置提示词，拼在每段 body 之后。放画风/颗粒/禁止文字水印/段首尾静止锚。",
    "ref_image": "【单人写法】人物参考图的「绝对路径」（D:/xxx/refs/girl.png）。留空则用网页上「上传参考图」传的那张。有多个人物请改用下面的 ref_images，两者同时存在时 ref_images 优先。",
    "ref_images": (
        "【多人写法】人物参考图数组，顺序就是 Picture 编号顺序：第 1 个是 P1、第 2 个是 P2……\n"
        "每项写成 {\"path\": \"D:/xxx/refs/a.png\", \"comfy_name\": \"xx_a.png\", \"label\": \"女生 A\"}，"
        "comfy_name 可省（自动按「前缀_ref序号」生成），label 只用于网页显示。\n"
        "⚠ 一人一张，别把多个人拼进一张图——拼图后每张脸只占 1/4 画幅，谁都锁不住，"
        "而且模型会把整张图的四人并排构图当成模板去复刻。\n"
        "⚠ 参考图用「半身或胸像」，脸占画幅 1/3 以上；全身景脸只占 10~15% 高时锁不住具体的人。\n"
        "空镜板不占人物位，永远是最后一张（3 个人物 + 空镜板 → 人物 P1/P2/P3，空镜板 P4）。"
    ),
    "bg_plate_path": "空镜板（无人场景图）的「绝对路径」。留空则用网页「上传空镜板」。",
    "segments": "分段列表，顺序即播放顺序。每段至少要有 body。",

    "segments[].title": "段标题，只用于网页卡片与日志显示，不参与生成。",
    "segments[].body": "该段提示词（必填）。只写这一段发生的事：人物动作 + 机位 + 光线 + 场景，不要写「上一段如何、下一段如何」。head/tail 会自动包在外面。",
    "segments[].seed": "该段随机种子（整数）。同一个 body 换 seed 就换一条演绎；不满意就换 seed 重出，别改 body。「别用 100000」（0.1% 恶性种子，必出瑕疵）。留空则自动随机。",
    "segments[].head": "可选。**整段覆盖**全局 head（不是追加），只对这一段生效。"
                       "以前链式段必须重写 head，现在 head 里用 {pic:} 占位符就自动换算，"
                       "所以只有「本段要额外强调某件事」或「本段接线特殊」时才需要写。",
    "segments[].link": "可选。本段与「上一段」怎么接。第 1 段忽略此字段。不写 = 默认链式承接。",
    "segments[].link.mode": "chain = 链式承接：本段第 0 帧 = 上段尾帧，动作连续、像素级接上（实测接缝 MAE 2~3）。transition = 转场：本段独立生成，不接上段尾帧，只靠拼接时叠化/黑屏过渡；改上游不会让你重出。",
    "segments[].link.transition": "仅 mode=transition 时生效。dissolve = 0.25s 交叉溶解（默认，最自然）；black = 0.8s 纯黑硬切（换场/换时间）；cut = 直接硬切（同场景切机位）。",
    "segments[].first_frame_override": "可选。用一张指定图当本段首帧，「优先于上段尾帧」。填图片文件名（如 sl_xxx_s01_last.png，会在 ComfyUI input 与片目录 refs/ 里找），一般由网页「设为本段首帧」按钮自动写入，手改少用。",
    "segments[].last_frame_override": "可选。用一张指定图当本段尾帧（实验性，默认不用）。一般由网页「锁定本段尾帧」按钮写入。",
}

FIELD_NOTE = {
    "picture": (
        "<Picture N> 按「本段实际接了几张图」从 1 数起，顺序是：先 first_frame → 再 last_frame → "
        "再 ref_image_1..9（人物参考按顺序排前面，空镜板永远最后）。所以同一片里编号会漂：\n"
        "· 单人第 1 段：P1=人物，P2=空镜板\n"
        "· 单人链式段：P1=首帧，P2=人物，P3=空镜板\n"
        "· 双人链式段：P1=首帧，P2=人物A，P3=人物B，P4=空镜板\n\n"
        "**所以不要在 head 里写死数字**，写成占位符，工具按本段接线自动换算：\n"
        "· {pic:1} / {pic:2} … = 第几个人物\n"
        "· {pic:plate} = 空镜板\n"
        "· {pic:first} = 本段首帧（上段尾帧）、{pic:last} = 本段锁定的尾帧\n"
        "· {pic?:first} = **可选**：本段没接首帧时，这一整行自动消失（写「本段首帧必须无缝延续」"
        "这种句子时用它，第 1 段就不会留半句空话）\n"
        "head 写一遍就能全片通用，第 1 段和链式段各自算对。"
        "引用本段没接的媒体会直接报错并列出本段实际接线。"
    ),
    "multi_person": (
        "多人镜头：参考图只给「这个人长什么样」，不给「他们怎么站」。"
        "关系必须写进 body——谁在画面左/右、各自朝向、有没有接触与遮挡，一个都不能省，例如：\n"
        "「<Picture 1> 的女生站在画面左侧，面朝右；<Picture 2> 的男生在右侧，半侧身面向她，"
        "右手搭在她左肩上，女生左肩被遮住一部分。」\n"
        "人一多脸就小：0.5MP 下建议用中近景，否则抬到 0.8~1.0MP（8GB 显存每段约 8 分钟）。"
    ),
    "length": "段时长只由顶层 seconds 决定（全片统一），不支持逐段不同时长。",
    "transition_switch": "把某段从 transition 改回 chain 时，上段尾帧可能没备进 ComfyUI input（转场段不共享尾帧），"
                        "本段会提示「缺上一段尾帧」并退化成不接首帧。补法：先重出上段，或在网页点该段「重出」让链路自己备好。",
    "auto_cut": "如果只给一整段长文本（或 segments 里只有 1 段很长的 body），会自动按空行 → 分隔线 → 句号 切成多段，"
                "每段约 130 字。想精确控制就在 segments 里自己分好。",
}


def build_template() -> dict:
    """返回一份可直接下载、照着改的模板（合法 JSON，注释放在 _ 开头的兄弟字段里）。"""
    seg1 = {
        "_说明": "第 1 段：不写 link（首段没有上一段）。head 只在顶层写一次，这里不用重复。",
        "title": "推门走出天台",
        "seed": 460211,
        "body": (
            "她缓缓推开一扇斑驳生锈的铁门，从门后走出来，铁门在身后缓慢回落。\n\n"
            "[机位] 摄影机在斜后方腰部高度轻微仰拍，人物位于画面偏左，"
            "极平稳地缓慢向前推进。\n\n"
            "[光线] 橙红夕阳沉在远处楼群之间，暖金色轮廓光勾出发丝与肩膀边缘。\n\n"
            "[场景] 粗糙灰色水泥天台，地面有浅浅积水，右前方灰白金属栏杆，栏杆外楼群层叠延伸到地平线。"
        ),
    }
    seg2 = {
        "_说明": "第 2 段：link=chain（默认）→ 第 0 帧接上段尾帧。这一段 Picture 编号会比第 1 段整体后移一位，"
                "但 head 里用的是 {pic:N}，会自动换算，所以依然不用重写。",
        "title": "走向栏杆",
        "seed": 460223,
        "link": {"mode": "chain", "transition": "dissolve"},
        "body": (
            "她从铁门边起步，缓步走向画面右侧的金属栏杆，右手抬起搭在栏杆上。\n\n"
            "[机位] 摄影机跟随她向右平移，保持中景，腰部高度。\n\n"
            "[光线] 与上段同一档橙红夕阳，只有云层亮度极轻微变化，绝不入夜、不转蓝调。"
        ),
    }
    seg3 = {
        "_说明": "第 3 段：link=transition + black → 独立生成（不接上段尾帧），拼接时用 0.8s 黑场切过来。适合换场/换时间。",
        "title": "入夜后的侧脸",
        "seed": 460237,
        "link": {"mode": "transition", "transition": "black"},
        "body": (
            "同一座天台的夜景，她侧身站在栏杆前，风吹动长发，侧脸被远处的城市灯光照亮。\n\n"
            "[机位] 固定机位近景侧脸特写，浅景深，背景灯点虚化成光斑。\n\n"
            "[光线] 蓝调夜景，冷色环境光 + 暖色城市灯点，人物侧脸有一道暖边光。"
        ),
    }
    return {
        "_说明": "⚠ 这是一份可以直接改的模板：改完存成 .json，回到网页点「导入分镜 JSON」→ 选这个文件 → 建片。"
                 "所有以 _ 开头的字段（_说明 / _字段说明 / _规则说明）都只是注释，不会被读取，可以整行删掉，也不影响导入。",
        "_字段说明": FIELD_DOC,
        "_规则说明": FIELD_NOTE,

        "title": "黄昏天台",
        "seconds": 5,
        "fps": 24,
        "megapixels": 0.5,
        "head": (
            "<Picture {pic:1}> 是人物参考，完整还原其中人物的脸型、五官、发型、服装与身材比例，"
            "全程不换人、不换装。\n\n"
            "<Picture {pic:plate}> 是全片唯一的场景空镜参考，环境结构、陈设与光源方向以它为准，保持不变。"
        ),
        "tail": (
            "写实电影摄影，35mm 镜头，柔和浅景深，真实光学散景，细腻胶片颗粒。"
            "画面中只有剧情里的人物，不出现文字、水印、字幕。\n\n"
            "[衔接] 段首约 0.5 秒动作与镜头保持稳定，段尾约 0.5 秒动作收住。"
        ),
        "ref_image": "",
        "ref_images": [],
        "bg_plate_path": "",
        "segments": [seg1, seg2, seg3],
    }
