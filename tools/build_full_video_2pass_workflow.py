import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "example_workflows" / "minimax_h3_ui_source.json"
OUTPUT = ROOT / "example_workflows" / "H3-FullVideo-2Pass.json"


def get_node(workflow, node_id):
    node = next((item for item in workflow["nodes"] if item["id"] == node_id), None)
    if node is None:
        raise RuntimeError(f"Required source node {node_id} was not found")
    return node


def make_load_image_node(node_id, title, image, pos, order, output_link=None):
    return {
        "id": node_id,
        "type": "LoadImage",
        "pos": pos,
        "size": [260, 320],
        "flags": {},
        "order": order,
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
            {
                "name": "IMAGE",
                "type": "IMAGE",
                "links": [output_link] if output_link is not None else [],
            },
            {"name": "MASK", "type": "MASK", "links": []},
        ],
        "title": title,
        "properties": {"Node name for S&R": "LoadImage"},
        "widgets_values": [image, "image"],
        "color": "#2a363b",
        "bgcolor": "#3f5159",
    }


def build_workflow(source):
    workflow = copy.deepcopy(source)
    workflow["id"] = "12fdb441-a503-4fb2-b5f1-2ffb3afc9a43"
    workflow["revision"] = 0

    for node_id in (83, 90, 106, 107, 110, 113, 118, 114, 135):
        get_node(workflow, node_id)["mode"] = 0

    get_node(workflow, 126)["mode"] = 4

    video_loader = get_node(workflow, 135)
    video_loader["title"] = "上传完整视频（原片）"
    values = video_loader["widgets_values"]
    values["force_rate"] = 24
    values["custom_width"] = 0
    values["custom_height"] = 0
    values["frame_load_cap"] = 0
    values["skip_first_frames"] = 0
    values["select_every_nth"] = 1
    values["format"] = "AnimateDiff"
    if isinstance(values.get("videopreview"), dict):
        values["videopreview"]["hidden"] = True
        values["videopreview"]["params"] = {}

    h3 = get_node(workflow, 6)
    h3["title"] = "H3 完整视频参考 + 图片参考 + 提示词"
    h3["widgets_values"][0] = (
        "<Subject 1> is the same adult woman shown in <Picture 1>, <Picture 2>, "
        "and <Picture 3> through <Picture 9>.\n\n"
        "<Picture 1> is the primary identity, face, hairstyle, body proportion, "
        "and outfit reference.\n"
        "<Picture 2> through <Picture 9> supplement facial identity, hairstyle, "
        "outfit details, and viewing angles; do not mix in another person. Ignore "
        "any reference image background, unrelated clothing, or conflicting expression.\n\n"
        "<Video 1> is the complete source video. Preserve its subject motion, action "
        "timing, camera movement, framing rhythm, and scene continuity. Replace the "
        "source subject with <Subject 1> while keeping the source video's complete "
        "story and motion.\n\n"
        "Keep the same identity, hairstyle, facial proportions, and body proportions "
        "through the whole output. Preserve natural skin texture, cloth movement, and "
        "continuous camera motion. No cuts, subtitles, text, logos, or watermark."
    )

    length_expression = get_node(workflow, 40)
    length_expression["widgets_values"][0] = (
        "max(5, min(3600, a + (5 - (a % 17)) % 17))"
    )
    length_expression["inputs"][0]["link"] = 35
    length_expression["inputs"][0]["type"] = "INT"

    fps_node = get_node(workflow, 17)
    fps_node["outputs"][0]["links"] = []

    video_loader["outputs"][1]["links"] = [35, 198]

    upscaler = get_node(workflow, 118)
    upscaler["widgets_values"] = [
        "minimax_h3_latent_upscaler_3d_fp16.safetensors",
        "scale by multiplier",
        2.0,
        32,
        True,
        True,
        "cuda",
        "fp16",
    ]
    upscaler["widgets_values_named"] = {
        "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
        "mode": "scale by multiplier",
        "mode.scale": 2.0,
        "align": 32,
        "enable_temporal_chunking": True,
        "force_unload": True,
        "device": "cuda",
        "precision": "fp16",
    }

    output = get_node(workflow, 114)
    output["title"] = "完整视频二采输出"
    output["widgets_values"]["filename_prefix"] = "H3_FullVideo_2Pass/H3_fullvideo_"
    if isinstance(output["widgets_values"].get("videopreview"), dict):
        output["widgets_values"]["videopreview"]["hidden"] = False
        output["widgets_values"]["videopreview"]["params"] = {}

    links = workflow["links"]
    for link in links:
        if link[0] == 35:
            link[1] = 135
            link[2] = 1
            link[5] = "INT"
            break
    else:
        raise RuntimeError("Source length link 35 was not found")

    for link in links:
        if link[0] == 107:
            link[3] = 164
            link[4] = 0
            break
    else:
        raise RuntimeError("Decoded image output link 107 was not found")

    h3["inputs"].append(
        {
            "name": "ref_images.ref_image_8",
            "type": "IMAGE",
            "link": 201,
        }
    )
    ref_slots = {
        item["name"]: index
        for index, item in enumerate(h3["inputs"])
        if item["name"].startswith("ref_images.ref_image_")
    }
    h3["inputs"][ref_slots["ref_images.ref_image_6"]]["link"] = 199
    h3["inputs"][ref_slots["ref_images.ref_image_7"]]["link"] = 200
    h3["inputs"][ref_slots["ref_images.ref_image_8"]]["link"] = 201
    links.extend(
        [
            [197, 164, 0, 114, 0, "IMAGE"],
            [198, 135, 1, 164, 2, "INT"],
            [199, 166, 0, 6, ref_slots["ref_images.ref_image_6"], "IMAGE"],
            [200, 167, 0, 6, ref_slots["ref_images.ref_image_7"], "IMAGE"],
            [201, 168, 0, 6, ref_slots["ref_images.ref_image_8"], "IMAGE"],
        ]
    )
    get_node(workflow, 114)["inputs"][0]["link"] = 197

    reference_nodes = [
        (24, "参考图 1", "image.png", [-620, 6400], 32),
        (15, "参考图 2", "image (1).png", [-345, 6400], 33),
        (123, "参考图 3", "image (2).png", [-70, 6400], 34),
        (130, "参考图 4", "image (3).png", [205, 6400], 35),
        (132, "参考图 5", "image (4).png", [480, 6400], 36),
        (133, "参考图 6", "image (5).png", [755, 6400], 37),
    ]
    for node_id, title, image, pos, order in reference_nodes:
        node = get_node(workflow, node_id)
        node["mode"] = 0
        node["title"] = title
        node["widgets_values"] = [image, "image"]
        node["pos"] = pos
        node["order"] = order

    workflow["nodes"].extend(
        [
            make_load_image_node(
                166,
                "参考图 7",
                "image (6).png",
                [1030, 6400],
                38,
                199,
            ),
            make_load_image_node(
                167,
                "参考图 8",
                "image (6)_face_crop.png",
                [1305, 6400],
                39,
                200,
            ),
            make_load_image_node(
                168,
                "参考图 9",
                "image.png",
                [1580, 6400],
                40,
                201,
            ),
        ]
    )

    workflow["nodes"].append(
        {
            "id": 164,
            "type": "ImageFromBatch",
            "pos": [2720, 6050],
            "size": [240, 82],
            "flags": {},
            "order": 64,
            "mode": 0,
            "inputs": [
                {"name": "image", "type": "IMAGE", "link": 107},
                {"name": "batch_index", "type": "INT", "widget": {"name": "batch_index"}, "link": None},
                {"name": "length", "type": "INT", "widget": {"name": "length"}, "link": 198},
            ],
            "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [197]}],
            "title": "裁回源视频帧数",
            "properties": {"Node name for S&R": "ImageFromBatch"},
            "widgets_values": [0, 0],
            "color": "#233",
            "bgcolor": "#355",
        }
    )
    workflow["nodes"].append(
        {
            "id": 165,
            "type": "Note",
            "pos": [-430, 6800],
            "size": [620, 260],
            "flags": {},
            "order": 65,
            "mode": 0,
            "inputs": [],
            "outputs": [],
            "title": "使用说明",
            "properties": {},
            "widgets_values": [
                "1. 在“上传完整视频（原片）”中选择视频，force_rate=24。\n"
                "2. 在参考图1~9中选择人物、脸型、发型、服装和角度参考图。\n"
                "3. 修改 H3 节点提示词，用 <Picture 1> 和 <Video 1> 明确引用。\n"
                "4. 原视频完整帧数会自动传入 H3，并向上对齐到 17 帧格式。\n"
                "5. 二采时间分块已开启；最终会裁回源视频帧数。"
            ],
        }
    )
    workflow["last_node_id"] = max(node["id"] for node in workflow["nodes"])
    workflow["last_link_id"] = max(link[0] for link in workflow["links"])
    return workflow


def main():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    workflow = build_workflow(source)
    OUTPUT.write_text(
        json.dumps(workflow, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
