#!/usr/bin/env python3
"""Read-only local dashboard server for Blender/Resolve productions."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import statistics
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
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError(f"Missing or invalid monitor config: {self.config_path}")
        storyboard_reference = raw.get("storyboard")
        if not isinstance(storyboard_reference, str) or not storyboard_reference:
            raise ValueError("monitor-config.json must reference storyboard.json")
        storyboard_path = relative_project_path(
            self.project_root, storyboard_reference, "storyboard"
        )
        storyboard = read_json(storyboard_path)
        if not isinstance(storyboard, dict) or storyboard.get("schema_version") != 1:
            raise ValueError(f"Missing or invalid storyboard: {storyboard_path}")
        shots = storyboard.get("shots")
        delivery = storyboard.get("delivery")
        project = storyboard.get("project")
        if not isinstance(shots, list) or not shots or not isinstance(delivery, dict) or not isinstance(project, dict):
            raise ValueError("storyboard.json must contain project, delivery and non-empty shots")

        normalized_shots = []
        expected_start = 1
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                raise ValueError(f"shots[{index}] must be an object")
            shot_id = str(shot.get("id") or "")
            start = safe_int(shot.get("timeline_frame_start"))
            end = safe_int(shot.get("timeline_frame_end"))
            directory = str(shot.get("frame_directory") or "")
            if not shot_id or start != expected_start or end is None or end < start or not directory:
                raise ValueError(f"Invalid or non-contiguous frame range for {shot_id or index}")
            duration = safe_int(shot.get("duration_frames"))
            if duration != end - start + 1:
                raise ValueError(f"Invalid duration_frames for {shot_id}")
            relative_project_path(self.project_root, directory, f"frame_directory for {shot_id}")
            normalized_shots.append({
                "id": shot_id,
                "label": shot_id.replace("_", " ").upper(),
                "start": start,
                "end": end,
                "directory": directory,
            })
            expected_start = end + 1

        fps_data = delivery.get("fps") if isinstance(delivery.get("fps"), dict) else {}
        fps_numerator = finite_number(fps_data.get("numerator"))
        fps_denominator = finite_number(fps_data.get("denominator"))
        if not fps_numerator or not fps_denominator:
            raise ValueError("storyboard delivery.fps must contain positive numerator and denominator")
        total_frames = safe_int(delivery.get("total_frames"))
        if total_frames != expected_start - 1:
            raise ValueError("delivery.total_frames does not match shot ranges")
        if safe_int(delivery.get("width"), 0) < 2 or safe_int(delivery.get("height"), 0) < 2:
            raise ValueError("delivery width and height must be positive")
        required_text = ("codec", "profile", "pixel_format", "color_space", "color_range", "codec_tag")
        if any(field not in delivery or not str(delivery[field]) for field in required_text):
            raise ValueError("delivery codec, profile and color fields are required")
        if safe_int(delivery.get("audio_streams"), -1) < 0:
            raise ValueError("delivery.audio_streams must be zero or greater")
        final_media = str(delivery.get("final_media") or "")
        relative_project_path(self.project_root, final_media, "final_media")

        return {
            "project_name": str(project.get("name") or self.project_root.name),
            "project_file": Path(str(project.get("approved_blend") or "")).name,
            "storyboard_sha256": hashlib.sha256(storyboard_path.read_bytes()).hexdigest(),
            "final_media": final_media,
            "ffprobe": str(raw.get("ffprobe") or ""),
            "shots": normalized_shots,
            "expected": {
                "width": safe_int(delivery.get("width")),
                "height": safe_int(delivery.get("height")),
                "fps": fps_numerator / fps_denominator,
                "frames": total_frames,
                "codec": str(delivery.get("codec") or "").lower(),
                "profile": str(delivery.get("profile") or ""),
                "pix_fmt": str(delivery.get("pixel_format") or "").lower(),
                "color": str(delivery.get("color_space") or "").lower(),
                "color_range": str(delivery.get("color_range") or "").lower(),
                "codec_tag": str(delivery.get("codec_tag") or "").lower(),
                "audio_streams": safe_int(delivery.get("audio_streams"), -1),
            },
        }

    def snapshot(self, force: bool = False) -> dict:
        with self.lock:
            now = time.monotonic()
            if force or self.summary is None or now - self.last_refresh >= REFRESH_SECONDS:
                self.summary = self._build_summary()
                self.last_refresh = now
            return copy.deepcopy(self.summary)

    def _canonical_state(self):
        render_state = read_json(self.project_root / "render" / "render-state.json")
        shot_state = read_json(self.project_root / "shot-status.json")
        resolve_state = read_json(self.project_root / "edit" / "resolve-run.json")
        delivery_report = read_json(self.project_root / "delivery" / "delivery-report.json")
        return render_state, shot_state, resolve_state, delivery_report

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
            gates = entry.get("gates") if isinstance(entry.get("gates"), dict) else {}
            frame_gate = gates.get("frames_validated") if isinstance(gates.get("frames_validated"), dict) else {}
            if frame_gate.get("status") == "pass" and entry.get("shot_id"):
                validated.add(str(entry["shot_id"]))
        return validated

    def _scan_shots(self, validated_shots: set[str]):
        shots = []
        preview_candidates = []
        newest_evidence = None
        total_present = 0

        for shot in self.config["shots"]:
            directory = relative_project_path(
                self.project_root, shot["directory"], f"directory for {shot['id']}"
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
        found = {"blender": [], "ffmpeg": [], "resolve": []}
        if os.name == "nt":
            executable = shutil.which("tasklist.exe")
            command = [executable, "/FO", "CSV", "/NH"] if executable else None
        else:
            executable = shutil.which("ps")
            command = [executable, "-eo", "pid=,comm="] if executable else None
        if not command:
            return found
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, errors="replace", timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return found

        rows = csv.reader(result.stdout.splitlines()) if os.name == "nt" else (
            line.strip().split(None, 1) for line in result.stdout.splitlines()
        )
        for row in rows:
            if len(row) < 2:
                continue
            if os.name == "nt":
                image_name = row[0].lower()
                pid = safe_int(row[1])
            else:
                pid = safe_int(row[0])
                image_name = Path(row[1]).name.lower()
            if pid is None:
                continue
            for name in found:
                if image_name in {name, f"{name}.exe"}:
                    found[name].append(pid)
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
            return copy.deepcopy(self.probe_cache_result)

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
                        "r_frame_rate,avg_frame_rate,duration,pix_fmt,color_range,color_space,"
                        "color_transfer,color_primaries,codec_tag_string,nb_read_frames"
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
            "avg_fps": parse_rate(video.get("avg_frame_rate")),
            "avg_fps_raw": video.get("avg_frame_rate"),
            "video_duration_seconds": finite_number(video.get("duration")),
            "audio_durations": [
                finite_number(item.get("duration"))
                for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
            ],
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
        if str(actual["profile"] or "").lower() != expected["profile"].lower():
            issues.append(f"编码 Profile 为 {actual['profile'] or '未知'}，要求 {expected['profile']}")
        if (actual["width"], actual["height"]) != (expected["width"], expected["height"]):
            issues.append(
                f"分辨率为 {actual['width'] or '—'}×{actual['height'] or '—'}，"
                f"要求 {expected['width']}×{expected['height']}"
            )
        if actual["fps"] is None or abs(actual["fps"] - expected["fps"]) > 0.001:
            issues.append(
                f"帧率为 {actual['fps_raw'] or '未知'}，要求 {expected['fps']:g} fps"
            )
        if actual["avg_fps"] is None or abs(actual["avg_fps"] - expected["fps"]) > 0.001:
            issues.append(f"平均帧率为 {actual['avg_fps_raw'] or '未知'}，要求 {expected['fps']:g} fps")
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
        expected_duration = expected["frames"] / expected["fps"]
        tolerance = max(1.0 / expected["fps"], 0.05)
        duration = actual["duration_seconds"]
        if duration is None or abs(duration - expected_duration) > tolerance:
            issues.append(f"容器时长为 {duration if duration is not None else '未知'}，应约为 {expected_duration:.6f} 秒")
        video_duration = actual["video_duration_seconds"]
        if video_duration is not None and abs(video_duration - expected_duration) > tolerance:
            issues.append(f"视频流时长为 {video_duration}，应约为 {expected_duration:.6f} 秒")
        for index, audio_duration in enumerate(actual["audio_durations"]):
            if audio_duration is not None and abs(audio_duration - expected_duration) > tolerance:
                issues.append(f"音轨 {index} 时长为 {audio_duration}，应约为 {expected_duration:.6f} 秒")
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

    def _apply_delivery_report(self, delivery: dict, report) -> dict:
        delivery = copy.deepcopy(delivery)
        decode_passed = None
        sha256_recorded = False
        report_valid = False
        if not delivery["exists"]:
            delivery["issues"].append("尚无绑定当前成片的交付 QA 报告")
        elif not isinstance(report, dict):
            delivery["issues"].append("尚无有效 delivery-report.json")
        else:
            media = report.get("media") if isinstance(report.get("media"), dict) else {}
            verification = report.get("verification") if isinstance(report.get("verification"), dict) else {}
            final_path = relative_project_path(
                self.project_root, self.config["final_media"], "final_media"
            )
            stat = final_path.stat()
            expected_path = self.config["final_media"].replace("\\", "/")
            actual_path = str(media.get("path") or "").replace("\\", "/")
            sha256_value = media.get("sha256")
            sha_is_hex = isinstance(sha256_value, str) and re.fullmatch(r"[0-9a-f]{64}", sha256_value) is not None
            identity_matches = (
                actual_path == expected_path
                and media.get("size_bytes") == stat.st_size
                and media.get("mtime_ns") == stat.st_mtime_ns
                and media.get("ctime_ns") == stat.st_ctime_ns
                and media.get("inode") == getattr(stat, "st_ino", 0)
            )
            expected_matches = report.get("expected") == self.config["expected"]
            probe_matches = report.get("actual") == delivery["actual"]
            storyboard_matches = report.get("storyboard_sha256") == self.config["storyboard_sha256"]
            if not identity_matches:
                delivery["issues"].append("QA 报告与当前成片路径或文件身份不一致")
            if not expected_matches or not probe_matches or not storyboard_matches:
                delivery["issues"].append("QA 报告与当前 storyboard 或媒体探测结果不一致")
            if not sha_is_hex:
                delivery["issues"].append("QA 报告没有有效的 SHA-256")
            elif identity_matches:
                digest = hashlib.sha256()
                with final_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                sha256_recorded = digest.hexdigest() == sha256_value
                if not sha256_recorded:
                    delivery["issues"].append("当前成片 SHA-256 与 QA 报告不一致")
            verification_passed = (
                report.get("status") == "complete"
                and verification.get("spec_passed") is True
                and verification.get("decode_passed") is True
                and verification.get("stable_file") is True
            )
            if not verification_passed:
                delivery["issues"].append("尚无当前成片的全片解码通过记录")
            report_valid = (
                identity_matches and expected_matches and probe_matches and storyboard_matches
                and sha256_recorded and verification_passed
            )
            if report_valid:
                decode_passed = True

        delivery["decode_passed"] = decode_passed
        delivery["sha256_recorded"] = sha256_recorded
        delivery["qa_passed"] = bool(delivery["spec_passed"] and report_valid)
        return delivery

    def _live_field(self, data, *keys, default=None):
        value = data
        for key in keys:
            if not isinstance(value, dict):
                return default
            value = value.get(key)
        return value if value is not None else default

    def _build_summary(self) -> dict:
        generated_at = iso_timestamp()
        render_state, shot_state, resolve_state, delivery_report = self._canonical_state()
        validated_shots = self._validated_shots(shot_state)
        shots, frame_count, newest_frame_at, preview = self._scan_shots(validated_shots)
        processes = self._processes()
        gpu = self._gpu()
        memory = self._memory()
        final_path = relative_project_path(
            self.project_root, self.config["final_media"], "final_media"
        )
        delivery = self._apply_delivery_report(self._probe_media(final_path), delivery_report)

        source_files = sum(
            item is not None for item in (render_state, shot_state, resolve_state, delivery_report)
        )
        source = "mixed" if source_files else "derived"
        render_state = render_state if isinstance(render_state, dict) else {}
        resolve_state = resolve_state if isinstance(resolve_state, dict) else {}
        expected_frames = self.config["expected"]["frames"]
        frame_count = min(frame_count, expected_frames)
        percent = round(clamp(100 * frame_count / expected_frames), 1)
        all_frames_present = bool(shots) and all(item["inventory_complete"] for item in shots)
        all_frames_verified = bool(shots) and all(item["verified"] for item in shots)
        if delivery["qa_passed"] and not (all_frames_present and all_frames_verified):
            delivery["issues"].append("全部镜头 frames_validated 通过后才能判定最终完成")
            delivery["qa_passed"] = False

        canonical_stage = str(render_state.get("stage") or "").lower()
        canonical_status = str(render_state.get("status") or "").lower()
        heartbeat_at = self._live_field(render_state, "health", "heartbeat_at")
        stale_after = safe_int(
            self._live_field(render_state, "health", "stale_after_seconds"), 90
        )
        heartbeat_age = None
        active_canonical = canonical_status in {"running", "cooldown"}
        stale = active_canonical
        if isinstance(heartbeat_at, str):
            try:
                heartbeat_time = datetime.fromisoformat(heartbeat_at).timestamp()
                heartbeat_age = max(0, int(time.time() - heartbeat_time))
                stale = active_canonical and heartbeat_age > stale_after
            except (ValueError, OverflowError):
                heartbeat_at = None
        else:
            heartbeat_at = None

        if delivery["qa_passed"]:
            stage = "delivery"
            status = "complete"
        elif canonical_status == "failed" and canonical_stage in ALLOWED_STAGES:
            stage = canonical_stage
            status = "failed"
        elif (
            active_canonical
            and canonical_stage in ALLOWED_STAGES
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
            status = "failed" if (
                not delivery["spec_passed"]
                or isinstance(delivery_report, dict) and delivery_report.get("status") == "failed"
            ) else "idle"

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

        samples = self._live_field(
            render_state, "current", "recent_frame_durations_seconds", default=[]
        )
        samples = [value for item in samples if (value := finite_number(item)) is not None and value > 0] if isinstance(samples, list) else []
        median_frame_seconds = statistics.median(samples) if samples else None
        eta_seconds = None
        if status == "running" and stage == "render" and median_frame_seconds is not None:
            eta_seconds = int(max(0, expected_frames - frame_count) * median_frame_seconds)

        recovery = (
            render_state.get("recovery")
            if isinstance(render_state.get("recovery"), dict)
            else {}
        )
        active_shot_id = self._live_field(render_state, "current", "shot_id")
        current_shot = next(
            (item for item in shots if item["id"] == active_shot_id),
            next(
                (item for item in shots if not item["inventory_complete"]),
                shots[-1] if shots else None,
            ),
        )
        batch_start = self._live_field(render_state, "current", "batch_start")
        batch_end = self._live_field(render_state, "current", "batch_end")
        batch_label = f"{batch_start}–{batch_end}" if batch_start is not None and batch_end is not None else None
        active_pids = processes["blender"] + processes["resolve"] + processes["ffmpeg"]
        state_mtimes = []
        for path in (
            self.project_root / "render" / "render-state.json",
            self.project_root / "shot-status.json",
            self.project_root / "edit" / "resolve-run.json",
            self.project_root / "delivery" / "delivery-report.json",
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
                "batch": batch_label,
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
                "render_job_status": str(self._live_field(resolve_state, "render_job", "status", default="") or ""),
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
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
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

    def _request_allowed(self) -> bool:
        host_values = self.headers.get_all("Host") or []
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        allowed = len(host_values) == 1 and fetch_site in {"", "none", "same-origin"}
        host = host_values[0].strip().lower() if allowed else ""
        extra_hosts = self.server.allowed_hosts
        if allowed and host not in extra_hosts:
            try:
                parsed = urlsplit("//" + host)
                port = parsed.port
                allowed = (
                    parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and not parsed.username and not parsed.path
                    and port in {None, self.server.server_address[1]}
                )
            except ValueError:
                allowed = False
        origin_values = self.headers.get_all("Origin") or []
        if allowed and origin_values:
            if len(origin_values) != 1:
                allowed = False
            else:
                try:
                    origin = urlsplit(origin_values[0])
                    origin_host = origin.hostname
                    origin_port = origin.port
                    local_origin = origin_host in {"127.0.0.1", "localhost", "::1"} and origin_port in {
                        None, self.server.server_address[1]
                    }
                    extra_origin = origin.netloc.lower() in extra_hosts
                    allowed = (
                        origin.scheme in {"http", "https"}
                        and (local_origin or extra_origin)
                        and not origin.username and not origin.path
                    )
                except ValueError:
                    allowed = False
        if not allowed:
            self._send_json({"error": "forbidden request origin"}, HTTPStatus.FORBIDDEN)
        return allowed

    def do_GET(self):
        if not self._request_allowed():
            return
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
        if not self._request_allowed():
            return
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self._security_headers()
        self.end_headers()

    def do_POST(self):
        if not self._request_allowed():
            return
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self._security_headers()
        self.end_headers()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, monitor, allowed_hosts=None):
        super().__init__(address, handler)
        self.monitor = monitor
        self.allowed_hosts = {value.strip().lower() for value in (allowed_hosts or []) if value.strip()}


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
    parser.add_argument(
        "--allowed-host", action="append", default=[],
        help="Additional exact Host for an authenticated tunnel; repeat as needed",
    )
    return parser.parse_args()


def main():
    args = arguments()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing non-loopback bind; use a protected tunnel explicitly.")
    monitor = Monitor(args.project_root, args.config)
    monitor.snapshot(force=True)
    server = DashboardServer(
        (args.host, args.port), DashboardHandler, monitor, allowed_hosts=args.allowed_host
    )
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
