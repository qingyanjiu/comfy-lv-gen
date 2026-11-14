"""把各种分镜输入归一化成 SelfLift 的 segments 结构。

支持三种输入（网页「导入 JSON」和 CLI `import` 共用同一套规则）：

1. 已经是 SelfLift 结构：`{"segments":[{"body": "...", ...}], ...}`
2. 旧 Director rich storyboard：`segments[].prompt_segment / prompt_video / shot + open_action ...`
3. 只有一整段长文本（`prompt` / `text` / `content` / 纯字符串）→ 按空行或句号自动切分

输出是纯数据，不碰文件系统，方便单测。

**分层（2026-11-13）**：约束层唯一存放在 `assets/selflift.global.json`
（workflow / clip / fps / seconds / 画质 / 过场 / head / tail），用户上传的分镜
只写内容层（title / story / 任务参考 / segments）。传 `global_cfg` 给 `normalize()`
就会忽略上传分镜里的同名全局字段，避免约束出现第二份。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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

# ---------------------------------------------------------------- 分层配置
# 约束层唯一来源：改这里等于改所有片；用户上传的分镜里带同名字段一律忽略。
GLOBAL_CFG_PATH = Path(
    "D:/Comfy-Desktop/.workbuddy/skills/h3-long-video/assets/selflift.global.json"
)
# 由全局配置接管的字段（上传分镜里写了也不生效）
GLOBAL_KEYS = (
    "workflow", "clip_name", "fps", "seconds", "megapixels", "aspect_ratio",
    "quality", "join_mode", "join_dissolve_seconds", "join_black_seconds",
    "chain_first_frame", "head", "tail",
)
# 内容层字段：每片不同，只认上传分镜里的值
CONTENT_KEYS = ("title", "story", "ref_image", "ref_images",
                "bg_plate_path", "segments")

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


def load_global_cfg(path=None) -> dict:
    """读唯一一份全局约束配置，去掉 `_` 开头的说明字段。"""
    p = Path(path or GLOBAL_CFG_PATH)
    if not p.is_file():
        raise ValueError(f"找不到全局约束配置：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    cfg = {k: v for k, v in data.items() if not k.startswith("_")}
    for key in ("workflow", "head", "tail"):
        if not str(cfg.get(key) or "").strip():
            raise ValueError(f"全局配置缺必需字段 {key}：{p}")
    return cfg


def compose_head(global_head: str, story=None) -> str:
    """把上传分镜的 `story`（本片人物/场景设定）注入全局 head 的 {story} 槽。

    没写 story 就把整块「【本片设定】」删掉，不留一个空标题。
    """
    text = re.sub(r"\s+", " ", str(story or "").strip())
    if text:
        return global_head.replace("{story}", text)
    return "\n\n".join(
        block for block in global_head.split("\n\n") if "{story}" not in block
    )


STORY_HEADING = "【本片设定】"


def extract_story_from_head(head) -> str:
    """从旧 head 的【本片设定】块里抢救本片设定文字。

    老片 / 在网页 head 框里手写了设定的片没有 `story` 字段，套用全局配置时
    不能让这段内容凭空消失。带 `{story}` 占位符（还没注入）的块不算设定。
    """
    for block in str(head or "").split("\n\n"):
        if STORY_HEADING not in block:
            continue
        rest = block.split(STORY_HEADING, 1)[1].strip()
        if rest and "{story}" not in rest:
            return re.sub(r"\s+", " ", rest)
    return ""


def replace_story_in_head(head, story) -> str:
    """只替换 head 里的【本片设定】块，其余内容一字不动。

    用户可能在 head 框里做过本片微调，改 story 时不能把整份 head 重置成全局配置。
    没写 story 就删掉这一块，不留空标题。
    """
    blocks = [b.strip() for b in str(head or "").split("\n\n")
              if b.strip() and STORY_HEADING not in b]
    text = re.sub(r"\s+", " ", str(story or "").strip())
    if text:
        blocks.append(STORY_HEADING + "\n" + text)
    return "\n\n".join(blocks)


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
              target_chars=130, split_single=True, max_segments=16,
              global_cfg=None) -> dict:
    """把任意分镜输入归一化成 SelfLift 结构（不写文件）。

    传 `global_cfg` 时走分层模式：head/tail/时长/画质等约束层一律取自全局配置，
    上传分镜里的同名字段被忽略（只保留 title / story / 任务参考 / segments）。
    不传则保持旧行为（head/tail 走 DEFAULT_* 兜底），供纯文本预览等场合用。
    """
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

    # 分层模式下这些值一律来自全局配置，别去解析上传分镜里的同名字段——
    # 用户随手写个 "8 秒" 之类的字符串也不该让导入失败。
    if not global_cfg:
        seconds = float(seconds or meta.get("seconds")
                        or meta.get("seconds_per_segment") or 8.0)
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

    out = {
        "title": str(title or meta.get("title") or "未命名长片"),
        "ref_image": str(meta.get("ref_image") or ""),
        "ref_images": ref_list(meta.get("ref_images")),
        "bg_plate_path": str(meta.get("bg_plate_path") or ""),
        "segments": segments,
    }
    if global_cfg:
        # 约束层：整份照抄全局配置，上传分镜里的同名值一律不生效。
        out.update({k: v for k, v in global_cfg.items() if k in GLOBAL_KEYS})
        out["head"] = compose_head(global_cfg["head"], meta.get("story"))
        out["story"] = str(meta.get("story") or "").strip()
        out["_ignored"] = [k for k in GLOBAL_KEYS
                           if isinstance(meta, dict) and k in meta]
    else:
        out.update({
            "head": _clean(meta.get("head") or meta.get("style") or "") or DEFAULT_HEAD,
            "tail": _clean(meta.get("tail") or "") or DEFAULT_TAIL,
            "seconds": seconds,
            "fps": fps,
            "megapixels": megapixels,
        })
    return out


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
    "title": "片名。也是成片文件名与建片目录名的前缀。留空则用第一段标题。",
    "story": "【可选】本片专属设定：人物是谁、穿什么、场景在哪、光线基调、情绪走向。写一段话即可，"
             "会被注入全局 head 的「【本片设定】」槽位，全片每段自动带上，不用逐段重复。不写则该槽位整块删掉。\n"
             "⚠ 只写「本片是什么」，别写通用规矩（不换人、不融脸、段首承接、眨眼、禁止项…），那些已在全局配置里。",
    "ref_image": "【单人写法】人物参考图的「绝对路径」（D:/xxx/refs/girl.png）。留空则用网页上「上传参考图」传的那张。有多个人物请改用下面的 ref_images，两者同时存在时 ref_images 优先。",
    "ref_images": (
        "【多人写法 / 任务参考】人物参考图数组，顺序就是 Picture 编号顺序：第 1 个是 P1、第 2 个是 P2……\n"
        "每项写成 {\"path\": \"D:/xxx/refs/a.png\", \"comfy_name\": \"xx_a.png\", \"label\": \"女生 A\"}，"
        "comfy_name 可省（自动按「前缀_ref序号」生成），label 只用于网页显示。\n"
        "⚠ 一人一张，别把多个人拼进一张图——拼图后每张脸只占 1/4 画幅，谁都锁不住，"
        "而且模型会把整张图的四人并排构图当成模板去复刻。\n"
        "⚠ 参考图用「半身或胸像」，脸占画幅 1/3 以上；全身景脸只占 10~15% 高时锁不住具体的人。\n"
        "空镜板不占人物位，永远是最后一张（3 个人物 + 空镜板 → 人物 P1/P2/P3，空镜板 P4）。"
    ),
    "bg_plate_path": "【任务参考】空镜板（无人场景图）的「绝对路径」。留空则用网页「上传空镜板」，或不接空镜板。",
    "segments": "分段列表，顺序即播放顺序。每段至少要有 body。",

    "segments[].title": "段标题，只用于网页卡片与日志显示，不参与生成。",
    "segments[].body": "【必填／核心】只写这一段发生的事，推荐四块、空行隔开：动作 / [机位] / [光线] / [场景]，"
                       "8 秒约 100–160 字。完整写法见 docs/segment编写规范.md。\n"
                       "不要写「严格继承上一段真实尾帧」「不换人」「不融脸」「段尾收住」「眨眼频率」「画风」"
                       "这类公用约束——已在全局配置里，逐段重复反而会稀释权重。\n"
                       "指人物用 {pic:1} / {pic:2}（按本段接线自动换算），不要写死 <Picture 1>。",
    "segments[].seed": "该段随机种子（整数）。同一个 body 换 seed 就换一条演绎；不满意就换 seed 重出，别改 body。「别用 100000」（恶性种子，必出瑕疵）。留空则自动随机。",
    "segments[].link": "可选。本段与「上一段」怎么接。第 1 段忽略此字段。不写 = 默认链式承接（首帧=上段真实尾帧）。",
    "segments[].link.mode": "chain = 链式承接：本段第 0 帧 = 上段尾帧，动作连续、像素级接上（实测接缝 MAE 2~3）。transition = 转场：本段独立生成，不接上段尾帧，只靠拼接时叠化/黑屏过渡；改上游不会让你重出。",
    "segments[].link.transition": "仅 mode=transition 时生效。dissolve = 0.25s 交叉溶解（默认，最自然）；black = 0.8s 纯黑硬切（换场/换时间）；cut = 直接硬切（同场景切机位）。",
    "segments[].head": "【一般不用写】段级 head 会**整段覆盖**全局 head（不是追加），写了就等于放弃全片统一约束。"
                       "只有本段接线特殊、需要完全不同说明时才用。",
    "segments[].first_frame_override": "可选。用一张指定图当本段首帧，「优先于上段尾帧」。一般由网页「设为本段首帧」按钮自动写入，手改少用。",
    "segments[].last_frame_override": "可选。用一张指定图当本段尾帧（实验性，默认不用）。一般由网页「锁定本段尾帧」按钮写入。",

    "_全局约束（不在这里写）": "head / tail / 秒数 / 帧率 / 画幅 / 画质 / 过场 / 工作流全部由唯一一份全局配置决定，"
                         "所有片共用。上传分镜里即使写了这些字段也会被忽略——不用填，也不用手动套用。"
}

FIELD_NOTE = {
    "layering": (
        "【最重要】内容与约束是分开的：\n"
        "· 你上传的分镜 = 内容层：title / story（本片设定）/ 人物参考图 / 空镜板 / segments[].body。\n"
        "· 全局配置 = 约束层，只有一份，所有片共用，由维护者统一优化。\n"
        "· 上传分镜里写 head / tail / seconds 等全局字段会被**静默忽略**；跑片时系统自动把最新约束"
        "合并进本片，你不用填、不用看、也不用手动套用。\n"
        "· 但**画幅 / 分辨率 / 画质 / 每段秒数**这四项以本片为准：网页上改了就一直是改后的值，"
        "点「一键跑全片」不会被全局缺省值冲掉（全局只在这些字段缺失时才补）。\n"
        "· 全局约束已经覆盖：不换人 / 不融脸 / 严格继承上一段尾帧 / 段尾收住 / 眨眼频率 / catchlight / "
        "画风质感 / 禁止项——body 里一条都不要重复。\n"
        "· 工具条「导出内容 JSON」只导出内容层（片名 / 本片设定 / 参考图 / 分段），"
        "不含任何公用约束，可直接再导入建片。"
    ),
    "body_shape": (
        "一段 body 的推荐骨架，四块之间空一行：\n"
        "  动作（谁、从哪到哪、身体哪个部位在动、动完停在哪）\n"
        "  [机位] 位置 / 高度 / 侧别 / 景别 / 运动（固定、缓慢推进、同速跟拍）\n"
        "  [光线] 与上段一致，或这一场怎么变\n"
        "  [场景] 本段用得到的物件与方位，只写人与物件的接触关系\n"
        "8 秒一段约 100–160 字。一段只推进一件事，动作写「持续、缓慢、可见」的，"
        "别写猛地转身/快速奔跑（H3 出不来快动作，只会卡顿）。"
    ),
    "picture": (
        "body 里指人物用 {pic:1} / {pic:2} …，工具按本段实际接线自动换算成真实编号。\n"
        "链式段的首帧会占掉 P1、人物整体后移一位，所以**不要写死 <Picture 1>**，会指到首帧上。\n"
        "{pic:plate} = 空镜板，body 里一般不用。引用本段没接的序号会直接报错并列出实际接线。"
    ),
    "multi_person": (
        "多人镜头：参考图只给「这个人长什么样」，不给「他们怎么站」。"
        "关系必须写进 body——谁在画面左/右、各自朝向、有没有接触与遮挡，一个都不能省，例如：\n"
        "「{pic:1} 的女生站在画面左侧，面朝右；{pic:2} 的男生在右侧，半侧身面向她，"
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
    """返回一份可直接下载、照着改的【内容层】模板（合法 JSON，注释放 _ 开头字段里）。

    2026-11-13 分层后：约束层（head/tail/秒数/画质…）不再出现在这里，
    统一由 `assets/selflift.global.json` 提供，所有片共用一份。
    """
    seg1 = {
        "_说明": "第 1 段：不写 link（首段没有上一段）。body 只写这一段发生的事，别抄通用约束。",
        "title": "推门走出天台",
        "body": (
            "她缓缓推开一扇斑驳生锈的铁门，从门后走出来，铁门在身后缓慢回落。\n\n"
            "[机位] 摄影机在斜后方腰部高度轻微仰拍，人物位于画面偏左，"
            "极平稳地缓慢向前推进。\n\n"
            "[光线] 橙红夕阳沉在远处楼群之间，暖金色轮廓光勾出发丝与肩膀边缘。\n\n"
            "[场景] 粗糙灰色水泥天台，地面有浅浅积水，右前方灰白金属栏杆，栏杆外楼群层叠延伸到地平线。"
        ),
    }
    seg2 = {
        "_说明": "第 2 段：link=chain（默认）→ 第 0 帧自动接上段真实尾帧。"
                "「严格继承上一段」这句已写在全局 head 里，用可选占位符自动生效，这里不用写。",
        "title": "走向栏杆",
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
        "link": {"mode": "transition", "transition": "black"},
        "body": (
            "同一座天台的夜景，她侧身站在栏杆前，风吹动长发，侧脸被远处的城市灯光照亮。\n\n"
            "[机位] 固定机位近景侧脸特写，浅景深，背景灯点虚化成光斑。\n\n"
            "[光线] 蓝调夜景，冷色环境光 + 暖色城市灯点，人物侧脸有一道暖边光。"
        ),
    }
    return {
        "_说明": "⚠ 只写「内容层」：title / story（可选，本片设定）/ 人物参考图 / 空镜板 / segments[].body。"
                 "改完存成 .json，回到网页点「导入分镜 JSON」选它 → 建片。"
                 "body 写法（四块骨架、机位、连续性、多人关系、篇幅）见 docs/segment编写规范.md；"
                 "head/tail/秒数/画质等公用约束不在这里写，跑片时自动合并。"
                 "所有 _ 开头字段只是注释，不读、可整行删。",
        "_字段说明": FIELD_DOC,
        "_规则说明": FIELD_NOTE,

        "title": "黄昏天台",
        "story": "一位黑色长直发的年轻女生，穿米白色针织开衫与深色长裤。场景是黄昏的城市天台，"
                 "橙红夕阳到入夜深蓝的光线缓慢过渡，安静、克制、略带孤独的都市剧情片基调。",
        "ref_images": [],
        "bg_plate_path": "",
        "segments": [seg1, seg2, seg3],
    }
