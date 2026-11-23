from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import (
    ROOT,
    SCRIPTS,
    indexed_png_bytes,
    make_project,
    png_bytes,
    write_config,
    write_frames,
    write_render_state,
    write_shot_status,
)
from production import (
    WorkflowError,
    expected_media,
    load_storyboard,
    media_identity,
    read_json,
    sha256_file,
    source_plan,
    utc_now,
    validate_document,
    validate_optional_states,
    validate_storyboard,
    write_json_atomic,
)
from render_supervisor import SingleInstanceLock, Supervisor, pid_exists
from verify_frames import inspect_shot_frames, record_frame_gate


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storyboard = make_project(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_second_shot_uses_local_source_and_global_record_frames(self):
        plan = source_plan(self.storyboard, self.root)
        self.assertEqual(plan[1]["timeline_frame_start"], 3)
        self.assertEqual(plan[1]["source_frame_start"], 0)
        self.assertEqual(plan[1]["source_frame_end"], 1)
        self.assertEqual(plan[1]["record_frame"], 2)

    def test_invalid_record_frame_is_rejected(self):
        self.storyboard["shots"][1]["record_frame"] = 3
        with self.assertRaisesRegex(WorkflowError, "record_frame"):
            validate_storyboard(self.storyboard, self.root)

    def test_non_contiguous_timeline_is_rejected(self):
        self.storyboard["shots"][1]["timeline_frame_start"] = 4
        with self.assertRaisesRegex(WorkflowError, "contiguous"):
            validate_storyboard(self.storyboard, self.root)

    def test_landscape_and_portrait_2x_upscale_are_valid(self):
        for width, height in ((3840, 2160), (2160, 3840)):
            value = copy.deepcopy(self.storyboard)
            value["delivery"] |= {
                "width": width,
                "height": height,
                "source_width": width // 2,
                "source_height": height // 2,
                "aspect_ratio": "16:9" if width > height else "9:16",
                "upscaled_4k": True,
            }
            validate_storyboard(value)

    def test_non_4k_upscale_is_rejected(self):
        self.storyboard["delivery"] |= {
            "source_width": 32, "source_height": 32, "upscaled_4k": True
        }
        with self.assertRaisesRegex(WorkflowError, "4K"):
            validate_storyboard(self.storyboard, self.root)

    def test_monitor_config_contains_only_storyboard_reference(self):
        write_config(self.root)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_monitor_config.py"), "--project-root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = read_json(self.root / "monitor" / "monitor-config.json")
        self.assertEqual(config, {"schema_version": 1, "storyboard": "storyboard.json", "ffprobe": ""})
        self.assertNotIn("shots", config)
        self.assertNotIn("expected", config)

    def test_resolve_plan_cross_validation(self):
        write_shot_status(self.root, self.storyboard)
        write_json_atomic(self.root / "edit" / "resolve-run.json", {
            "schema_version": 1,
            "status": "complete",
            "route": {"type": "script_api", "reason": "test", "capabilities": {}},
            "project_name": "test-project",
            "backup_path": "backups/test.drp",
            "timeline": "main_v001",
            "source_plan": source_plan(self.storyboard, self.root),
            "render_job": None,
            "readback_passed": True,
            "updated_at": utc_now(),
        })
        messages = validate_optional_states(self.root, self.storyboard, self.root / "storyboard.json")
        self.assertTrue(any(message.startswith("OK resolve-run") for message in messages))

    def test_delivery_report_cross_validation_and_hex_sha(self):
        final = self.root / "delivery" / "final.mp4"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"test-media")
        report = {
            "schema_version": 1,
            "status": "complete",
            "storyboard_sha256": sha256_file(self.root / "storyboard.json"),
            "media": media_identity(final, self.root),
            "expected": expected_media(self.storyboard),
            "actual": {},
            "verification": {"spec_passed": True, "decode_passed": True, "stable_file": True},
            "tool_versions": {"ffprobe": "test", "ffmpeg": "test"},
            "issues": [],
            "created_at": utc_now(),
        }
        validate_document("delivery-report", report)
        write_json_atomic(self.root / "delivery" / "delivery-report.json", report)
        messages = validate_optional_states(self.root, self.storyboard, self.root / "storyboard.json")
        self.assertTrue(any(message.startswith("OK delivery-report") for message in messages))
        final.write_bytes(b"replacement-with-a-new-hash")
        with self.assertRaisesRegex(WorkflowError, "current final media"):
            validate_optional_states(self.root, self.storyboard, self.root / "storyboard.json")
        report["media"]["sha256"] = "z" * 64
        with self.assertRaisesRegex(WorkflowError, "schema validation"):
            validate_document("delivery-report", report)


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storyboard = make_project(self.root)
        write_shot_status(self.root, self.storyboard)

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_frames_pass_and_record_gate(self):
        shot = self.storyboard["shots"][0]
        write_frames(self.root, self.storyboard, shot["id"])
        report = inspect_shot_frames(self.storyboard, self.root, shot)
        self.assertEqual(report["status"], "complete", report["issues"])
        record_frame_gate(self.root, self.storyboard, report, "render/validation/shot_01.frames.json")
        status = read_json(self.root / "shot-status.json")
        self.assertEqual(status["shots"][0]["gates"]["frames_validated"]["status"], "pass")

    def test_truncated_png_is_quarantined_and_rerenderable(self):
        shot = self.storyboard["shots"][0]
        write_frames(self.root, self.storyboard, shot["id"])
        damaged = self.root / shot["frame_directory"] / "frame_000002.png"
        damaged.write_bytes(damaged.read_bytes()[:-8])
        report = inspect_shot_frames(self.storyboard, self.root, shot, quarantine_invalid=True)
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(damaged.exists())
        self.assertEqual(len(report["invalid_frames"]), 1)
        damaged.write_bytes(png_bytes())
        self.assertEqual(inspect_shot_frames(self.storyboard, self.root, shot)["status"], "complete")

    def test_duplicate_and_unexpected_frame_files_fail(self):
        shot = self.storyboard["shots"][0]
        write_frames(self.root, self.storyboard, shot["id"])
        directory = self.root / shot["frame_directory"]
        (directory / "frame_1.png").write_bytes(png_bytes())
        (directory / "frame_000099.png").write_bytes(png_bytes())
        report = inspect_shot_frames(self.storyboard, self.root, shot)
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(report["duplicates"])
        self.assertTrue(report["unexpected_files"])

    def test_wrong_dimensions_fail_full_decode(self):
        shot = self.storyboard["shots"][0]
        write_frames(self.root, self.storyboard, shot["id"])
        (self.root / shot["frame_directory"] / "frame_000002.png").write_bytes(png_bytes(32, 32))
        report = inspect_shot_frames(self.storyboard, self.root, shot)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("invalid 1 frames", report["issues"])

    def test_invalid_png_filter_type_fails_even_with_valid_crc_and_zlib(self):
        shot = self.storyboard["shots"][0]
        write_frames(self.root, self.storyboard, shot["id"])
        (self.root / shot["frame_directory"] / "frame_000002.png").write_bytes(png_bytes(filter_type=5))
        report = inspect_shot_frames(self.storyboard, self.root, shot)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("filter type 5", report["invalid_frames"][0]["error"])

    def test_indexed_png_requires_palette_and_legal_bit_depth(self):
        from pngcheck import PngError, validate_png
        missing_palette = self.root / "missing-palette.png"
        missing_palette.write_bytes(indexed_png_bytes(include_palette=False))
        with self.assertRaisesRegex(PngError, "requires PLTE"):
            validate_png(missing_palette)
        illegal_depth = self.root / "illegal-depth.png"
        illegal_depth.write_bytes(indexed_png_bytes(bit_depth=16, include_palette=True))
        with self.assertRaisesRegex(PngError, "bit depth"):
            validate_png(illegal_depth)


