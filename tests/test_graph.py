import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import run_h3_long_video as pipeline
import director


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


if __name__ == "__main__":
    unittest.main()
