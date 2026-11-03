import json
from pathlib import Path

import director as h3_director


ROOT = Path(__file__).resolve().parents[1]


def load_image_node(node_id, title, filename, y, linked=True, order=None):
    return {
        "id": node_id,
        "type": "LoadImage",
        "pos": [-620, y],
        "size": [315, 314],
        "flags": {},
        "order": node_id - 1 if order is None else order,
        "mode": 0,
        "inputs": [
            {
                "localized_name": "image",
                "name": "image",
                "type": "COMBO",
                "widget": {"name": "image"},
                "link": None,
            },
            {
                "localized_name": "upload",
                "name": "upload",
                "type": "IMAGEUPLOAD",
                "widget": {"name": "upload"},
                "link": None,
            },
        ],
        "outputs": [
            {"name": "IMAGE", "type": "IMAGE", "links": [node_id] if linked else []},
            {"name": "MASK", "type": "MASK", "links": []},
        ],
        "title": title,
        "properties": {"Node name for S&R": "LoadImage"},
        "widgets_values": [filename, "image"],
    }


def build_workflow(config):
    storyboard = {
        "segments": [
            {
                "seconds": segment["seconds"],
                "two_pass": segment.get("two_pass", False),
                "prompt": segment["prompt"],
                **(
                    {"end_frame_prompt": segment["end_frame_prompt"]}
                    if segment.get("end_frame_prompt")
                    else {}
                ),
                **(
                    {"start_frame_prompt": segment["start_frame_prompt"]}
                    if segment.get("start_frame_prompt")
                    else {}
                ),
                **(
                    {"hold_composition": segment["hold_composition"]}
                    if segment.get("hold_composition") is not None
                    else {}
                ),
                **(
                    {
                        "continue_from_previous": segment[
                            "continue_from_previous"
                        ]
                    }
                    if segment.get("continue_from_previous") is not None
                    else {}
                ),
                **(
                    {"transition_seconds": segment["transition_seconds"]}
                    if segment.get("transition_seconds") is not None
                    else {}
                ),
                **(
                    {"transition_prompt": segment["transition_prompt"]}
                    if segment.get("transition_prompt")
                    else {}
                ),
            }
            for segment in config["segments"]
        ]
    }
    storyboard_text = json.dumps(storyboard, indent=2, ensure_ascii=False)
    reference_defaults = config["reference_images"]
    while len(reference_defaults) < 3:
        reference_defaults.append("image.png")

    nodes = [
        load_image_node(1, "参考图 1（必选）", reference_defaults[0], 80),
        load_image_node(2, "参考图 2", reference_defaults[1], 440),
        load_image_node(3, "参考图 3（可选）", reference_defaults[2], 800, linked=False),
        load_image_node(5, "首帧图（可选）", reference_defaults[0], 1160, order=3),
        {
            "id": 4,
            "type": "H3LongVideoDirector",
            "pos": [20, 80],
            "size": [840, 1120],
            "flags": {},
            "order": 4,
            "mode": 0,
            "inputs": [
                {
                    "localized_name": "reference_1",
                    "name": "reference_1",
                    "type": "IMAGE",
                    "link": 1,
                },
                {
                    "localized_name": "reference_2",
                    "name": "reference_2",
                    "shape": 7,
                    "type": "IMAGE",
                    "link": 2,
                },
                {
                    "localized_name": "reference_3",
                    "name": "reference_3",
                    "shape": 7,
                    "type": "IMAGE",
                    "link": None,
                },
                {
                    "localized_name": "first_frame",
                    "name": "first_frame",
                    "shape": 7,
                    "type": "IMAGE",
                    "link": 3,
                },
            ],
            "outputs": [
                {"name": "job_id", "type": "STRING", "links": []},
                {"name": "status", "type": "STRING", "links": []},
            ],
            "title": "MiniMax H3 Long Video Director",
            "properties": {
                "Node name for S&R": "H3LongVideoDirector",
            },
            "widgets_values": [
                storyboard_text,
                (
                    "Picture 1（参考图1）是<女主角>的主要身份、脸型、五官、发型和"
                    "身高比例基准；只有在剧情已经换好最终服装后才参考它的服装，"
                    "宿舍试穿阶段必须按每段提示词更换衣服。\n"
                    "Picture 2（参考图2）是中性表情脸型参考，用于稳定眼睛、"
                    "鼻子、脸型和闭嘴嘴宽；不要复制它的白色上衣和背景。\n"
                    "所有段落必须保持与这些参考图一致的<女主角>身份，"
                    "不得混入其他人物的脸或发型；服装状态严格跟随各段剧情。"
                ),
                config["steps"],
                config["two_pass_steps"],
                config["sampler"],
                config["scheduler"],
                config["megapixels"],
                config["final_width"],
                config["final_height"],
                "跟随每段 JSON",
                False,
                config.get("keyframe_steps", 6),
                "{}",
                config.get("base_seed", 0),
                True,
                h3_director.STAGE_REVIEW,
                "",
                "",
                h3_director.KEYFRAME_BACKEND_OPTIONS[0],
                h3_director.RESOLUTION_OPTIONS[0],
                h3_director.STATIC_COMPACT_HOLD_DEFAULT,
                True,  # confirm_reference_expand：先出扩图确认
            ],
        },
    ]
    return {
        "id": "6f3176cf-6d82-4ec5-b65f-127ae3cfd62f",
        "revision": 0,
        "last_node_id": 5,
        "last_link_id": 3,
        "nodes": nodes,
        "links": [
            [1, 1, 0, 4, 0, "IMAGE"],
            [2, 2, 0, 4, 1, "IMAGE"],
            [3, 5, 0, 4, 9, "IMAGE"],
        ],
        "groups": [],
        "config": {},
        "extra": {
            "ds": {"scale": 0.8, "offset": [700, 100]},
        },
        "version": 0.4,
    }


