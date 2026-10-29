import copy
import hashlib
import json
import tempfile
import unittest
from unittest import mock
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
        self.assertTrue(graph["43"]["inputs"]["enable_temporal_chunking"])
        self.assertIn("temporal_split_param", graph["47"]["inputs"])

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
        self.assertTrue(
            all(
                item["end_frame_prompt"].startswith(
                    "Picture 1 is the identity reference."
                )
                for item in segments
            )
        )

    def test_director_uses_explicit_end_frame_prompt(self):
        value = json.dumps(
            {
                "segments": [
                    {
                        "seconds": 5,
                        "prompt": "walk toward the door",
                        "end_frame_prompt": "standing at the open door",
                    }
                ]
            }
        )
        segment = director._normalize_storyboard(value)[0]
        self.assertEqual(segment["end_frame_prompt"], "standing at the open door")

    def test_director_resolves_fixed_and_random_seeds(self):
        self.assertEqual(director._resolve_seed(12345, False), 12345)
        with mock.patch.object(director.secrets, "randbits", return_value=987654):
            self.assertEqual(director._resolve_seed(12345, True), 987654)

    def test_first_segment_allows_zero_transition_seconds(self):
        value = json.dumps(
            {
                "segments": [
                    {
                        "seconds": 5,
                        "continue_from_previous": False,
                        "transition_seconds": 0,
                        "prompt": "first shot",
                    }
                ]
            }
        )
        segments = director._normalize_storyboard(value)
        self.assertEqual(segments[0]["transition_seconds"], 0)

    def test_later_cut_requires_positive_transition_seconds(self):
        value = json.dumps(
            {
                "segments": [
                    {"seconds": 5, "prompt": "first"},
                    {
                        "seconds": 5,
                        "continue_from_previous": False,
                        "transition_seconds": 0,
                        "prompt": "second",
                    },
                ]
            }
        )
        with self.assertRaises(ValueError):
            director._normalize_storyboard(value)

    def test_fade_transition_filters_are_centered_on_cut(self):
        video_filters, audio_filters = pipeline.build_fade_filters(
            [(5.0, 1.0)]
        )
        self.assertEqual(
            video_filters,
            [
                "fade=t=out:st=4.500000:d=0.500000:enable='between(t,4.500000,5.000000)'",
                "fade=t=in:st=5.000000:d=0.500000:enable='between(t,5.000000,5.500000)'",
            ],
        )
        self.assertEqual(
            audio_filters,
            [
                "afade=t=out:st=4.500000:d=0.500000:enable='between(t,4.500000,5.000000)'",
                "afade=t=in:st=5.000000:d=0.500000:enable='between(t,5.000000,5.500000)'",
            ],
        )

    def test_two_pass_repair_scales_both_streams_to_final_size(self):
        filter_complex = pipeline.build_two_pass_repair_filter(720, 1280, 2)
        self.assertIn("[0:v]scale=720:1280", filter_complex)
        self.assertIn("[1:v]scale=720:1280", filter_complex)
        self.assertIn("trim=end_frame=2", filter_complex)
        self.assertIn("trim=start_frame=2", filter_complex)

    def test_default_transition_mode_is_fade(self):
        self.assertEqual(self.config["transition_mode"], "fade")

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
        config["stabilize_full_body"] = True
        segment["hold_composition"] = None
        self.assertTrue(pipeline.should_anchor_last_frame(config, segment))

    def test_keyframe_graph_builds_first_and_last_targets(self):
        segment = pipeline.segment_specs(self.config)[0]
        graph = pipeline.build_keyframe_graph(
            self.config,
            segment["prompt"],
            None,
            123,
            2.0,
        )
        self.assertEqual(graph["38"]["class_type"], "SaveImage")
        self.assertEqual(graph["40"]["class_type"], "SaveImage")
        self.assertNotIn("last_frame", graph["30"]["inputs"])
        self.assertNotIn("first_frame", graph["30"]["inputs"])
        self.assertEqual(graph["27"]["inputs"]["megapixels"], 0.6)
        self.assertEqual(graph["34"]["inputs"]["steps"], 6)
        self.assertEqual(graph["30"]["inputs"]["ref_image_size"], "max")

    def test_keyframe_quality_controls_do_not_slow_video_generation(self):
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
        self.assertEqual(graph["27"]["inputs"]["megapixels"], 0.2)
        self.assertEqual(graph["34"]["inputs"]["steps"], 6)
        self.assertEqual(graph["30"]["inputs"]["ref_image_size"], "match")

    def test_keyframe_chain_reuses_previous_end_as_next_start(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "segment one",
            },
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": True,
                "prompt": "segment two",
            },
        ]
        specs = pipeline.segment_specs(config)
        state_path = self.temp / ".state.json"
        state = {}
        calls = []

        def fake_keyframe(
            config,
            segment,
            seed,
            first_frame=None,
            prompt=None,
            label=None,
        ):
            number = len(calls)
            first = self.temp / f"first_{number}.png"
            last = self.temp / f"last_{number}.png"
            first.write_bytes(b"first")
            last.write_bytes(b"last")
            calls.append((first_frame, prompt, label))
            return {
                "prompt_id": f"prompt-{number}",
                "first_frame": str(first),
                "last_frame": str(last),
            }

        with mock.patch.object(pipeline, "run_keyframe", side_effect=fake_keyframe):
            keyframes = pipeline.ensure_keyframe_chain(
                config,
                specs,
                state,
                state_path,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(keyframes[1]["start_frame"], keyframes[0]["end_frame"])
        self.assertEqual(keyframes[1]["end_frame"], keyframes[1]["start_frame"])
        self.assertTrue(state_path.is_file())
        self.assertEqual(
            keyframes[0]["end_frame"],
            keyframes[0]["opening_draft_last_frame"],
        )

    def test_storyboard_cut_generates_independent_next_start(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "segment one",
            },
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "continue_from_previous": False,
                "transition_seconds": 1.0,
                "prompt": "new camera angle",
            },
        ]
        specs = pipeline.segment_specs(config)
        state_path = self.temp / "cut_state.json"
        state = {}
        calls = []

        def fake_keyframe(
            config,
            segment,
            seed,
            first_frame=None,
            prompt=None,
            label=None,
        ):
            number = len(calls)
            first = self.temp / f"cut_first_{number}.png"
            last = self.temp / f"cut_last_{number}.png"
            first.write_bytes(b"first")
            last.write_bytes(b"last")
            calls.append((first_frame, prompt, label))
            return {
                "prompt_id": f"cut-prompt-{number}",
                "first_frame": str(first),
                "last_frame": str(last),
            }

        with mock.patch.object(pipeline, "run_keyframe", side_effect=fake_keyframe):
            keyframes = pipeline.ensure_keyframe_chain(
                config,
                specs,
                state,
                state_path,
            )

        self.assertEqual(len(calls), 3)
        self.assertIsNone(calls[1][0])
        self.assertFalse(keyframes[1]["continue_from_previous"])
        self.assertNotEqual(
            keyframes[1]["start_frame"],
            keyframes[0]["end_frame"],
        )
        self.assertFalse(
            pipeline.should_continue_from_previous(config, specs[1])
        )

    def test_custom_first_end_prompt_uses_a_second_keyframe_draft(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "walking forward",
                "end_frame_prompt": "standing at the mirror",
            }
        ]
        specs = pipeline.segment_specs(config)
        calls = []

        def fake_keyframe(
            config,
            segment,
            seed,
            first_frame=None,
            prompt=None,
            label=None,
        ):
            number = len(calls)
            first = self.temp / f"custom_first_{number}.png"
            last = self.temp / f"custom_last_{number}.png"
            first.write_bytes(b"first")
            last.write_bytes(b"last")
            calls.append(prompt)
            return {
                "prompt_id": f"custom-prompt-{number}",
                "first_frame": str(first),
                "last_frame": str(last),
            }

        with mock.patch.object(pipeline, "run_keyframe", side_effect=fake_keyframe):
            pipeline.ensure_keyframe_chain(
                config,
                specs,
                {},
                self.temp / "custom_state.json",
            )

        self.assertEqual(
            calls,
            ["walking forward", "standing at the mirror"],
        )

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
        self.assertEqual(len(workflow["links"]), 3)
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
        self.assertFalse(director_node["widgets_values"][10])
        self.assertEqual(
            director_node["widgets_values"][11], self.config["keyframe_steps"]
        )
        self.assertEqual(director_node["widgets_values"][12], "{}")
        self.assertEqual(
            director_node["widgets_values"][13], self.config["base_seed"]
        )
        self.assertTrue(director_node["widgets_values"][14])


if __name__ == "__main__":
    unittest.main()
