#!/usr/bin/env python3
"""Read-only local dashboard server for Blender/Resolve productions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from fractions import Fraction
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from PIL import Image
except ImportError:  # Preview remains unavailable rather than exposing an unchecked frame.
    Image = None


SCRIPT_DIR = Path(__file__).resolve().parent
FRAME_PATTERN = re.compile(r"^frame_(\d+)\.png$", re.IGNORECASE)
ALLOWED_STATUSES = {"idle", "running", "cooldown", "failed", "complete"}
ALLOWED_STAGES = {"render", "edit", "delivery"}
REFRESH_SECONDS = 3.0


def iso_timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or time.time()).astimezone().isoformat(timespec="seconds")


def finite_number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def safe_int(value, default=None):
    number = finite_number(value)
    return int(number) if number is not None else default


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def relative_project_path(project_root: Path, value: str, field: str) -> Path:
    candidate = (project_root / value).resolve()
    root = project_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"{field} must stay inside project_root")
    return candidate


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def windows_path(path: Path) -> str:
    parts = path.resolve().parts
    if len(parts) >= 4 and parts[1] == "mnt" and len(parts[2]) == 1:
        return f"{parts[2].upper()}:/" + "/".join(parts[3:])
    return str(path)


def parse_rate(value) -> float | None:
    if not value:
        return None
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


class Monitor:
    def __init__(self, project_root: Path, config_path: Path):
        self.project_root = project_root.resolve()
        self.config_path = config_path.resolve()
        self.config = self._load_config()
        self.lock = threading.Lock()
        self.last_refresh = 0.0
        self.summary = None
        self.latest_frame: Path | None = None
        self.preview_cache_key = None
        self.preview_cache_result = None
        self.probe_cache_key = None
        self.probe_cache_result = None

    def _load_config(self) -> dict:
        raw = read_json(self.config_path)
        if not isinstance(raw, dict):
            raise ValueError(f"Missing or invalid monitor config: {self.config_path}")

        shots = raw.get("shots")
        if not isinstance(shots, list) or not shots:
            raise ValueError("monitor-config.json must contain a non-empty shots array")

        normalized_shots = []
        for index, shot in enumerate(shots, start=1):
            if not isinstance(shot, dict):
                raise ValueError(f"shots[{index - 1}] must be an object")
            shot_id = str(shot.get("id") or f"shot_{index:02d}")
            start = safe_int(shot.get("start"))
            end = safe_int(shot.get("end"))
            directory = str(shot.get("directory") or shot_id)
            if start is None or end is None or start > end:
                raise ValueError(f"Invalid frame range for {shot_id}")
            normalized_shots.append(
                {
                    "id": shot_id,
                    "label": str(shot.get("label") or shot_id.replace("_", " ").upper()),
                    "start": start,
                    "end": end,
                    "directory": directory,
                }
            )

        expected = raw.get("expected") if isinstance(raw.get("expected"), dict) else {}
        render_root = str(raw.get("render_root") or "render/frames")
        final_media = str(raw.get("final_media") or "delivery/final.mp4")

        relative_project_path(self.project_root, render_root, "render_root")
        relative_project_path(self.project_root, final_media, "final_media")

        return {
            "project_name": str(raw.get("project_name") or self.project_root.name),
            "project_file": Path(str(raw.get("project_file") or "")).name,
            "render_root": render_root,
            "final_media": final_media,
            "ffprobe": str(raw.get("ffprobe") or ""),
            "shots": normalized_shots,
            "expected": {
                "width": safe_int(expected.get("width"), 2160),
                "height": safe_int(expected.get("height"), 3840),
                "fps": finite_number(expected.get("fps"), 30.0),
                "frames": safe_int(
                    expected.get("frames"),
                    sum(item["end"] - item["start"] + 1 for item in normalized_shots),
                ),
                "codec": str(expected.get("codec") or "hevc").lower(),
                "pix_fmt": str(expected.get("pix_fmt") or "yuv420p").lower(),
                "color": str(expected.get("color") or "bt709").lower(),
                "color_range": str(expected.get("color_range") or "tv").lower(),
                "codec_tag": str(expected.get("codec_tag") or "hvc1").lower(),
                "audio_streams": safe_int(expected.get("audio_streams"), 0),
            },
        }

    def snapshot(self, force: bool = False) -> dict:
        with self.lock:
            now = time.monotonic()
            if force or self.summary is None or now - self.last_refresh >= REFRESH_SECONDS:
                self.summary = self._build_summary()
                self.last_refresh = now
            return self.summary

    def _canonical_state(self):
        render_state = read_json(self.project_root / "render" / "render-state.json")
        shot_state = read_json(self.project_root / "shot-status.json")
        resolve_state = read_json(self.project_root / "edit" / "resolve-run.json")
        return render_state, shot_state, resolve_state

    def _validated_shots(self, raw) -> set[str]:
        if isinstance(raw, dict):
            entries = raw.get("shots")
        else:
            entries = raw
        if not isinstance(entries, list):
            return set()

        validated = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("status") or "").lower()
            if entry.get("validated") is True or state in {"verified", "validated", "complete"}:
                shot_id = entry.get("id") or entry.get("shot_id")
                if shot_id:
                    validated.add(str(shot_id))
        return validated

    def _scan_shots(self, validated_shots: set[str]):
        render_root = relative_project_path(
            self.project_root, self.config["render_root"], "render_root"
        )
        shots = []
        preview_candidates = []
        newest_evidence = None
        total_present = 0

        for shot in self.config["shots"]:
            directory = relative_project_path(
                render_root, shot["directory"], f"directory for {shot['id']}"
            )
            frame_numbers = set()
            if directory.is_dir():
                for entry in os.scandir(directory):
                    match = FRAME_PATTERN.match(entry.name)
                    if not match or not entry.is_file(follow_symlinks=False):
                        continue
                    frame_number = int(match.group(1))
                    if shot["start"] <= frame_number <= shot["end"]:
                        frame_numbers.add(frame_number)
                        preview_candidates.append(
                            (frame_number, shot["id"], Path(entry.path))
                        )
                try:
                    directory_mtime = directory.stat().st_mtime
                    newest_evidence = max(
                        newest_evidence or directory_mtime, directory_mtime
                    )
                except OSError:
                    pass

            expected_numbers = set(range(shot["start"], shot["end"] + 1))
            total = len(expected_numbers)
            present = len(frame_numbers)
            total_present += present
            inventory_complete = frame_numbers == expected_numbers
            verified = shot["id"] in validated_shots
            if verified:
                state = "verified"
            elif inventory_complete:
                state = "inventory_complete"
            elif present:
                state = "partial"
            else:
                state = "queued"

            shots.append(
                {
                    "id": shot["id"],
                    "label": shot["label"],
                    "start": shot["start"],
                    "end": shot["end"],
                    "present": present,
                    "total": total,
                    "percent": round(100 * present / total, 1),
                    "last_frame": max(frame_numbers) if frame_numbers else None,
                    "inventory_complete": inventory_complete,
                    "verified": verified,
                    "status": state,
                }
            )

        preview = self._choose_preview(preview_candidates)
        return shots, total_present, newest_evidence, preview

    def _choose_preview(self, candidates):
        if Image is None:
            self.latest_frame = None
            return {
                "available": False,
                "reason": "Pillow 未安装，未暴露未经完整解码的预览帧",
            }

        ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
        for frame_number, shot_id, path in ordered[:12]:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            if key == self.preview_cache_key:
                valid, dimensions = self.preview_cache_result
            else:
                try:
                    with Image.open(path) as image:
                        image.load()
                        valid = image.format == "PNG"
                        dimensions = [image.width, image.height]
                except (OSError, ValueError):
                    valid, dimensions = False, None
                self.preview_cache_key = key
                self.preview_cache_result = (valid, dimensions)

            if valid:
                self.latest_frame = path
                return {
                    "available": True,
                    "shot_id": shot_id,
                    "frame": frame_number,
                    "filename": path.name,
                    "dimensions": dimensions,
                    "version": stat.st_mtime_ns,
                    "validated_at": iso_timestamp(),
                    "url": "/api/latest-frame",
                }

        self.latest_frame = None
        return {"available": False, "reason": "没有找到可完整解码的 PNG 帧"}

    def _processes(self) -> dict:
        tasklist = shutil.which("tasklist.exe")
        if not tasklist:
            return {"blender": [], "ffmpeg": [], "resolve": []}
        try:
            result = subprocess.run(
                [tasklist, "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"blender": [], "ffmpeg": [], "resolve": []}

        found = {"blender": [], "ffmpeg": [], "resolve": []}
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 2:
                continue
            image_name = row[0].lower()
            pid = safe_int(row[1])
            if pid is None:
                continue
            if image_name == "blender.exe":
                found["blender"].append(pid)
            elif image_name == "ffmpeg.exe":
                found["ffmpeg"].append(pid)
            elif image_name == "resolve.exe":
                found["resolve"].append(pid)
        return found

    def _memory(self):
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                values[key] = float(raw.strip().split()[0]) / 1024 / 1024
            total = values["MemTotal"]
            available = values["MemAvailable"]
            used = max(0.0, total - available)
            return {
                "used_gb": round(used, 1),
                "total_gb": round(total, 1),
                "percent": round(clamp(100 * used / total), 1),
            }
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return None

    def _gpu(self):
        candidates = [
            shutil.which("nvidia-smi"),
            shutil.which("nvidia-smi.exe"),
            "/usr/lib/wsl/lib/nvidia-smi",
            "/mnt/c/Windows/System32/nvidia-smi.exe",
        ]
        executable = next((str(item) for item in candidates if item and Path(item).is_file()), None)
        if not executable:
            return None
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=3,
                check=False,
            )
            values = [finite_number(item.strip()) for item in result.stdout.splitlines()[0].split(",")]
            if len(values) != 4 or any(item is None for item in values):
                return None
            utilization, used_mb, total_mb, temperature = values
            return {
                "percent": round(clamp(utilization), 1),
                "vram_used_gb": round(used_mb / 1024, 1),
                "vram_total_gb": round(total_mb / 1024, 1),
                "vram_percent": round(clamp(100 * used_mb / total_mb), 1),
                "temperature_c": round(temperature, 1),
            }
        except (OSError, IndexError, subprocess.TimeoutExpired, ZeroDivisionError):
            return None

    def _ffprobe_executable(self):
        configured = self.config["ffprobe"]
        if configured:
            candidate = relative_project_path(self.project_root, configured, "ffprobe")
            if candidate.is_file():
                return candidate
        for name in ("ffprobe", "ffprobe.exe"):
            candidate = shutil.which(name)
            if candidate:
                return Path(candidate)
        return None

    def _probe_media(self, path: Path):
        if not path.is_file():
            return {
                "exists": False,
                "filename": path.name,
                "probe_available": self._ffprobe_executable() is not None,
                "spec_passed": False,
                "issues": ["尚未找到最终成片"],
                "actual": {},
            }

        stat = path.stat()
        probe = self._ffprobe_executable()
        cache_key = (
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
            str(probe) if probe else "",
        )
        if cache_key == self.probe_cache_key:
            return self.probe_cache_result

        if probe is None:
            result = {
                "exists": True,
                "filename": path.name,
                "size_bytes": stat.st_size,
                "probe_available": False,
                "spec_passed": False,
                "issues": ["未找到 ffprobe，无法校验交付规格"],
                "actual": {},
            }
            self.probe_cache_key = cache_key
            self.probe_cache_result = result
            return result

        input_path = windows_path(path) if probe.suffix.lower() == ".exe" else str(path)
        try:
            completed = subprocess.run(
                [
                    str(probe),
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_entries",
                    (
                        "format=duration,size:"
                        "stream=index,codec_type,codec_name,profile,width,height,"
                        "r_frame_rate,pix_fmt,color_range,color_space,color_transfer,"
                        "color_primaries,codec_tag_string,nb_read_frames"
                    ),
                    "-of",
                    "json",
                    input_path,
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=45,
                check=False,
            )
            payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            payload = {}

        streams = payload.get("streams") if isinstance(payload, dict) else None
        streams = streams if isinstance(streams, list) else []
        video = next(
            (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
            {},
        )
        audio_count = sum(
            1
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        )
        format_data = payload.get("format") if isinstance(payload, dict) else {}
        format_data = format_data if isinstance(format_data, dict) else {}
        actual = {
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": safe_int(video.get("width")),
            "height": safe_int(video.get("height")),
            "fps": parse_rate(video.get("r_frame_rate")),
            "fps_raw": video.get("r_frame_rate"),
            "pix_fmt": video.get("pix_fmt"),
            "color_range": video.get("color_range"),
            "color_space": video.get("color_space"),
            "color_transfer": video.get("color_transfer"),
            "color_primaries": video.get("color_primaries"),
            "codec_tag": video.get("codec_tag_string"),
            "frames": safe_int(video.get("nb_read_frames")),
            "duration_seconds": finite_number(format_data.get("duration")),
            "audio_streams": audio_count,
        }

        expected = self.config["expected"]
        issues = []
        expected_codec = expected["codec"]
        codec_ok = actual["codec"] in (
            {"hevc", "h265"} if expected_codec in {"hevc", "h265"} else {expected_codec}
        )
        if not codec_ok:
            issues.append(
                f"编码为 {(actual['codec'] or '未知').upper()}，要求 H.265/HEVC"
            )
        if (actual["width"], actual["height"]) != (expected["width"], expected["height"]):
            issues.append(
                f"分辨率为 {actual['width'] or '—'}×{actual['height'] or '—'}，"
                f"要求 {expected['width']}×{expected['height']}"
            )
        if actual["fps"] is None or abs(actual["fps"] - expected["fps"]) > 0.001:
            issues.append(
                f"帧率为 {actual['fps_raw'] or '未知'}，要求 {expected['fps']:g} fps"
            )
        if str(actual["pix_fmt"] or "").lower() != expected["pix_fmt"]:
            issues.append(
                f"像素格式为 {actual['pix_fmt'] or '未知'}，要求 {expected['pix_fmt']}"
            )
        for key, label in (
            ("color_space", "matrix"),
            ("color_transfer", "transfer"),
            ("color_primaries", "primaries"),
        ):
            if str(actual[key] or "").lower() != expected["color"]:
                issues.append(f"Rec.709 {label} 标记缺失或不匹配")
        if str(actual["color_range"] or "").lower() != expected["color_range"]:
            issues.append("色彩范围不是有限范围 tv")
        if str(actual["codec_tag"] or "").lower() != expected["codec_tag"]:
            issues.append(
                f"MP4 标签为 {actual['codec_tag'] or '未知'}，要求 {expected['codec_tag']}"
            )
        if actual["frames"] != expected["frames"]:
            issues.append(
                f"实际帧数为 {actual['frames'] if actual['frames'] is not None else '未知'}，"
                f"要求 {expected['frames']}"
            )
        if actual["audio_streams"] != expected["audio_streams"]:
            issues.append(
                f"音轨数为 {actual['audio_streams']}，要求 {expected['audio_streams']}"
            )

        result = {
            "exists": True,
            "filename": path.name,
            "size_bytes": stat.st_size,
            "modified_at": iso_timestamp(stat.st_mtime),
            "probe_available": True,
            "spec_passed": not issues,
            "issues": issues,
            "actual": actual,
        }
        self.probe_cache_key = cache_key
        self.probe_cache_result = result
        return result

    def _live_field(self, data, *keys, default=None):
        value = data
        for key in keys:
            if not isinstance(value, dict):
                return default
            value = value.get(key)
        return value if value is not None else default

    def _build_summary(self) -> dict:
        generated_at = iso_timestamp()
        render_state, shot_state, resolve_state = self._canonical_state()
        validated_shots = self._validated_shots(shot_state)
        shots, frame_count, newest_frame_at, preview = self._scan_shots(validated_shots)
        processes = self._processes()
        gpu = self._gpu()
        memory = self._memory()
        final_path = relative_project_path(
            self.project_root, self.config["final_media"], "final_media"
        )
        delivery = self._probe_media(final_path)

        source_files = sum(
            item is not None for item in (render_state, shot_state, resolve_state)
        )
        source = "mixed" if source_files else "derived"
        render_state = render_state if isinstance(render_state, dict) else {}
        resolve_state = resolve_state if isinstance(resolve_state, dict) else {}
        delivery_state = (
            render_state.get("delivery")
            if isinstance(render_state.get("delivery"), dict)
            else {}
        )

        expected_frames = self.config["expected"]["frames"]
        frame_count = min(frame_count, expected_frames)
        percent = round(clamp(100 * frame_count / expected_frames), 1)
        all_frames_present = bool(shots) and all(item["inventory_complete"] for item in shots)
        all_frames_verified = bool(shots) and all(item["verified"] for item in shots)

        decode_passed = delivery_state.get("decode_passed")
        if not isinstance(decode_passed, bool):
            decode_passed = None
        sha256_value = delivery_state.get("sha256")
        sha256_recorded = isinstance(sha256_value, str) and len(sha256_value.strip()) == 64
        delivery["decode_passed"] = decode_passed
        delivery["sha256_recorded"] = sha256_recorded
        delivery["qa_passed"] = bool(
            delivery["spec_passed"] and decode_passed is True and sha256_recorded
        )
        if delivery["spec_passed"] and decode_passed is not True:
            delivery["issues"].append("尚无全片解码通过记录")
        if delivery["spec_passed"] and not sha256_recorded:
            delivery["issues"].append("尚无 SHA-256 记录")

        canonical_stage = str(render_state.get("stage") or "").lower()
        canonical_status = str(render_state.get("status") or "").lower()
        heartbeat_at = self._live_field(render_state, "health", "heartbeat_at")
        stale_after = safe_int(
            self._live_field(render_state, "health", "stale_after_seconds"), 90
        )
        heartbeat_age = None
        stale = False
        if isinstance(heartbeat_at, str):
            try:
                heartbeat_time = datetime.fromisoformat(heartbeat_at).timestamp()
                heartbeat_age = max(0, int(time.time() - heartbeat_time))
                stale = heartbeat_age > stale_after
            except ValueError:
                heartbeat_at = None

        if (
            canonical_stage in ALLOWED_STAGES
            and canonical_status in ALLOWED_STATUSES
            and not stale
        ):
            stage = canonical_stage
            status = canonical_status
        elif not all_frames_present:
            stage = "render"
            status = "running" if processes["blender"] else "idle"
        elif not delivery["exists"]:
            stage = "edit"
            status = "running" if processes["resolve"] or processes["ffmpeg"] else "idle"
        else:
            stage = "delivery"
            status = "complete" if delivery["qa_passed"] else (
                "failed" if not delivery["spec_passed"] else "idle"
            )

        if stage == "render" and status == "running":
            title = "Blender 正在渲染"
        elif stage == "render":
            title = "渲染未完成，当前没有活动进程"
        elif stage == "edit" and status == "running":
            title = "正在剪辑或编码"
        elif stage == "edit":
            title = "帧序列已齐，等待剪辑或编码"
        elif status == "complete":
            title = "成片已通过最终质检"
        elif status == "failed":
            title = "交付质检未通过"
        else:
            title = "成片待完成最终质检"

        median_frame_seconds = finite_number(
            self._live_field(render_state, "current", "median_frame_seconds")
        )
        eta_seconds = None
        if status == "running" and stage == "render" and median_frame_seconds is not None:
            eta_seconds = int(max(0, expected_frames - frame_count) * median_frame_seconds)

        recovery = (
            render_state.get("recovery")
            if isinstance(render_state.get("recovery"), dict)
            else {}
        )
        current_shot = next(
            (item for item in shots if not item["inventory_complete"]),
            shots[-1] if shots else None,
        )
        active_pids = processes["blender"] + processes["resolve"] + processes["ffmpeg"]
        state_mtimes = []
        for path in (
            self.project_root / "render" / "render-state.json",
            self.project_root / "shot-status.json",
            self.project_root / "edit" / "resolve-run.json",
            final_path,
        ):
            try:
                state_mtimes.append(path.stat().st_mtime)
            except OSError:
                pass
        if newest_frame_at:
            state_mtimes.append(newest_frame_at)

        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "source": source,
            "project_name": self.config["project_name"],
            "project_file": self.config["project_file"],
            "expected": self.config["expected"],
            "stage": stage,
            "status": status,
            "title": title,
            "progress": {
                "current": frame_count,
                "total": expected_frames,
                "percent": percent,
                "eta_seconds": eta_seconds,
                "all_frames_present": all_frames_present,
                "all_frames_verified": all_frames_verified,
            },
            "current": {
                "shot_id": current_shot["id"] if current_shot else None,
                "batch": self._live_field(render_state, "current", "batch"),
                "last_valid_frame": preview.get("frame"),
                "median_frame_seconds": median_frame_seconds,
            },
            "health": {
                "heartbeat_at": heartbeat_at,
                "heartbeat_age_seconds": heartbeat_age,
                "stale_after_seconds": stale_after,
                "stale": stale,
                "gpu": gpu,
                "memory": memory,
                "processes": processes,
                "active_pids": active_pids,
            },
            "recovery": {
                "batch_retry_count": safe_int(recovery.get("batch_retry_count")),
                "total_crash_count": safe_int(recovery.get("total_crash_count")),
                "reboot_resume_count": safe_int(recovery.get("reboot_resume_count")),
                "auto_resume_enabled": (
                    recovery.get("auto_resume_enabled")
                    if isinstance(recovery.get("auto_resume_enabled"), bool)
                    else None
                ),
                "last_error_signature": str(recovery.get("last_error_signature") or ""),
            },
            "resolve": {
                "timeline": str(resolve_state.get("timeline") or ""),
                "render_job_status": str(resolve_state.get("render_job_status") or ""),
            },
            "delivery": delivery,
            "shots": shots,
            "preview": preview,
            "freshness": {
                "observed_at": generated_at,
                "newest_evidence_at": iso_timestamp(max(state_mtimes)) if state_mtimes else None,
                "stale": stale,
            },
        }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "RenderWatch/1.0"

    @property
    def monitor(self) -> Monitor:
        return self.server.monitor

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, body: bytes, content_type: str, status=HTTPStatus.OK, no_cache=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_cache:
            self.send_header("Cache-Control", "no-store, max-age=0")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status, no_cache=True)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {
            "/",
            "/index.html",
            "/render-monitor-demo/",
            "/render-monitor-demo/index.html",
        }:
            try:
                body = (SCRIPT_DIR / "index.html").read_bytes()
            except OSError:
                self._send_json({"error": "index.html unavailable"}, HTTPStatus.NOT_FOUND)
                return
            self._send_bytes(body, "text/html; charset=utf-8")
            return

        if path == "/healthz":
            self._send_json({"ok": True, "service": "render-watch"})
            return

        if path == "/api/status":
            self._send_json(self.monitor.snapshot())
            return

        if path == "/api/latest-frame":
            self.monitor.snapshot()
            frame = self.monitor.latest_frame
            if frame is None:
                self._send_json({"error": "validated preview unavailable"}, HTTPStatus.NOT_FOUND)
                return
            try:
                body = frame.read_bytes()
            except OSError:
                self._send_json({"error": "preview unavailable"}, HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(frame.name)[0] or "image/png"
            self._send_bytes(body, content_type, no_cache=True)
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.end_headers()

    def do_POST(self):
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.end_headers()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, monitor):
        super().__init__(address, handler)
        self.monitor = monitor


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=SCRIPT_DIR.parent,
        help="Project root containing render/edit state (default: parent of monitor folder)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SCRIPT_DIR / "monitor-config.json",
        help="Dashboard config JSON",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4876)
    return parser.parse_args()


def main():
    args = arguments()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing non-loopback bind; use a protected tunnel explicitly.")
    monitor = Monitor(args.project_root, args.config)
    monitor.snapshot(force=True)
    server = DashboardServer((args.host, args.port), DashboardHandler, monitor)
    print(f"Render Watch: http://{args.host}:{args.port}/", flush=True)
    print(f"Project: {monitor.config['project_name']} (read-only)", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