class MediaContractTests(unittest.TestCase):
    def valid_actual(self):
        return {
            "codec": "hevc", "profile": "Main", "width": 64, "height": 64,
            "fps": 30.0, "fps_raw": "30/1", "avg_fps": 30.0, "avg_fps_raw": "30/1",
            "frames": 4, "duration_seconds": 4 / 30,
            "video_duration_seconds": 4 / 30, "audio_durations": [],
            "pix_fmt": "yuv420p", "color_range": "tv", "color_space": "bt709",
            "color_transfer": "bt709", "color_primaries": "bt709",
            "codec_tag": "hvc1", "audio_streams": 0,
        }

    def test_average_frame_rate_and_duration_are_required(self):
        from production import media_spec_issues
        expected = {
            "codec": "hevc", "profile": "Main", "width": 64, "height": 64,
            "fps": 30.0, "frames": 4, "pix_fmt": "yuv420p", "color": "bt709",
            "color_range": "tv", "codec_tag": "hvc1", "audio_streams": 0,
        }
        self.assertEqual(media_spec_issues(self.valid_actual(), expected), [])
        variable = self.valid_actual() | {"avg_fps": 24.0}
        self.assertTrue(any("average fps" in issue for issue in media_spec_issues(variable, expected)))
        long_audio = self.valid_actual() | {"audio_durations": [2.0], "audio_streams": 1}
        expected_with_audio = expected | {"audio_streams": 1}
        self.assertTrue(any("audio stream" in issue for issue in media_spec_issues(long_audio, expected_with_audio)))
        edit_list = self.valid_actual() | {"duration_seconds": 2.0}
        self.assertTrue(any("container duration" in issue for issue in media_spec_issues(edit_list, expected)))


