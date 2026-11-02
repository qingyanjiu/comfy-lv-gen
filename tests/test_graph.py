import copy
import hashlib
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import run_h3_long_video as pipeline
import director
from tools import build_director_workflow


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

    def test_tail_guard_leaves_spare_frames_on_exact_lengths(self):
        # 8 s is the pathological case: ceil(8*24) = 192 already satisfies the
        # 5-mod-17 rule, so without a guard the corrupt final frame would be
        # published as the clip's last frame.
        self.assertEqual(pipeline.h3_frame_count(8), 192)
        self.assertEqual(pipeline.output_frames(8), 192)
        guarded = pipeline.h3_frame_count(8, 2)
        self.assertEqual(guarded, 209)
        self.assertGreaterEqual(guarded - pipeline.output_frames(8), 2)

        # Durations that already round up keep their length, so the guard costs
        # nothing in the common case.
        for seconds in (5, 6, 9, 12, 15):
            self.assertEqual(
                pipeline.h3_frame_count(seconds, 2),
                pipeline.h3_frame_count(seconds),
                f"{seconds}s should not grow",
            )

    def test_build_graph_never_publishes_the_corrupt_tail_frame(self):
        segment = pipeline.segment_specs(self.config)[0]
        graph = pipeline.build_graph(
            self.config,
            segment["prompt"],
            None,
            None,
            123,
            8,
            False,
        )
        generated = graph["30"]["inputs"]["length"]
        published = graph["40"]["inputs"]["length"]
        self.assertEqual(published, pipeline.output_frames(8))
        self.assertGreater(generated, published)
        # Node 38 saves the frame that becomes the next segment's start anchor,
        # so it must point inside the published range.
        self.assertEqual(graph["38"]["inputs"]["batch_index"], published - 1)

        # Turning the guard off restores the old (buggy) behaviour, which is
        # what the regression is being pinned against.
        self.config["tail_guard_frames"] = 0
        unguarded = pipeline.build_graph(
            self.config, segment["prompt"], None, None, 123, 8, False
        )
        self.assertEqual(unguarded["30"]["inputs"]["length"], 192)

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

    def test_two_pass_repair_blends_anchor_into_opening(self):
        filter_complex = pipeline.build_two_pass_repair_filter(
            720, 1280, 2, blend_frames=4
        )
        self.assertIn("[0:v]scale=720:1280", filter_complex)
        self.assertIn("[1:v]scale=720:1280", filter_complex)
        self.assertIn("blend=shortest=1:all_expr=", filter_complex)
        # The anchor weight must decrease with the frame index, so the clip
        # hands over to the generated footage and *stays* there.
        self.assertIn("clip((N-2)/4,0,1)", filter_complex)
        self.assertNotIn("concat=n=2", filter_complex)

    def test_stitch_reuses_signature_for_unchanged_segments(self):
        import copy as copy_module

        config = copy_module.deepcopy(self.config)
        config["segments"] = [
            {"seconds": 5, "two_pass": False, "prompt": "single"}
        ]
        specs = pipeline.segment_specs(config)
        record = {
            "segment": 1,
            "video": (self.temp / "missing.mp4").as_posix(),
            "source_prompt_id": "abc",
            "generation_sha1": pipeline.generation_signature(
                config, specs[0], "abc"
            ),
        }
        state = {"segments": [record]}
        self.assertFalse(pipeline._all_segments_current(config, specs, state))
        record["video"] = (self.temp / "present.mp4").as_posix()
        (self.temp / "present.mp4").write_bytes(b"video")
        self.assertTrue(pipeline._all_segments_current(config, specs, state))
        record["generation_sha1"] = "stale"
        self.assertFalse(pipeline._all_segments_current(config, specs, state))

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
        self.assertEqual(graph["27"]["inputs"]["megapixels"], self.config["megapixels"])
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

    def _fake_keyframe_factory(self, prefix, calls=None, seeds=None):
        def fake_keyframe(
            config,
            segment,
            seed,
            first_frame=None,
            prompt=None,
            label=None,
        ):
            number = len(calls) if calls is not None else len(seeds)
            first = self.temp / f"{prefix}_first_{number}.png"
            last = self.temp / f"{prefix}_last_{number}.png"
            first.write_bytes(b"first")
            last.write_bytes(b"last")
            if calls is not None:
                calls.append((seed, first_frame, prompt, label))
            if seeds is not None:
                seeds.append(seed)
            return {
                "prompt_id": f"{prefix}-{number}",
                "first_frame": str(first),
                "last_frame": str(last),
            }

        return fake_keyframe

    def test_keyframe_chain_uses_manual_override(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "segment one",
            }
        ]
        specs = pipeline.segment_specs(config)
        manual_end = self.temp / "manual_end.png"
        manual_end.write_bytes(b"manual")
        state = {"keyframe_overrides": {"1": {"end": manual_end.as_posix()}}}
        calls = []
        with mock.patch.object(
            pipeline,
            "run_keyframe",
            side_effect=self._fake_keyframe_factory("override", calls=calls),
        ):
            keyframes = pipeline.ensure_keyframe_chain(
                config,
                specs,
                state,
                self.temp / "override_state.json",
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(keyframes[0]["end_frame"], manual_end.as_posix())
        self.assertEqual(keyframes[0]["end_source"], "manual")
        self.assertEqual(state["keyframes"][0]["end_source"], "manual")

    def test_keyframe_reroll_regenerates_with_a_fresh_seed(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 5,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "segment one",
            }
        ]
        specs = pipeline.segment_specs(config)
        stale_start = self.temp / "stale_start.png"
        stale_end = self.temp / "stale_end.png"
        stale_start.write_bytes(b"start")
        stale_end.write_bytes(b"end")
        state = {
            "keyframes": [
                {
                    "segment": 1,
                    "start_frame": stale_start.as_posix(),
                    "end_frame": stale_end.as_posix(),
                    "start_sha1": "stale",
                    "end_sha1": "",
                }
            ],
            "keyframe_attempts": {"1": {"end": 2}},
        }
        seeds = []
        with mock.patch.object(
            pipeline,
            "run_keyframe",
            side_effect=self._fake_keyframe_factory("reroll", seeds=seeds),
        ):
            pipeline.ensure_keyframe_chain(
                config,
                specs,
                state,
                self.temp / "reroll_state.json",
            )
        self.assertEqual(len(seeds), 2)
        self.assertEqual(
            seeds[1],
            int(config["base_seed"]) + 700000 + 2 * 101,
        )

    def test_manual_keyframe_and_source_helpers(self):
        target = self.temp / "user.png"
        target.write_bytes(b"user")
        overrides = {"2": {"start": target.as_posix()}}
        self.assertEqual(
            pipeline.manual_keyframe(overrides, 2, "start"),
            target.as_posix(),
        )
        self.assertIsNone(pipeline.manual_keyframe(overrides, 2, "end"))
        self.assertIsNone(pipeline.manual_keyframe({}, 2, "start"))
        self.assertEqual(pipeline.keyframe_source("manual:/x.png"), "manual")
        self.assertEqual(pipeline.keyframe_source("hold:abc"), "hold")
        self.assertEqual(pipeline.keyframe_source("deadbeef"), "ai")

    def test_two_pass_repair_ramp_pins_then_hands_over_to_footage(self):
        # Frame 0..fix_frames-1 = the static anchor, then the anchor fades out
        # over blend_frames frames and the generated footage owns the clip.
        # The first version of this ramp was inverted: it faded *to* the anchor
        # and pinned it forever, so every two-pass segment became a slideshow.
        weights = [pipeline.repair_blend_weight(2, 4, n) for n in range(10)]
        self.assertEqual(weights[:3], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(weights[3], 0.25)
        self.assertAlmostEqual(weights[4], 0.5)
        self.assertAlmostEqual(weights[5], 0.75)
        self.assertEqual(weights[6:], [1.0, 1.0, 1.0, 1.0])

        graph = pipeline.build_two_pass_repair_filter(832, 1472, 2, 4)
        self.assertIn("clip((N-2)/4,0,1)", graph)
        self.assertNotIn("2+4-N", graph)

    def test_two_pass_repair_without_blend_trims_the_anchor(self):
        graph = pipeline.build_two_pass_repair_filter(832, 1472, 2, 0)
        self.assertIn("trim=end_frame=2", graph)
        self.assertIn("trim=start_frame=2", graph)
        self.assertIn("concat=n=2", graph)
        self.assertEqual(pipeline.repair_blend_weight(2, 0, 1), 0.0)
        self.assertEqual(pipeline.repair_blend_weight(2, 0, 2), 1.0)

    def test_end_keyframe_prompt_keeps_the_camera_continuous_by_default(self):
        cfg = {"keyframe_reanchor_every": 0}
        prompt = pipeline.end_keyframe_prompt(cfg, "she sits down", 0)
        self.assertTrue(prompt.startswith("she sits down"))
        self.assertIn(pipeline.DEFAULT_KEYFRAME_CAMERA_PROMPT, prompt)
        self.assertNotIn(pipeline.DEFAULT_KEYFRAME_REANCHOR_PROMPT, prompt)

    def test_end_keyframe_prompt_reanchors_only_on_selected_segments(self):
        cfg = {"keyframe_reanchor_every": 2, "keyframe_camera_continuity": False}
        # Re-anchor fires on segment 2, 4, ... (1-based), lock is off.
        self.assertEqual(
            pipeline.end_keyframe_prompt(cfg, "p", 0), "p"
        )
        second = pipeline.end_keyframe_prompt(cfg, "p", 1)
        self.assertIn(pipeline.DEFAULT_KEYFRAME_REANCHOR_PROMPT, second)
        self.assertNotIn(pipeline.DEFAULT_KEYFRAME_CAMERA_PROMPT, second)
        # The camera note is appended after the re-anchor so it wins the tail weighting.
        combined = pipeline.end_keyframe_prompt(
            {"keyframe_reanchor_every": 1, "keyframe_camera_continuity": True}, "p", 0
        )
        self.assertLess(
            combined.index(pipeline.DEFAULT_KEYFRAME_REANCHOR_PROMPT),
            combined.index(pipeline.DEFAULT_KEYFRAME_CAMERA_PROMPT),
        )

    def test_end_keyframe_prompt_honours_custom_camera_text(self):
        prompt = pipeline.end_keyframe_prompt(
            {
                "keyframe_camera_continuity": True,
                "keyframe_camera_prompt": "same lens please",
                "keyframe_reanchor_every": 0,
            },
            "p",
            0,
        )
        self.assertTrue(prompt.endswith("same lens please"))

    def _free_end_config(self):
        config = copy.deepcopy(self.config)
        config["segments"] = [
            {
                "seconds": 15,
                "two_pass": False,
                "hold_composition": False,
                "free_end": True,
                "prompt": "she walks all the way to the bench",
            },
            {
                "seconds": 6,
                "two_pass": False,
                "hold_composition": False,
                "prompt": "she sits down",
            },
        ]
        return config

    def test_video_only_options_do_not_invalidate_the_keyframes(self):
        spec = {"seconds": 15, "two_pass": True, "prompt": "she walks"}
        base = dict(self.config)
        base.pop("video_camera_continuity", None)
        base.pop("video_camera_prompt", None)
        toggled = dict(base)
        toggled["video_camera_continuity"] = True
        toggled["video_camera_prompt"] = "Tracking shot, keep the full body."
        self.assertEqual(
            pipeline.generation_signature(base, spec, "kf"),
            pipeline.generation_signature(toggled, spec, "kf"),
        )
        # ... but the segment prompt itself does change the video signature.
        self.assertNotEqual(
            pipeline.generation_signature(base, spec, "kf"),
            pipeline.generation_signature(
                base, pipeline.video_prompt_spec(toggled, spec), "kf"
            ),
        )

    def test_video_prompt_carries_the_camera_note_only_when_enabled(self):
        spec = {"seconds": 15, "two_pass": True, "prompt": "she walks to the bench"}
        off = {"video_camera_continuity": False}
        self.assertIs(pipeline.video_prompt_spec(off, spec), spec)

        on = {
            "video_camera_continuity": True,
            "video_camera_prompt": "Tracking shot, keep the full body in frame.",
        }
        patched = pipeline.video_prompt_spec(on, spec)
        self.assertEqual(
            patched["prompt"],
            "she walks to the bench\n\nTracking shot, keep the full body in frame.",
        )
        # The original spec is untouched, so the keyframe prompt is unaffected.
        self.assertEqual(spec["prompt"], "she walks to the bench")
        self.assertTrue(patched["two_pass"])

        # Applying it twice must not stack the note.
        again = pipeline.video_prompt_spec(on, patched)
        self.assertEqual(again["prompt"], patched["prompt"])

        # An empty note leaves the prompt alone.
        self.assertIs(
            pipeline.video_prompt_spec(
                {"video_camera_continuity": True, "video_camera_prompt": "  "}, spec
            ),
            spec,
        )

    def test_free_end_segment_skips_the_end_keyframe(self):
        config = self._free_end_config()
        specs = pipeline.segment_specs(config)
        self.assertTrue(specs[0]["free_end"])
        self.assertFalse(specs[1]["free_end"])

        calls = []

        def fake_keyframe(
            config, segment, seed, first_frame=None, prompt=None, label=None
        ):
            number = len(calls)
            first = self.temp / f"free_first_{number}.png"
            last = self.temp / f"free_last_{number}.png"
            first.write_bytes(b"first")
            last.write_bytes(b"last")
            calls.append((first_frame, prompt))
            return {
                "prompt_id": f"free-{number}",
                "first_frame": str(first),
                "last_frame": str(last),
            }

        state = {}
        with mock.patch.object(pipeline, "run_keyframe", side_effect=fake_keyframe):
            keyframes = pipeline.ensure_keyframe_chain(
                config, specs, state, self.temp / "free_state.json"
            )

        # Only the opening start frame is rendered: no end draft at all.
        self.assertEqual(len(calls), 1)
        self.assertIsNone(keyframes[0]["end_frame"])
        self.assertEqual(keyframes[0]["end_source"], "free")
        self.assertTrue(keyframes[0]["free_end"])
        # The next segment cannot inherit a frame from a free end.
        self.assertIsNone(keyframes[1]["start_frame"])
        self.assertEqual(keyframes[1]["start_source"], "pending-video")
        self.assertIsNone(keyframes[1]["end_frame"])

    def test_free_end_hands_its_rendered_tail_to_the_next_segment(self):
        config = self._free_end_config()
        specs = pipeline.segment_specs(config)
        tail = self.temp / "seg1_last_frame.png"
        tail.write_bytes(b"tail")
        state = {
            "segments": [
                {
                    "segment": 1,
                    "video": str(self.temp / "seg1.mp4"),
                    "last_frame": str(tail),
                    "next_frame": str(tail),
                }
            ]
        }

        def fake_keyframe(
            config, segment, seed, first_frame=None, prompt=None, label=None
        ):
            first = self.temp / "only_start.png"
            first.write_bytes(b"first")
            return {
                "prompt_id": "opening",
                "first_frame": str(first),
                "last_frame": str(self.temp / "opening_last.png"),
            }

        with mock.patch.object(pipeline, "run_keyframe", side_effect=fake_keyframe):
            keyframes = pipeline.ensure_keyframe_chain(
                config, specs, state, self.temp / "free_tail_state.json"
            )

        self.assertEqual(keyframes[1]["start_frame"], str(tail))
        self.assertEqual(keyframes[1]["start_source"], "from-video")
        # Once the start is known, the next segment gets a real end keyframe.
        self.assertIsNotNone(keyframes[1]["end_frame"])

    def test_missing_keyframes_ignores_free_and_pending_frames(self):
        tail = self.temp / "tail.png"
        tail.write_bytes(b"tail")
        state = {
            "keyframes": [
                {
                    "segment": 1,
                    "start_frame": tail.as_posix(),
                    "end_frame": None,
                    "start_source": "ai",
                    "end_source": "free",
                },
                {
                    "segment": 2,
                    "start_frame": None,
                    "end_frame": None,
                    "start_source": "pending-video",
                    "end_source": "pending-video",
                },
            ]
        }
        self.assertEqual(pipeline.missing_keyframes(state, 2), [])

    def test_missing_keyframes_lists_absent_segments(self):
        existing = self.temp / "exists.png"
        existing.write_bytes(b"frame")
        state = {
            "keyframes": [
                {
                    "segment": 1,
                    "start_frame": existing.as_posix(),
                    "end_frame": existing.as_posix(),
                },
                {
                    "segment": 2,
                    "start_frame": (self.temp / "gone.png").as_posix(),
                    "end_frame": existing.as_posix(),
                },
            ]
        }
        self.assertEqual(pipeline.missing_keyframes(state, 3), [2, 3])
        self.assertEqual(pipeline.missing_keyframes(state, 1), [])

    def test_job_keyframes_expose_pending_reroll_state(self):
        frame = self.temp / "kf.png"
        frame.write_bytes(b"frame")
        job_dir = self.temp / "job"
        job_dir.mkdir()
        (job_dir / ".state.json").write_text(
            json.dumps(
                {
                    "keyframes": [
                        {
                            "segment": 1,
                            "start_frame": frame.as_posix(),
                            "end_frame": frame.as_posix(),
                            "start_sha1": "aaaaaaaa1111",
                            "end_sha1": "",
                        }
                    ],
                    "keyframe_attempts": {"1": {"end": 3}},
                }
            ),
            encoding="utf-8",
        )
        keyframes = director._job_keyframes(job_dir, "job")
        self.assertEqual(len(keyframes), 1)
        item = keyframes[0]
        self.assertFalse(item["start_pending"])
        self.assertTrue(item["end_pending"])
        self.assertEqual(item["start_attempt"], 0)
        self.assertEqual(item["end_attempt"], 3)
        # The URL carries a version token so a regenerated or replaced frame is
        # never served from the browser cache.
        self.assertIn("?v=", item["start_url"])
        self.assertIn("?v=", item["end_url"])
        self.assertNotEqual(item["start_url"], item["end_url"])

    def test_previous_video_tail_prefers_the_rendered_last_frame(self):
        tail = self.temp / "prev_tail.png"
        tail.write_bytes(b"tail")
        state = {
            "segments": [
                {"segment": 1, "last_frame": str(self.temp / "gone.png")},
                {"segment": 2, "last_frame": str(tail)},
            ]
        }
        # Asking for segment 3 looks at the record of segment 2.
        self.assertEqual(
            pipeline.previous_video_tail(state, 3), str(tail)
        )
        # Missing records / files return None so the caller can raise.
        self.assertIsNone(pipeline.previous_video_tail(state, 2))
        self.assertIsNone(pipeline.previous_video_tail({}, 2))

    def _write_job_keyframes(self, count=2, *, pending=()):
        """Create a job dir whose keyframe frames all exist on disk."""
        job_dir = self.temp / f"job_{count}_{len(pending)}"
        job_dir.mkdir(exist_ok=True)
        records = []
        for index in range(1, count + 1):
            frame = job_dir / f"kf{index}.png"
            frame.write_bytes(f"frame{index}".encode("utf-8"))
            records.append(
                {
                    "segment": index,
                    "start_frame": frame.as_posix(),
                    "end_frame": frame.as_posix(),
                    "start_sha1": "" if ("start", index) in pending else f"s{index}",
                    "end_sha1": "" if ("end", index) in pending else f"e{index}",
                }
            )
        (job_dir / ".state.json").write_text(
            json.dumps({"keyframes": records}), encoding="utf-8"
        )
        return job_dir

    def test_job_keyframes_keeps_free_end_cards_with_a_placeholder(self):
        start = self.temp / "free_start.png"
        start.write_bytes(b"start")
        job_dir = self.temp / "free_job"
        job_dir.mkdir()
        (job_dir / ".state.json").write_text(
            json.dumps(
                {
                    "keyframes": [
                        {
                            "segment": 1,
                            "start_frame": start.as_posix(),
                            "end_frame": None,
                            "start_sha1": "s1",
                            "end_sha1": "free:s1",
                            "start_source": "ai",
                            "end_source": "free",
                            "free_end": True,
                        },
                        {
                            "segment": 2,
                            "start_frame": None,
                            "end_frame": None,
                            "start_sha1": "pending-video:2",
                            "end_sha1": "pending-video:end:2",
                            "start_source": "pending-video",
                            "end_source": "pending-video",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        keyframes = director._job_keyframes(job_dir, "job")
        self.assertEqual(len(keyframes), 2)
        first, second = keyframes
        self.assertTrue(first["start_available"])
        self.assertFalse(first["end_available"])
        self.assertIsNone(first["end_url"])
        self.assertEqual(first["end_source"], "free")
        self.assertTrue(first["free_end"])
        self.assertFalse(second["start_available"])
        self.assertIsNone(second["start_url"])
        self.assertEqual(second["start_source"], "pending-video")

    def test_keyframes_ready_requires_every_frame_on_disk(self):
        job_dir = self._write_job_keyframes(2)
        keyframes = director._job_keyframes(job_dir, "job")
        self.assertTrue(director._keyframes_ready(keyframes, 2))
        # Fewer frames than the storyboard asks for is not "ready".
        self.assertFalse(director._keyframes_ready(keyframes, 3))
        self.assertFalse(director._keyframes_ready([], 2))

        pending_dir = self._write_job_keyframes(2, pending=[("end", 2)])
        pending = director._job_keyframes(pending_dir, "job")
        self.assertFalse(director._keyframes_ready(pending, 2))

    def test_segment_total_falls_back_to_state_then_keyframes(self):
        keyframes = [{"index": 1}, {"index": 2}]
        self.assertEqual(director._segment_total({"segments": [{}, {}]}, {}, keyframes), 2)
        self.assertEqual(
            director._segment_total({}, {"review_segments": 5}, keyframes), 5
        )
        self.assertEqual(
            director._segment_total({}, {"keyframe_total": 4}, keyframes), 4
        )
        self.assertEqual(director._segment_total({}, {}, keyframes), 2)
        self.assertEqual(director._segment_total({}, {}, []), 0)

    def test_review_state_offers_resume_when_keyframes_are_complete(self):
        # The reported bug: a reloaded job that died while rendering videos has
        # a complete keyframe set but awaiting_review == False. It must still
        # offer a button instead of leaving the panel with no action.
        job_dir = self._write_job_keyframes(2)
        keyframes = director._job_keyframes(job_dir, "job")
        status = {"status": "interrupted"}
        self.assertEqual(
            director._review_state(status, {}, keyframes, 2, 0), "resume"
        )
        # Legacy jobs (no awaiting_review flag at all) behave the same way.
        self.assertEqual(
            director._review_state({"status": "completed"}, {}, keyframes, 2, 1),
            "resume",
        )
        # Nothing left to render -> no banner.
        self.assertIsNone(
            director._review_state({"status": "completed"}, {}, keyframes, 2, 2)
        )
        # A live worker must never be told to continue.
        self.assertIsNone(
            director._review_state({"status": "running"}, {}, keyframes, 2, 0)
        )

    def test_review_state_keeps_the_gate_and_offers_keyframe_recovery(self):
        job_dir = self._write_job_keyframes(2)
        keyframes = director._job_keyframes(job_dir, "job")
        self.assertEqual(
            director._review_state(
                {"status": "awaiting_review"},
                {"awaiting_review": True},
                keyframes,
                2,
                0,
            ),
            "gate",
        )
        # Failed before any keyframe existed -> offer to finish the keyframes.
        self.assertEqual(
            director._review_state({"status": "failed"}, {}, [], 3, 0),
            "keyframes",
        )
        # A job that never ran keeps whatever ComfyUI's own queue button does.
        self.assertIsNone(
            director._review_state({"status": "unknown"}, {}, [], 3, 0)
        )

    def test_chained_start_frame_is_served_from_the_previous_end(self):
        first_start = self.temp / "s1.png"
        shared = self.temp / "shared.png"
        last = self.temp / "e2.png"
        for path in (first_start, shared, last):
            path.write_bytes(path.name.encode("utf-8"))
        job_dir = self.temp / "job"
        job_dir.mkdir()
        (job_dir / ".state.json").write_text(
            json.dumps(
                {
                    "keyframes": [
                        {
                            "segment": 1,
                            "start_frame": first_start.as_posix(),
                            "end_frame": shared.as_posix(),
                            "start_sha1": "s1sig",
                            "end_sha1": "sharedsig",
                            "continue_from_previous": False,
                        },
                        {
                            "segment": 2,
                            "start_frame": shared.as_posix(),
                            "end_frame": last.as_posix(),
                            "start_sha1": "sharedsig",
                            "end_sha1": "endsig",
                            "continue_from_previous": True,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        keyframes = director._job_keyframes(job_dir, "job")
        self.assertEqual(len(keyframes), 2)
        self.assertFalse(keyframes[0]["start_inherited"])
        # Segment 2's start frame is literally segment 1's end frame, so it must
        # be served through that segment: re-rendering the upstream end frame
        # then updates this card immediately instead of leaving a stale copy.
        self.assertTrue(keyframes[1]["start_inherited"])
        self.assertEqual(keyframes[1]["start_url"], keyframes[0]["end_url"])

    def test_non_chained_start_frame_keeps_its_own_media_url(self):
        own_start = self.temp / "own.png"
        own_end = self.temp / "own_end.png"
        for path in (own_start, own_end):
            path.write_bytes(path.name.encode("utf-8"))
        job_dir = self.temp / "job"
        job_dir.mkdir()
        (job_dir / ".state.json").write_text(
            json.dumps(
                {
                    "keyframes": [
                        {
                            "segment": 1,
                            "start_frame": own_start.as_posix(),
                            "end_frame": own_end.as_posix(),
                            "start_sha1": "asig",
                            "end_sha1": "bsig",
                            "continue_from_previous": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        item = director._job_keyframes(job_dir, "job")[0]
        self.assertFalse(item["start_inherited"])
        self.assertIn("/keyframe/1/start", item["start_url"])

    def test_cut_boundary_keeps_both_frames_independent(self):
        first_start = self.temp / "cut_s1.png"
        first_end = self.temp / "cut_e1.png"
        cut_start = self.temp / "cut_s2.png"
        cut_end = self.temp / "cut_e2.png"
        for path in (first_start, first_end, cut_start, cut_end):
            path.write_bytes(path.name.encode("utf-8"))
        job_dir = self.temp / "job"
        job_dir.mkdir()
        (job_dir / ".state.json").write_text(
            json.dumps(
                {
                    "keyframes": [
                        {
                            "segment": 1,
                            "start_frame": first_start.as_posix(),
                            "end_frame": first_end.as_posix(),
                            "start_sha1": "s1",
                            "end_sha1": "e1",
                            "continue_from_previous": False,
                        },
                        {
                            "segment": 2,
                            "start_frame": cut_start.as_posix(),
                            "end_frame": cut_end.as_posix(),
                            "start_sha1": "s2",
                            "end_sha1": "e2",
                            "continue_from_previous": False,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        keyframes = director._job_keyframes(job_dir, "job")
        # A cut renders its own opening frame; it must never be folded into the
        # previous segment's end frame.
        self.assertFalse(keyframes[1]["chain"])
        self.assertFalse(keyframes[1]["start_inherited"])
        self.assertNotEqual(keyframes[1]["start_url"], keyframes[0]["end_url"])
        self.assertIn("/keyframe/2/start", keyframes[1]["start_url"])

    def test_director_node_exposes_stage_controls(self):
        inputs = director.H3LongVideoDirector.INPUT_TYPES()
        self.assertIn("stage", inputs["optional"])
        self.assertIn("resume_job_id", inputs["optional"])
        self.assertEqual(
            inputs["optional"]["stage"][0],
            [director.STAGE_REVIEW, director.STAGE_ALL, director.STAGE_VIDEOS],
        )
        self.assertEqual(
            director.STAGE_TO_FLAG[director.STAGE_REVIEW],
            "keyframes",
        )
        self.assertEqual(director.STAGE_TO_FLAG[director.STAGE_VIDEOS], "videos")
        self.assertEqual(director.STAGE_TO_FLAG[director.STAGE_ALL], "all")

    def test_director_workflow_carries_the_stage_widget(self):
        workflow = json.loads(
            (REPO / "example_workflows" / "H3-Long-Video-Director.json").read_text(
                encoding="utf-8"
            )
        )
        node = next(
            item
            for item in workflow["nodes"]
            if item["type"] == "H3LongVideoDirector"
        )
        self.assertEqual(node["widgets_values"][15], director.STAGE_REVIEW)
        self.assertEqual(node["widgets_values"][16], "")

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

    def test_single_clip_graph_uses_fixed_first_last_and_nine_references(self):
        config = copy.deepcopy(self.config)
        config["reference_images"] = [
            f"reference_{index}.png" for index in range(1, 10)
        ]
        for name in config["reference_images"]:
            (self.temp / name).write_bytes(b"test image placeholder")
        graph = pipeline.build_graph(
            config,
            "single prompt",
            config["reference_images"][0],
            config["reference_images"][1],
            123,
            5,
            False,
        )
        self.assertEqual(graph["1"]["class_type"], "VHS_LoadImagePath")
        self.assertEqual(graph["11"]["class_type"], "VHS_LoadImagePath")
        self.assertEqual(graph["30"]["inputs"]["first_frame"], ["1", 0])
        self.assertEqual(graph["30"]["inputs"]["last_frame"], ["11", 0])
        for index in range(9):
            self.assertEqual(
                graph["30"]["inputs"][f"ref_images.ref_image_{index}"],
                [str(2 + index), 0],
            )

    def test_single_clip_runner_skips_keyframe_generation(self):
        config = copy.deepcopy(self.config)
        config["single_clip"] = True
        config["fixed_first_frame"] = (self.temp / "first.png").as_posix()
        config["fixed_last_frame"] = (self.temp / "last.png").as_posix()
        segment = pipeline.segment_specs(config)[0]
        state_path = self.temp / "single_state.json"
        expected = {
            "segment": 1,
            "prompt_id": "1234567890",
            "video": "output.mp4",
            "seconds": 5,
            "two_pass": False,
        }
        with mock.patch.object(
            pipeline,
            "run_segment",
            return_value=dict(expected),
        ) as run_segment, mock.patch.object(
            pipeline,
            "normalize_video_resolution",
            side_effect=lambda config, video, prompt_id: Path(video),
        ):
            record = pipeline.run_single_clip(config, segment, state_path)

        run_segment.assert_called_once()
        args = run_segment.call_args.args
        self.assertEqual(args[3], config["fixed_first_frame"])
        self.assertEqual(args[4], config["fixed_last_frame"])
        self.assertEqual(record["start_frame"], config["fixed_first_frame"])
        self.assertEqual(record["end_frame"], config["fixed_last_frame"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "single_clip")
        self.assertEqual(len(state["segments"]), 1)

    def test_single_clip_node_has_no_storyboard_json_and_exposes_nine_refs(self):
        inputs = director.H3SingleClipDirector.INPUT_TYPES()
        self.assertIn("first_frame", inputs["required"])
        self.assertIn("last_frame", inputs["required"])
        self.assertIn("reference_1", inputs["required"])
        self.assertNotIn("storyboard", inputs["required"])
        for index in range(2, 10):
            self.assertIn(f"reference_{index}", inputs["optional"])

    def test_single_clip_workflow_is_direct_prompt_only(self):
        workflow = build_director_workflow.build_single_clip_workflow(self.config)
        node = next(
            item
            for item in workflow["nodes"]
            if item["type"] == "H3SingleClipDirector"
        )
        images = [
            item for item in workflow["nodes"] if item["type"] == "LoadImage"
        ]
        self.assertEqual(len(images), 11)
        self.assertEqual(len(workflow["links"]), 4)
        self.assertEqual(node["inputs"][0]["name"], "first_frame")
        self.assertEqual(node["inputs"][1]["name"], "last_frame")
        self.assertEqual(node["inputs"][10]["name"], "reference_9")
        self.assertNotIn("segments", node["widgets_values"][0])
        self.assertEqual(node["widgets_values"][1], 5.0)
        self.assertFalse(node["widgets_values"][10])

    # ------------------------------------------------------------------
    # Qwen keyframe backends
    # ------------------------------------------------------------------

    def test_qwen_keyframe_graph_hits_the_fixed_size(self):
        for backend in pipeline.QWEN_BACKENDS:
            for canvas in (None, self.config["reference_images"][0]):
                graph = pipeline.build_qwen_keyframe_graph(
                    self.config,
                    backend,
                    "test prompt",
                    canvas,
                    123,
                )
                save = graph["32"]
                self.assertEqual(save["class_type"], "SaveImage")
                self.assertIn("keyframes", save["inputs"]["filename_prefix"])
                if backend == pipeline.BACKEND_QWEN2511:
                    latent = graph["27"]
                    if canvas is None:
                        self.assertEqual(latent["class_type"], "EmptySD3LatentImage")
                        self.assertEqual(
                            (latent["inputs"]["width"], latent["inputs"]["height"]),
                            pipeline.keyframe_size(self.config),
                        )
                    else:
                        self.assertEqual(latent["class_type"], "VAEEncode")
                        scaler = graph[latent["inputs"]["pixels"][0]]
                        self.assertEqual(scaler["class_type"], "ImageScale")
                        self.assertEqual(
                            (
                                scaler["inputs"]["width"],
                                scaler["inputs"]["height"],
                            ),
                            pipeline.keyframe_size(self.config),
                        )
                else:
                    # 2.1 has no external latent: the encoder builds one from
                    # image_1, so image_1 must already be the exact size and
                    # resolution must stay 0 or the size drifts with the input.
                    encoder = graph["28"]
                    self.assertEqual(encoder["class_type"], "TextEncodeQwenImage21")
                    self.assertEqual(encoder["inputs"]["resolution"], 0)
                    first = graph[encoder["inputs"]["images.image_1"][0]]
                    self.assertEqual(first["class_type"], "ImageScale")
                    # 2.1 跟着图1/画布的比例出图（不再固定 608x1056），
                    # 4:5 的参考图因此不会被裁成 9:16 的窄条。
                    self.assertEqual(
                        (first["inputs"]["width"], first["inputs"]["height"]),
                        pipeline.qwen_keyframe_size(self.config, canvas),
                    )
                    # The rest are pure identity input: pixel-budgeted, never
                    # allowed to define the latent size. With a canvas there
                    # must be NO extra slots at all -- canvas + references
                    # measured as two people in the output.
                    extra = [
                        key
                        for key in encoder["inputs"]
                        if key.startswith("images.image_") and key != "images.image_1"
                    ]
                    if canvas is None:
                        for key in extra:
                            node = graph[encoder["inputs"][key][0]]
                            self.assertEqual(
                                node["class_type"], "ImageScaleToTotalPixels"
                            )
                    else:
                        self.assertEqual(extra, [])

    def test_qwen_keyframe_native_weights_skip_the_lora_node(self):
        # keyframe_qwen_lora: "none" must drop LoraLoaderModelOnly entirely and
        # wire UNET -> (Cache) directly, for both backends.
        for backend in pipeline.QWEN_BACKENDS:
            config = dict(self.config)
            config["keyframe_qwen_lora"] = "none"
            config["keyframe_qwen_steps"] = 20
            graph = pipeline.build_qwen_keyframe_graph(config, backend, "p", None, 123)
            self.assertFalse(
                [n for n in graph.values() if n["class_type"] == "LoraLoaderModelOnly"],
                backend,
            )
            self.assertEqual(graph["30"]["inputs"]["steps"], 20, backend)

    def test_qwen_keyframe_canvas_never_takes_references(self):
        # Edit passes (canvas given) must not feed identity references: the
        # canvas already carries the person, and measured on 2.1 the model
        # paints the references as additional people instead of treating them
        # as identity anchors. Start frames (canvas None) keep the references.
        for backend in pipeline.QWEN_BACKENDS:
            encoder_type = (
                "TextEncodeQwenImageEditPlus"
                if backend == pipeline.BACKEND_QWEN2511
                else "TextEncodeQwenImage21"
            )
            slot_prefix = (
                "image" if backend == pipeline.BACKEND_QWEN2511 else "images.image_"
            )

            def count_slots(graph):
                encoder = next(
                    node
                    for node in graph.values()
                    if node["class_type"] == encoder_type
                    and node["inputs"].get("prompt") == "p"
                )
                return sum(
                    1 for key in encoder["inputs"] if key.startswith(slot_prefix)
                )

            start = pipeline.build_qwen_keyframe_graph(
                self.config, backend, "p", None, 123
            )
            self.assertEqual(
                count_slots(start),
                len(self.config["reference_images"]),
                backend,
            )
            edit = pipeline.build_qwen_keyframe_graph(
                self.config, backend, "p", self.config["reference_images"][0], 123
            )
            self.assertEqual(count_slots(edit), 1, backend)

    def test_qwen_keyframe_canvas_refs_opt_in_keeps_references(self):
        # The identity-swap workflow (draw an angle plate with 2511, then paint
        # the reference person's face onto it) needs canvas AND references at
        # the same time; `keyframe_qwen_canvas_refs` re-enables them.
        config = dict(self.config)
        config["keyframe_qwen_canvas_refs"] = True
        for backend in pipeline.QWEN_BACKENDS:
            graph = pipeline.build_qwen_keyframe_graph(
                config, backend, "p", config["reference_images"][0], 123
            )
            load_images = [
                node
                for node in graph.values()
                if node["class_type"] == "VHS_LoadImagePath"
            ]
            # canvas + every reference photo
            self.assertEqual(
                len(load_images),
                1 + len(config["reference_images"]),
                backend,
            )

    def test_qwen_keyframe_canvas_blur_only_when_enabled(self):
        # `keyframe_qwen_canvas_blur_mp` mushes the canvas before it becomes
        # the latent, so 2.1 has to re-imagine details and the identity
        # reference actually steers the face (identity-swap pass). Off by
        # default: a derived frame that merely continues the action needs the
        # crisp canvas to keep the previous frame's detail.
        config = dict(self.config)
        config["keyframe_qwen_canvas_refs"] = True
        canvas = config["reference_images"][0]

        plain = pipeline.build_qwen_keyframe_graph(
            config, pipeline.BACKEND_QWEN21, "p", canvas, 123
        )
        self.assertNotIn("26b", plain)
        self.assertEqual(
            plain["28"]["inputs"]["images.image_1"][0], "26"
        )

        config["keyframe_qwen_canvas_blur_mp"] = 0.12
        mushed = pipeline.build_qwen_keyframe_graph(
            config, pipeline.BACKEND_QWEN21, "p", canvas, 123
        )
        self.assertEqual(mushed["26b"]["class_type"], "ImageScaleToTotalPixels")
        self.assertEqual(mushed["26b"]["inputs"]["megapixels"], 0.12)
        self.assertEqual(mushed["26c"]["class_type"], "ImageScale")
        self.assertEqual(
            (mushed["26c"]["inputs"]["width"], mushed["26c"]["inputs"]["height"]),
            pipeline.qwen_keyframe_size(config, canvas),
        )
        self.assertEqual(
            mushed["28"]["inputs"]["images.image_1"][0], "26c"
        )

    def test_qwen_keyframe_graph_wires_the_right_models(self):
        models = {
            pipeline.BACKEND_QWEN2511: (
                "UNETLoader",
                "qwen_image_edit_2511_int8_convrot.safetensors",
                "TextEncodeQwenImageEditPlus",
                8,
            ),
            # 2.1 走 ray 微调版 + 20 步、不挂 LoRA（定稿配方）。官方 int8
            # 会把头转向 3/4 侧面，8 步蒸馏档也只是省时间，这一档不追速度。
            pipeline.BACKEND_QWEN21: (
                "UNETLoader",
                r"Qwen\Qwen21-rayQwen21_v1_int8_convrot.safetensors",
                "TextEncodeQwenImage21",
                20,
            ),
        }
        for backend, (loader, unet, encoder, steps) in models.items():
            graph = pipeline.build_qwen_keyframe_graph(
                self.config, backend, "p", None, 123
            )
            loaders = [
                node
                for node in graph.values()
                if node["class_type"] == loader
                and "unet_name" in node["inputs"]
            ]
            self.assertTrue(loaders, backend)
            self.assertEqual(loaders[0]["inputs"]["unet_name"], unet)
            encoders = [
                node
                for node in graph.values()
                if node["class_type"] == encoder
                and node["inputs"].get("prompt") == "p"
            ]
            self.assertTrue(encoders, backend)
            self.assertEqual(graph["30"]["inputs"]["steps"], steps)
            self.assertEqual(graph["30"]["inputs"]["cfg"], 1.0)
            loras = [
                node
                for node in graph.values()
                if node["class_type"] == "LoraLoaderModelOnly"
            ]
            if backend == pipeline.BACKEND_QWEN21:
                # ray 版 20 步不需要蒸馏 LoRA，挂了反而把皮肤磨平
                self.assertEqual(loras, [])
            else:
                self.assertTrue(loras, "2511 必须挂 Lightning 8 步 LoRA")

    def test_run_keyframe_dispatches_to_the_qwen_builder(self):
        config = dict(self.config)
        segment = pipeline.segment_specs(config)[0]
        (self.temp / "prev.png").write_bytes(b"previous frame")
        for backend, canvas in (
            (pipeline.BACKEND_QWEN2511, None),
            (pipeline.BACKEND_QWEN2511, "prev.png"),
            (pipeline.BACKEND_QWEN21, None),
            (pipeline.BACKEND_QWEN21, "prev.png"),
        ):
            config["keyframe_backend"] = backend
            fake_entry = {
                "outputs": {
                    "32": {
                        "images": [
                            {
                                "filename": "fake.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
            with mock.patch.object(
                pipeline, "submit_graph", return_value=("pid", fake_entry)
            ) as submit:
                record = pipeline.run_keyframe(
                    config, segment, 123, first_frame=canvas, prompt="p"
                )
            self.assertEqual(submit.call_count, 1)
            graph = submit.call_args[0][1]
            self.assertIn("qwen", json.dumps(graph), backend)
            if canvas is None:
                self.assertIsNotNone(record["first_frame"])
                self.assertIsNone(record["last_frame"])
            else:
                self.assertIsNone(record["first_frame"])
                self.assertIsNotNone(record["last_frame"])
        # The suffix guards against the edit models rendering the identity
        # references as extra people; it must reach the encoder prompt.
        config["keyframe_backend"] = pipeline.BACKEND_QWEN21
        fake_entry = {
            "outputs": {
                "32": {
                    "images": [
                        {"filename": "fake.png", "subfolder": "", "type": "output"}
                    ]
                }
            }
        }
        with mock.patch.object(
            pipeline,
            "submit_graph",
            return_value=("pid", fake_entry),
        ) as submit:
            pipeline.run_keyframe(config, segment, 123, first_frame=None, prompt="p")
        encoder = next(
            node
            for node in submit.call_args[0][1].values()
            if node["class_type"] == "TextEncodeQwenImage21"
        )
        self.assertIn("不新增人物", encoder["inputs"]["prompt"])


class ResolutionTests(unittest.TestCase):
    """分辨率下拉 → final_width/height。"""

    def test_presets_beat_the_two_int_widgets(self):
        for label, expected in (
            ("720x1280（9:16 标清）", (720, 1280)),
            ("832x1472（9:16 默认）", (832, 1472)),
            ("1080x1920（9:16 高清）", (1080, 1920)),
            ("480x854（9:16 省显存）", (480, 854)),
        ):
            self.assertEqual(director.resolve_resolution(label, 999, 111), expected)

    def test_custom_uses_the_int_widgets(self):
        self.assertEqual(
            director.resolve_resolution("自定义（用 final_width / final_height）", 640, 1136),
            (640, 1136),
        )

    def test_empty_value_falls_back_to_int_widgets(self):
        self.assertEqual(director.resolve_resolution("", 704, 1216), (704, 1216))


class KeyframeAlignTests(unittest.TestCase):
    """关键帧对齐到成片分辨率：比例不符时裁切 / 扩图，尺寸最终必须精确相等。"""

    def setUp(self):
        from PIL import Image

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_dir.name)
        self.config = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
        self.config["comfy_input_dir"] = self.temp.as_posix()
        self.config["final_width"] = 832
        self.config["final_height"] = 1472
        self.frame = self.temp / "frame.png"
        Image.new("RGB", (608, 1056), (120, 130, 140)).save(self.frame)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _size(self, path):
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    def test_crop_mode_hits_the_exact_final_size(self):
        self.config["keyframe_align"] = "crop"
        out = pipeline.align_frame_to_target(self.config, self.frame.as_posix())
        self.assertEqual(self._size(out), (832, 1472))

    def test_off_mode_leaves_the_generated_size_alone(self):
        self.config["keyframe_align"] = "off"
        out = pipeline.align_frame_to_target(self.config, self.frame.as_posix())
        self.assertEqual(self._size(out), (608, 1056))

    def test_already_aligned_frame_is_untouched(self):
        from PIL import Image

        target = self.temp / "done.png"
        Image.new("RGB", (832, 1472), (10, 20, 30)).save(target)
        before = target.stat().st_mtime_ns
        out = pipeline.align_frame_to_target(self.config, target.as_posix())
        self.assertEqual(out, target.as_posix())
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_outpaint_canvas_keeps_the_target_aspect(self):
        target = (832, 1472)
        for source in ((608, 1056), (1024, 1024), (1280, 720)):
            canvas_w, canvas_h, px, py, sw, sh = pipeline.outpaint_canvas_geometry(source, target)
            self.assertLess(abs(canvas_w / canvas_h - target[0] / target[1]) / (target[0] / target[1]), 0.005)
            # 源图必须完整落在画布里（另一半才交给扩图去补）
            self.assertGreaterEqual(px, 0)
            self.assertGreaterEqual(py, 0)
            self.assertLessEqual(px + sw, canvas_w)
            self.assertLessEqual(py + sh, canvas_h)

    def test_outpaint_canvas_is_32_aligned(self):
        canvas_w, canvas_h, *_ = pipeline.outpaint_canvas_geometry((608, 1056), (720, 1280))
        self.assertEqual(canvas_w % 32, 0)
        self.assertEqual(canvas_h % 32, 0)

    def test_outpaint_graph_uses_the_outpaint_lora_and_canvas_size(self):
        graph = pipeline.build_qwen_keyframe_graph(
            self.config,
            pipeline.BACKEND_QWEN21,
            pipeline.DEFAULT_OUTPAINT_PROMPT,
            self.frame.as_posix(),
            4242,
            preset=pipeline.OUTPAINT_PRESET,
            size=(576, 1024),
        )
        by_type = {node["class_type"]: node for node in graph.values()}
        self.assertEqual(by_type["UNETLoader"]["inputs"]["unet_name"], pipeline.OUTPAINT_PRESET["unet"])
        self.assertIn("outpaint", by_type["LoraLoaderModelOnly"]["inputs"]["lora_name"])
        self.assertEqual(by_type["KSampler"]["inputs"]["steps"], pipeline.OUTPAINT_PRESET["steps"])
        # 画布尺寸必须落到 ImageScale 上，否则扩出来的还是原尺寸
        self.assertEqual(by_type["ImageScale"]["inputs"]["width"], 576)
        self.assertEqual(by_type["ImageScale"]["inputs"]["height"], 1024)
        # 2.1 的 latent 由编码器自己从 image_1 派生（第 3 个输出），
        # 所以画布必须接进 TextEncodeQwenImage21 的 image_1 槽位。
        encoder = by_type["TextEncodeQwenImage21"]
        self.assertEqual(encoder["inputs"]["resolution"], 0)
        self.assertIn("images.image_1", encoder["inputs"])
        latent_source = by_type["KSampler"]["inputs"]["latent_image"]
        self.assertEqual(graph[latent_source[0]]["class_type"], "TextEncodeQwenImage21")

    def test_near_target_aspect_does_not_pay_for_an_outpaint(self):
        """1080x1920 不是 32 的倍数，2.1 会输出 1088x1920（差 0.74%）。

        这一档必须直接 cover+crop，不能每帧多跑一次扩图——阈值卡在 0.5%
        的时候，选 1080 就是每帧白等一分钟。
        """
        from PIL import Image

        self.config["keyframe_align"] = "outpaint"
        self.config["final_width"] = 1080
        self.config["final_height"] = 1920
        frame = self.temp / "almost.png"
        Image.new("RGB", (1088, 1920), (90, 100, 110)).save(frame)

        calls = []
        original = pipeline.run_outpaint_pass

        def fake(*args, **kwargs):
            calls.append(args)
            raise AssertionError("不该走到扩图")

        pipeline.run_outpaint_pass = fake
        try:
            out = pipeline.align_frame_to_target(self.config, frame.as_posix())
        finally:
            pipeline.run_outpaint_pass = original
        self.assertEqual(calls, [])
        self.assertEqual(self._size(out), (1080, 1920))

    def test_align_never_raises_on_a_missing_file(self):
        missing = (self.temp / "nope.png").as_posix()
        self.assertEqual(pipeline.align_frame_to_target(self.config, missing), missing)


if __name__ == "__main__":
    unittest.main()
