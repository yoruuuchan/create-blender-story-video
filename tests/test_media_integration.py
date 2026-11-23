from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SKILL, SCRIPTS, make_project, write_config, write_frames, write_render_state, write_shot_status
from production import probe_media, read_json, require_executable


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and FFprobe are required")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filters = subprocess.run([FFMPEG, "-hide_banner", "-h", "filter=zscale"], capture_output=True, check=False)
        encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False)
        if filters.returncode != 0 or "libx265" not in encoders.stdout or "libx264" not in encoders.stdout:
            raise unittest.SkipTest("FFmpeg with zscale, libx264 and libx265 is required")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storyboard = make_project(self.root, width=64, height=64)
        write_frames(self.root, self.storyboard)
        self.master = None

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, script, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, args)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(
            result.returncode, expected,
            f"{script} exited {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        return result

    def encode_first_shot(self):
        self.run_script(
            "encode_media.py", "shot", "--project-root", self.root, "--shot-id", "shot_01"
        )
        return self.root / self.storyboard["shots"][0]["clip_path"]

    def create_master(self):
        master_frames = self.root / "master-frames"
        master_frames.mkdir()
        for shot in self.storyboard["shots"]:
            for frame in range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1):
                shutil.copyfile(
                    self.root / shot["frame_directory"] / f"frame_{frame:06d}.png",
                    master_frames / f"frame_{frame:06d}.png",
                )
        from encode_media import video_filter
        master = self.root / "edit-master.mov"
        command = [
            FFMPEG, "-hide_banner", "-y", "-framerate", "30", "-start_number", "1",
            "-i", str(master_frames / "frame_%06d.png"), "-frames:v", "4",
            "-vf", video_filter(self.storyboard), "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709", str(master),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        return master

    def test_png_to_shot_to_final_hevc_and_bound_report(self):
        clip = self.encode_first_shot()
        actual = probe_media(require_executable(FFPROBE, "ffprobe"), clip)
        self.assertEqual(actual["codec"], "h264")
        self.assertEqual(actual["frames"], 2)
        self.assertEqual((actual["width"], actual["height"]), (64, 64))
        self.assertEqual(actual["color_transfer"], "bt709")

        guarded = self.run_script(
            "encode_media.py", "shot", "--project-root", self.root, "--shot-id", "shot_01", expected=1
        )
        self.assertIn("--replace", guarded.stderr)

        master = self.create_master()
        self.run_script(
            "encode_media.py", "final", "--project-root", self.root, "--master", master,
            "--input-primaries", "bt709", "--input-transfer", "bt709",
            "--input-matrix", "bt709", "--input-range", "tv",
        )
        report = read_json(self.root / "delivery" / "delivery-report.json")
        self.assertEqual(report["status"], "complete", report["issues"])
        self.assertTrue(report["verification"]["decode_passed"])
        self.assertEqual(report["actual"]["codec"], "hevc")
        self.assertEqual(report["actual"]["profile"], "Main")
        self.assertEqual(report["actual"]["frames"], 4)
        self.assertEqual(report["actual"]["codec_tag"], "hvc1")
        self.assertRegex(report["media"]["sha256"], r"^[0-9a-f]{64}$")

        verified = self.run_script("verify_media.py", "--project-root", self.root)
        self.assertIn("OK final media", verified.stdout)

        write_config(self.root)
        write_shot_status(self.root, self.storyboard, frames_pass=True)
        write_render_state(self.root, self.storyboard, status="complete", stage="delivery")
        spec = importlib.util.spec_from_file_location(
            "dashboard_server_real_media", SKILL / "assets" / "render-monitor-dashboard" / "server.py"
        )
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)
        monitor = dashboard.Monitor(self.root, self.root / "monitor" / "monitor-config.json")
        summary = monitor.snapshot(force=True)
        self.assertTrue(summary["delivery"]["qa_passed"], summary["delivery"]["issues"])
        self.assertEqual(summary["status"], "complete")

    def test_verify_media_rejects_wrong_frame_count_and_codec(self):
        clip = self.encode_first_shot()
        final = self.root / "delivery" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(clip, final)
        result = self.run_script("verify_media.py", "--project-root", self.root, expected=1)
        self.assertIn("FAIL final media", result.stderr)
        report = read_json(self.root / "delivery" / "delivery-report.json")
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["verification"]["spec_passed"])

    def test_fractional_fps_is_derived_from_storyboard(self):
        self.storyboard["delivery"]["fps"] = {"numerator": 30000, "denominator": 1001}
        from production import write_json_atomic
        write_json_atomic(self.root / "storyboard.json", self.storyboard)
        result = self.run_script(
            "encode_media.py", "shot", "--project-root", self.root,
            "--shot-id", "shot_01", "--dry-run",
        )
        command = json.loads(result.stdout)
        index = command.index("-framerate")
        self.assertEqual(command[index + 1], "30000/1001")
        self.assertIn("30000/1001", command)


if __name__ == "__main__":
    unittest.main()