class ProcessAndEncodingContractTests(unittest.TestCase):
    def test_single_instance_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "render.lock"
            with SingleInstanceLock(path):
                with self.assertRaisesRegex(WorkflowError, "Another render supervisor"):
                    with SingleInstanceLock(path):
                        pass

    def test_windows_pid_probe_is_read_only(self):
        completed = subprocess.CompletedProcess([], 0, stdout='"blender.exe","1234","Console"\r\n', stderr="")
        with mock.patch("render_supervisor.os.name", "nt"), \
             mock.patch("render_supervisor.subprocess.run", return_value=completed), \
             mock.patch("render_supervisor.os.kill") as kill:
            self.assertTrue(pid_exists(1234))
            kill.assert_not_called()

    def test_blender_child_receives_the_same_custom_storyboard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storyboard = make_project(root)
            alternate = root / "alternate.json"
            write_json_atomic(alternate, storyboard)
            args = SimpleNamespace(
                project_root=root, storyboard=alternate, blender=sys.executable,
                no_factory_startup=False, enable_auto_resume=False, reboot_resume=False,
            )
            supervisor = Supervisor(args)
            command = supervisor.command(storyboard["shots"][0], 1, 2)
            self.assertIn("--storyboard", command)
            self.assertEqual(command[command.index("--storyboard") + 1], str(alternate))

    def test_reboot_budget_exhaustion_disables_auto_resume_on_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storyboard = make_project(root)
            write_render_state(root, storyboard, status="idle")
            state_path = root / "render" / "render-state.json"
            state = read_json(state_path)
            state["recovery"]["reboot_resume_count"] = 1
            state["recovery"]["auto_resume_enabled"] = True
            write_json_atomic(state_path, state)
            args = SimpleNamespace(
                project_root=root, storyboard=None, blender=sys.executable,
                no_factory_startup=False, enable_auto_resume=False, reboot_resume=True,
            )
            supervisor = Supervisor(args)
            with self.assertRaisesRegex(WorkflowError, "budget is exhausted"):
                supervisor.initial_state()
            persisted = read_json(state_path)
            self.assertFalse(persisted["recovery"]["auto_resume_enabled"])
            self.assertEqual(persisted["status"], "failed")
            self.assertIn("budget is exhausted", persisted["recovery"]["last_error_signature"])

    def test_srgb_filter_performs_real_transfer_conversion(self):
        from encode_media import video_filter
        with tempfile.TemporaryDirectory() as temporary:
            storyboard = make_project(Path(temporary))
            self.assertIn("zscale=", video_filter(storyboard))
            self.assertIn("transferin=iec61966-2-1", video_filter(storyboard))

    def test_rec709_rgb_filter_uses_scale_without_fake_retag(self):
        from encode_media import video_filter
        with tempfile.TemporaryDirectory() as temporary:
            storyboard = make_project(Path(temporary))
            storyboard["delivery"]["input_color"]["transfer"] = "bt709"
            result = video_filter(storyboard)
            self.assertTrue(result.startswith("scale="))
            self.assertNotIn("zscale", result)


if __name__ == "__main__":
    unittest.main()
