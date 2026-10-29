import json
from pathlib import Path


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


def main():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    workflow = build_workflow(config)
    output = ROOT / "example_workflows" / "H3-Long-Video-Director.json"
    output.write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
