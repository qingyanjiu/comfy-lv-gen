import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import run_h3_long_video as pipeline
import director
from tools.build_full_video_2pass_workflow import SOURCE, build_workflow


REPO = Path(__file__).resolve().parents[1]


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_dir.name)
        for name in self.config["reference_images"]:
            (self.temp / name).write_bytes(b"test image placeholder")
        self.config["comfy_input_dir"] = self.temp.as_posix()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_segment_has_no_reference_as_fixed_opening(self):
        segment = pipeline.segment_specs(self.config)[0]
        graph = pipeline.build_graph(
            self.config,
            segment["prompt"],
            None,
            None,
            123,
            segment["seconds"],
            False,
        )
        self.assertNotIn("first_frame", graph["30"]["inputs"])
        self.assertIn("ref_images.ref_image_0", graph["30"]["inputs"])

    def test_two_pass_graph_uses_compatible_upscale_path(self):
        segment = pipeline.segment_specs(self.config)[0]
        anchor = (self.temp / self.config["reference_images"][0]).as_posix()
        graph = pipeline.build_graph(
            self.config,
            segment["prompt"],
            anchor,
            anchor,
            123,
            segment["seconds"],
            True,
        )
        self.assertEqual(graph["43"]["inputs"]["mode"], "scale by multiplier")
        self.assertEqual(graph["43"]["inputs"]["mode.scale"], 2.0)
        self.assertEqual(graph["43"]["class_type"], "MinimaxH3LatentUpscaler3D")
        self.assertEqual(graph["47"]["class_type"], "MMH3UltimateUpscale")
        self.assertEqual(graph["36"]["inputs"]["samples"], ["47", 0])
        self.assertTrue(graph["43"]["inputs"]["enable_temporal_chunking"])
        self.assertIn("temporal_split_param", graph["47"]["inputs"])

    def test_full_video_workflow_enables_video_reference_and_two_pass(self):
        source = json.loads(SOURCE.read_text(encoding="utf-8"))
        workflow = build_workflow(source)
        nodes = {node["id"]: node for node in workflow["nodes"]}
        links = {link[0]: link for link in workflow["links"]}

        self.assertEqual(nodes[135]["mode"], 0)
        self.assertEqual(nodes[90]["mode"], 0)
        self.assertEqual(nodes[113]["mode"], 0)
        self.assertEqual(nodes[114]["mode"], 0)
        self.assertEqual(nodes[126]["mode"], 4)
        self.assertEqual(links[35][1:3], [135, 1])
        self.assertEqual(links[197][1:4], [164, 0, 114])
        self.assertEqual(links[198][1:4], [135, 1, 164])
        self.assertEqual(
            nodes[6]["inputs"][
                next(
                    index
                    for index, value in enumerate(nodes[6]["inputs"])
                    if value["name"] == "ref_videos.ref_video_0"
                )
            ]["link"],
            172,
        )

    def test_generation_signature_changes_with_prompt(self):
        first = pipeline.segment_specs(self.config)[0]
        changed = copy.deepcopy(first)
        changed["prompt"] += " changed"
        original_hash = pipeline.generation_signature(self.config, first, None)
        changed_hash = pipeline.generation_signature(self.config, changed, None)
        self.assertNotEqual(original_hash, changed_hash)
        self.assertEqual(len(original_hash), hashlib.sha1().digest_size * 2)

    def test_director_storyboard_accepts_arbitrary_segment_count(self):
        value = json.dumps(
            {
                "segments": [
                    {"seconds": 3, "two_pass": False, "prompt": "one"},
                    {"seconds": 5, "two_pass": True, "prompt": "two"},
                    {"seconds": 7, "two_pass": False, "prompt": "three"},
                ]
            }
        )
        segments = director._normalize_storyboard(value)
        self.assertEqual([item["seconds"] for item in segments], [3.0, 5.0, 7.0])
        self.assertEqual([item["two_pass"] for item in segments], [False, True, False])

    def test_director_global_two_pass_mode_overrides_segments(self):
        value = json.dumps(
            {
                "segments": [
                    {"seconds": 3, "two_pass": False, "prompt": "one"},
                    {"seconds": 5, "two_pass": False, "prompt": "two"},
                ]
            }
        )
        segments = director._normalize_storyboard(value, "全部二采")
        self.assertEqual([item["two_pass"] for item in segments], [True, True])

    def test_director_reference_notes_are_prepended_to_every_prompt(self):
        value = json.dumps(
            {
                "segments": [
                    {"seconds": 3, "two_pass": False, "prompt": "one"},
                    {"seconds": 5, "two_pass": False, "prompt": "two"},
                ]
            }
        )
        segments = director._normalize_storyboard(
            value,
            reference_notes="Picture 1 is the identity reference.",
        )
        self.assertTrue(
            all(
                item["prompt"].startswith("Picture 1 is the identity reference.")
                for item in segments
            )
        )

    def test_segment_can_disable_last_frame_composition_anchor(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "change outfit",
            }
        ]
        segment = pipeline.segment_specs(config)[0]
        self.assertFalse(pipeline.should_anchor_last_frame(config, segment))
        segment["hold_composition"] = None
        self.assertTrue(pipeline.should_anchor_last_frame(config, segment))

    def test_dorm_bus_shopping_example_is_exactly_one_minute(self):
        storyboard = json.loads(
            (REPO / "examples" / "dorm_bus_shopping_60s.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(storyboard["segments"]), 12)
        self.assertEqual(
            sum(segment["seconds"] for segment in storyboard["segments"]),
            60.0,
        )
        self.assertTrue(
            all(segment["two_pass"] is False for segment in storyboard["segments"])
        )
        self.assertFalse(storyboard["segments"][1]["hold_composition"])
        self.assertTrue(
            all("<女主角>" in segment["prompt"] for segment in storyboard["segments"])
        )
        self.assertIn("浅灰色居家短袖", storyboard["segments"][0]["prompt"])
        for segment in storyboard["segments"][1:]:
            self.assertNotIn("浅蓝色短款针织开衫", segment["prompt"])
            self.assertNotIn("深棕色高腰百褶裙", segment["prompt"])

    def test_director_workflow_links_reference_images(self):
        workflow = json.loads(
            (REPO / "example_workflows" / "H3-Long-Video-Director.json").read_text(
                encoding="utf-8"
            )
        )
        director_node = next(
            node for node in workflow["nodes"] if node["type"] == "H3LongVideoDirector"
        )
        self.assertEqual(len(workflow["links"]), 2)
        storyboard = json.loads(director_node["widgets_values"][0])
        self.assertEqual(len(storyboard["segments"]), len(self.config["segments"]))
        self.assertIn("Picture 1", director_node["widgets_values"][1])
        self.assertEqual(director_node["widgets_values"][2], self.config["steps"])
        self.assertEqual(
            director_node["widgets_values"][3], self.config["two_pass_steps"]
        )
        self.assertEqual(director_node["widgets_values"][4], self.config["sampler"])
        self.assertEqual(director_node["widgets_values"][5], self.config["scheduler"])
        self.assertEqual(
            director_node["widgets_values"][6], self.config["megapixels"]
        )
        self.assertEqual(
            director_node["widgets_values"][7], self.config["final_width"]
        )
        self.assertEqual(
            director_node["widgets_values"][8], self.config["final_height"]
        )
        self.assertEqual(director_node["widgets_values"][9], "跟随每段 JSON")


if __name__ == "__main__":
    unittest.main()
