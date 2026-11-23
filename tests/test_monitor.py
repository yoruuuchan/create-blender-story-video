from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from helpers import SKILL, make_project, write_config, write_frames, write_render_state, write_shot_status
from production import media_identity, sha256_file, utc_now, write_json_atomic

SPEC = importlib.util.spec_from_file_location("dashboard_server", SKILL / "assets" / "render-monitor-dashboard" / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storyboard = make_project(self.root)
        write_config(self.root)
        write_shot_status(self.root, self.storyboard, frames_pass=True)
        write_frames(self.root, self.storyboard)
        self.monitor = server.Monitor(self.root, self.root / "monitor" / "monitor-config.json")
        self.process_patch = mock.patch.object(self.monitor, "_processes", return_value={"blender": [], "ffmpeg": [], "resolve": []})
        self.gpu_patch = mock.patch.object(self.monitor, "_gpu", return_value=None)
        self.memory_patch = mock.patch.object(self.monitor, "_memory", return_value=None)
        self.process_patch.start()
        self.gpu_patch.start()
        self.memory_patch.start()
        self.ffprobe_patch = mock.patch.object(self.monitor, "_ffprobe_executable", return_value=Path("ffprobe"))
        self.ffprobe_patch.start()
        payload = {
            "streams": [{
                "index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main",
                "width": 64, "height": 64, "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
                "duration": "0.133333", "pix_fmt": "yuv420p",
                "color_range": "tv", "color_space": "bt709", "color_transfer": "bt709",
                "color_primaries": "bt709", "codec_tag_string": "hvc1", "nb_read_frames": "4",
            }],
            "format": {"duration": "0.133333", "size": "1000"},
        }
        self.run_patch = mock.patch.object(
            server.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr=""),
        )
        self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.ffprobe_patch.stop()
        self.memory_patch.stop()
        self.gpu_patch.stop()
        self.process_patch.stop()
        self.temporary.cleanup()

    def create_media(self):
        path = self.root / "delivery" / "final.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"current-media")
        return path

    def write_valid_report(self):
        path = self.create_media()
        probe = self.monitor._probe_media(path)
        report = {
            "schema_version": 1,
            "status": "complete",
            "storyboard_sha256": sha256_file(self.root / "storyboard.json"),
            "media": media_identity(path, self.root),
            "expected": self.monitor.config["expected"],
            "actual": probe["actual"],
            "verification": {"spec_passed": True, "decode_passed": True, "stable_file": True},
            "tool_versions": {"ffprobe": "test", "ffmpeg": "test"},
            "issues": [],
            "created_at": utc_now(),
        }
        write_json_atomic(self.root / "delivery" / "delivery-report.json", report)
        return report

    def test_complete_state_without_media_cannot_pass(self):
        write_render_state(self.root, self.storyboard, status="complete", stage="delivery")
        summary = self.monitor.snapshot(force=True)
        self.assertFalse(summary["delivery"]["qa_passed"])
        self.assertNotEqual(summary["status"], "complete")
        self.assertNotEqual(summary["title"], "成片已通过最终质检")

    def test_current_hash_bound_report_and_frames_allow_complete(self):
        self.write_valid_report()
        write_render_state(self.root, self.storyboard, status="complete", stage="delivery")
        summary = self.monitor.snapshot(force=True)
        self.assertTrue(summary["delivery"]["spec_passed"], summary["delivery"]["issues"])
        self.assertTrue(summary["delivery"]["qa_passed"], summary["delivery"]["issues"])
        self.assertEqual(summary["status"], "complete")

    def test_replaced_media_invalidates_old_report(self):
        self.write_valid_report()
        self.assertEqual(self.monitor.snapshot(force=True)["status"], "complete")
        path = self.root / "delivery" / "final.mp4"
        path.write_bytes(b"replacement-media-with-a-different-size")
        summary = self.monitor.snapshot(force=True)
        self.assertFalse(summary["delivery"]["qa_passed"])
        self.assertNotEqual(summary["status"], "complete")
        self.assertTrue(any("不一致" in issue for issue in summary["delivery"]["issues"]))

    def test_same_stat_replacement_is_caught_by_recomputed_sha(self):
        report = self.write_valid_report()
        path = self.root / "delivery" / "final.mp4"
        original_stat = path.stat()
        path.write_bytes(b"updated-media")
        self.assertEqual(len(b"updated-media"), original_stat.st_size)
        probe = self.monitor._probe_media(path)
        with mock.patch.object(Path, "stat", return_value=original_stat):
            delivery = self.monitor._apply_delivery_report(probe, report)
        self.assertFalse(delivery["sha256_recorded"])
        self.assertFalse(delivery["qa_passed"])

    def test_missing_frame_blocks_complete_even_with_valid_media_report(self):
        self.write_valid_report()
        shot = self.storyboard["shots"][0]
        (self.root / shot["frame_directory"] / "frame_000001.png").unlink()
        summary = self.monitor.snapshot(force=True)
        self.assertFalse(summary["delivery"]["qa_passed"])
        self.assertNotEqual(summary["status"], "complete")
        self.assertTrue(any("frames_validated" in issue for issue in summary["delivery"]["issues"]))

    def test_repeated_snapshots_do_not_grow_cached_issues(self):
        self.create_media()
        first = self.monitor.snapshot(force=True)
        second = self.monitor.snapshot(force=True)
        self.assertEqual(first["delivery"]["issues"], second["delivery"]["issues"])
        self.assertEqual(len(first["delivery"]["issues"]), len(set(first["delivery"]["issues"])))

    def test_active_state_without_heartbeat_is_stale_not_running(self):
        write_render_state(self.root, self.storyboard, status="running", heartbeat=False)
        summary = self.monitor.snapshot(force=True)
        self.assertTrue(summary["health"]["stale"])
        self.assertNotEqual(summary["status"], "running")

    def test_eta_uses_recent_frame_duration_median(self):
        write_render_state(self.root, self.storyboard, status="running", heartbeat=True)
        for shot in self.storyboard["shots"]:
            for frame in range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1):
                (self.root / shot["frame_directory"] / f"frame_{frame:06d}.png").unlink()
        summary = self.monitor.snapshot(force=True)
        self.assertEqual(summary["current"]["median_frame_seconds"], 4.0)
        self.assertEqual(summary["progress"]["eta_seconds"], 16)

    def test_non_hex_report_sha_is_rejected(self):
        report = self.write_valid_report()
        report["media"]["sha256"] = "z" * 64
        write_json_atomic(self.root / "delivery" / "delivery-report.json", report)
        summary = self.monitor.snapshot(force=True)
        self.assertFalse(summary["delivery"]["qa_passed"])

    def test_host_origin_and_write_request_boundaries(self):
        http_server = server.DashboardServer(
            ("127.0.0.1", 0), server.DashboardHandler, self.monitor,
            allowed_hosts=["status.example.com:443"],
        )
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        port = http_server.server_address[1]
        try:
            def request(host, origin=None, fetch_site=None, method="GET"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.putrequest(method, "/healthz", skip_host=True)
                connection.putheader("Host", host)
                if origin:
                    connection.putheader("Origin", origin)
                if fetch_site:
                    connection.putheader("Sec-Fetch-Site", fetch_site)
                connection.endheaders()
                response = connection.getresponse()
                response.read()
                status = response.status
                connection.close()
                return status

            self.assertEqual(request(f"127.0.0.1:{port}"), 200)
            self.assertEqual(request("evil.example"), 403)
            self.assertEqual(request(f"127.0.0.1:{port}", origin="https://evil.example"), 403)
            self.assertEqual(request(f"127.0.0.1:{port}", fetch_site="cross-site"), 403)
            self.assertEqual(request(f"127.0.0.1:{port}", method="POST"), 405)
            self.assertEqual(request("status.example.com:443"), 200)
            self.assertEqual(request("status.example.com:9999"), 403)
            self.assertEqual(request("status.example.com:443", origin="https://status.example.com:9999"), 403)
            self.assertEqual(request("status.example.com:443", origin="https://status.example.com:443"), 200)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
