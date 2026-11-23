from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "create-blender-story-video"
SCRIPTS = SKILL / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from production import utc_now, write_json_atomic


def png_bytes(width=64, height=64, rgb=(24, 96, 160), color_type=2, filter_type=0):
    channels = {2: 3, 6: 4}[color_type]
    pixel = bytes(rgb[:channels]) if channels == 3 else bytes((*rgb, 255))
    raw = b"".join(bytes([filter_type]) + pixel * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def indexed_png_bytes(width=64, height=64, bit_depth=8, include_palette=False):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, bit_depth, 3, 0, 0, 0)
    pixel = b"\0\0" if bit_depth == 16 else b"\0"
    raw = b"".join(b"\0" + pixel * width for _ in range(height))
    palette = chunk(b"PLTE", b"\0\0\0") if include_palette else b""
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + palette
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def make_storyboard(width=64, height=64, fps=(30, 1), upscaled=False):
    source_width = width // 2 if upscaled else width
    source_height = height // 2 if upscaled else height
    return {
        "schema_version": 1,
        "production_mode": "autonomous",
        "project": {"name": "test-project", "approved_blend": "blender/approved.blend"},
        "delivery": {
            "aspect_ratio": f"{width // __import__('math').gcd(width, height)}:{height // __import__('math').gcd(width, height)}",
            "source_width": source_width,
            "source_height": source_height,
            "width": width,
            "height": height,
            "fps": {"numerator": fps[0], "denominator": fps[1]},
            "total_frames": 4,
            "codec": "hevc",
            "profile": "Main",
            "pixel_format": "yuv420p",
            "color_space": "bt709",
            "color_range": "tv",
            "codec_tag": "hvc1",
            "audio_streams": 0,
            "upscaled_4k": upscaled,
            "final_media": "delivery/final.mp4",
            "input_color": {
                "primaries": "bt709",
                "transfer": "iec61966-2-1",
                "matrix": "gbr",
                "range": "pc",
            },
        },
        "render": {
            "threads": 1,
            "batch_size": 2,
            "cooldown_seconds": 0,
            "max_batch_retries": 1,
            "max_reboot_resumes": 1,
            "heartbeat_seconds": 1,
            "stale_after_seconds": 30,
        },
        "shots": [
            {
                "id": "shot_01",
                "timeline_frame_start": 1,
                "timeline_frame_end": 2,
                "source_frame_start": 0,
                "source_frame_end": 1,
                "record_frame": 0,
                "duration_frames": 2,
                "subject_action": "subject moves",
                "camera_action": "camera holds",
                "hold_frames": 0,
                "camera": "CAM_SHOT_01",
                "reference_path": "references/shot_01.png",
                "frame_directory": "production-renders/shot_01",
                "clip_path": "production-renders/shot_01/shot_01.mp4",
                "audio_cue": "",
                "acceptance_checks": ["subject remains in frame"],
            },
            {
                "id": "shot_02",
                "timeline_frame_start": 3,
                "timeline_frame_end": 4,
                "source_frame_start": 0,
                "source_frame_end": 1,
                "record_frame": 2,
                "duration_frames": 2,
                "subject_action": "subject settles",
                "camera_action": "camera holds",
                "hold_frames": 1,
                "camera": "CAM_SHOT_02",
                "reference_path": "references/shot_02.png",
                "frame_directory": "production-renders/shot_02",
                "clip_path": "production-renders/shot_02/shot_02.mp4",
                "audio_cue": "",
                "acceptance_checks": ["subject remains in frame"],
            },
        ],
    }


def make_project(root: Path, **kwargs):
    storyboard = make_storyboard(**kwargs)
    (root / "blender").mkdir(parents=True, exist_ok=True)
    (root / "blender" / "approved.blend").write_bytes(b"approved-test-blend")
    write_json_atomic(root / "storyboard.json", storyboard)
    return storyboard


def write_frames(root: Path, storyboard: dict, shot_id=None):
    color_type = 2
    for shot in storyboard["shots"]:
        if shot_id and shot["id"] != shot_id:
            continue
        directory = root / shot["frame_directory"]
        directory.mkdir(parents=True, exist_ok=True)
        for frame in range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1):
            (directory / f"frame_{frame:06d}.png").write_bytes(
                png_bytes(storyboard["delivery"]["source_width"], storyboard["delivery"]["source_height"], color_type=color_type)
            )


def write_shot_status(root: Path, storyboard: dict, frames_pass=False):
    def gate(status):
        return {"status": status, "evidence": ["test"], "method": "test", "checked_at": utc_now()}
    value = {
        "schema_version": 1,
        "shots": [
            {
                "shot_id": shot["id"],
                "gates": {
                    "reference_ready": gate("pass"),
                    "blockout": gate("pass"),
                    "lookdev": gate("pass"),
                    "camera_animation": gate("pass"),
                    "frames_validated": gate("pass" if frames_pass else "pending"),
                },
                "issues": [],
                "next_action": "",
                "updated_at": utc_now(),
            }
            for shot in storyboard["shots"]
        ],
    }
    write_json_atomic(root / "shot-status.json", value)
    return value


def write_config(root: Path):
    write_json_atomic(root / "monitor" / "monitor-config.json", {
        "schema_version": 1,
        "storyboard": "storyboard.json",
        "ffprobe": "",
    })


def write_render_state(root: Path, storyboard: dict, status="running", stage="render", heartbeat=True):
    from production import sha256_file
    write_json_atomic(root / "render" / "render-state.json", {
        "schema_version": 1,
        "supervisor_id": "test-supervisor",
        "storyboard_sha256": sha256_file(root / "storyboard.json"),
        "blend_path": "blender/approved.blend",
        "blend_sha256": sha256_file(root / "blender" / "approved.blend"),
        "stage": stage,
        "status": status,
        "current": {
            "shot_id": "shot_01",
            "batch_id": "batch-1",
            "batch_start": 1,
            "batch_end": 2,
            "next_frame": 1,
            "last_valid_frame": None,
            "recent_frame_durations_seconds": [2.0, 4.0, 6.0],
            "active_process": None,
        },
        "frames": {
            "expected_total": 4,
            "verified_by_shot": {"shot_01": [], "shot_02": []},
        },
        "health": {
            "heartbeat_at": utc_now() if heartbeat else None,
            "last_progress_at": None,
            "stale_after_seconds": 30,
        },
        "recovery": {
            "batch_retry_count": 0,
            "total_crash_count": 0,
            "reboot_resume_count": 0,
            "auto_resume_enabled": False,
            "last_error_signature": "",
        },
        "updated_at": utc_now(),
    })
