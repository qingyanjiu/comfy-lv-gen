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
        segments.append({
            "id": f"s{index:02d}",
            "title": _pick_title(source, index) if source else
                     (re.sub(r"\s+", " ", body)[:14] or f"第 {index} 段"),
            "body": body,
        })

    return {
        "title": str(title or meta.get("title") or "未命名长片"),
        "head": _clean(meta.get("head") or meta.get("style") or "") or DEFAULT_HEAD,
        "tail": _clean(meta.get("tail") or "") or DEFAULT_TAIL,
        "seconds": seconds,
        "fps": fps,
        "megapixels": megapixels,
        "segments": segments,
    }