def build_single_clip_workflow(config):
    nodes = [
        load_image_node(101, "固定首帧", config["reference_images"][0], 0, linked=True),
        load_image_node(102, "固定尾帧", config["reference_images"][-1], 360, linked=True),
    ]
    nodes[0]["pos"] = [-1050, 0]
    nodes[0]["outputs"][0]["links"] = [1]
    nodes[1]["pos"] = [-1050, 360]
    nodes[1]["outputs"][0]["links"] = [2]

    reference_positions = [
        (-700, 0),
        (-380, 0),
        (-60, 0),
        (-700, 360),
        (-380, 360),
        (-60, 360),
        (-700, 720),
        (-380, 720),
        (-60, 720),
    ]
    reference_node_ids = []
    for index, position in enumerate(reference_positions, start=1):
        node_id = 110 + index - 1
        reference_node_ids.append(node_id)
        node = load_image_node(
            node_id,
            f"参考图 {index}",
            config["reference_images"][min(index - 1, len(config["reference_images"]) - 1)],
            position[1],
            linked=index <= 2,
        )
        node["pos"] = list(position)
        node["outputs"][0]["links"] = []
        if index == 1:
            node["outputs"][0]["links"] = [3]
        elif index == 2:
            node["outputs"][0]["links"] = [4]
        nodes.append(node)

    node_inputs = [
        {
            "localized_name": "first_frame",
            "name": "first_frame",
            "type": "IMAGE",
            "link": 1,
        },
        {
            "localized_name": "last_frame",
            "name": "last_frame",
            "type": "IMAGE",
            "link": 2,
        },
    ]
    for index in range(1, 10):
        node_inputs.append(
            {
                "localized_name": f"reference_{index}",
                "name": f"reference_{index}",
                "shape": 7 if index > 1 else None,
                "type": "IMAGE",
                "link": index + 2 if index <= 2 else None,
            }
        )
    for item in node_inputs:
        if item.get("shape") is None:
            item.pop("shape", None)

    prompt = (
        "<Picture1>、<Picture2> 分别定义本段首帧和尾帧构图。"
        "<Picture3> 起是女主角的身份、脸型、五官、发型和服装参考。\n"
        "<女主角>从首帧自然过渡到尾帧，保持同一人物身份、发型、服装和身体比例。"
        "9:16竖屏完整全身构图，摄影机不推进、不后退、不缩放，"
        "头顶、身体和鞋底始终完整入镜。步态自然，动作幅度克制。"
        "面部稳定，双唇自然闭合，嘴角平直，嘴宽与参考图中的中性表情一致；"
        "不微笑、不露牙、不张嘴、不咧嘴，嘴巴绝不突然放大。"
        "环境与人物接触关系真实，不穿模、不跳帧、不切镜，无文字、字幕和水印。"
    )
    director = {
        "id": 200,
        "type": "H3SingleClipDirector",
        "pos": [360, 0],
        "size": [640, 820],
        "flags": {},
        "order": 11,
        "mode": 0,
        "inputs": node_inputs,
        "outputs": [
            {"name": "job_id", "type": "STRING", "links": []},
            {"name": "status", "type": "STRING", "links": []},
        ],
        "title": "MiniMax H3 单段｜首尾帧 + 9 参考图",
        "properties": {
            "Node name for S&R": "H3SingleClipDirector",
        },
        "widgets_values": [
            prompt,
            config.get("single_clip_seconds", 5.0),
            config["steps"],
            config["two_pass_steps"],
            config["sampler"],
            config["scheduler"],
            config["megapixels"],
            config.get("single_clip_final_width", 720),
            config.get("single_clip_final_height", 1280),
            config.get("ref_image_size", "match"),
            False,
            config.get("base_seed", 0),
            True,
            "",
            h3_director.RESOLUTION_OPTIONS[0],
            h3_director.STATIC_COMPACT_HOLD_DEFAULT,
        ],
    }
    nodes.append(director)

    links = [
        [1, 101, 0, 200, 0, "IMAGE"],
        [2, 102, 0, 200, 1, "IMAGE"],
        [3, 110, 0, 200, 2, "IMAGE"],
        [4, 111, 0, 200, 3, "IMAGE"],
    ]
    return {
        "id": "3aa59dac-c45d-49b6-8df2-96b851875f70",
        "revision": 0,
        "last_node_id": 200,
        "last_link_id": 4,
        "nodes": nodes,
        "links": links,
        "groups": [
            {
                "id": 1,
                "title": "固定首帧 / 固定尾帧",
                "bounding": [-1090, -50, 330, 760],
                "color": "#8A8",
                "font_size": 24,
                "flags": {},
            },
            {
                "id": 2,
                "title": "参考图 1–9（只连接实际使用的图片）",
                "bounding": [-740, -50, 1000, 1120],
                "color": "#A88",
                "font_size": 24,
                "flags": {},
            },
        ],
        "config": {},
        "extra": {
            "ds": {"scale": 0.62, "offset": [1120, 120]},
        },
        "version": 0.4,
    }


def main():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    workflow = build_workflow(config)
    output = ROOT / "example_workflows" / "H3-Long-Video-Director.json"
    output.write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    single_clip = build_single_clip_workflow(config)
    single_clip_output = (
        ROOT / "example_workflows" / "H3-Single-Clip-FirstLastRefs.json"
    )
    single_clip_output.write_text(
        json.dumps(single_clip, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(output)
    print(single_clip_output)


if __name__ == "__main__":
    main()
