#!/usr/bin/env python3
"""Shared contracts for the Blender story-video production scripts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path, PureWindowsPath

import jsonschema

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = SKILL_ROOT / "assets" / "schemas"
SCHEMA_FILES = {
    "storyboard": "storyboard.schema.json",
    "shot-status": "shot-status.schema.json",
    "render-state": "render-state.schema.json",
    "resolve-run": "resolve-run.schema.json",
    "delivery-report": "delivery-report.schema.json",
}


class WorkflowError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"Missing JSON file: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise WorkflowError(f"Cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def validate_document(kind: str, value: dict) -> None:
    schema_path = SCHEMA_DIR / SCHEMA_FILES[kind]
    schema = read_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        messages = []
        for error in errors[:20]:
            location = ".".join(str(item) for item in error.absolute_path) or "<root>"
            messages.append(f"{location}: {error.message}")
        suffix = f"; and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise WorkflowError(f"{kind} schema validation failed: " + "; ".join(messages) + suffix)


def validate_relative_path(value: str, field: str) -> None:
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or ".." in path.parts or ".." in windows.parts:
        raise WorkflowError(f"{field} must be a relative path inside the project: {value}")


def project_path(root: Path, value: str, field: str) -> Path:
    validate_relative_path(value, field)
    root = root.resolve()
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkflowError(f"{field} escapes the project root: {value}")
    return candidate


def validate_storyboard(value: dict, project_root: Path | None = None) -> None:
    validate_document("storyboard", value)
    delivery = value["delivery"]
    source_width = delivery["source_width"]
    source_height = delivery["source_height"]
    width = delivery["width"]
    height = delivery["height"]
    aspect = Fraction(*(int(part) for part in delivery["aspect_ratio"].split(":")))
    if Fraction(width, height) != aspect or Fraction(source_width, source_height) != aspect:
        raise WorkflowError("Source and delivery dimensions must match aspect_ratio")
    if delivery["upscaled_4k"]:
        if (width, height) not in {(2160, 3840), (3840, 2160)}:
            raise WorkflowError("upscaled_4k requires a 4K delivery resolution")
        if (width, height) != (source_width * 2, source_height * 2):
            raise WorkflowError("upscaled_4k requires an exact 2x source-to-delivery scale")
    elif (source_width, source_height) != (width, height):
        raise WorkflowError("Non-upscaled delivery must keep source and delivery dimensions equal")

    expected_start = 1
    identifiers = set()
    frame_directories = set()
    clip_paths = set()
    for index, shot in enumerate(value["shots"]):
        prefix = f"shots[{index}]"
        shot_id = shot["id"]
        if shot_id in identifiers:
            raise WorkflowError(f"Duplicate shot id: {shot_id}")
        identifiers.add(shot_id)
        if shot["timeline_frame_start"] != expected_start:
            raise WorkflowError(
                f"{prefix}.timeline_frame_start must be {expected_start}; ranges must be contiguous"
            )
        duration = shot["timeline_frame_end"] - shot["timeline_frame_start"] + 1
        if duration != shot["duration_frames"]:
            raise WorkflowError(f"{prefix} timeline range does not match duration_frames")
        if shot["source_frame_start"] != 0:
            raise WorkflowError(f"{prefix}.source_frame_start must be 0 for generated shot MP4 files")
        if shot["source_frame_end"] - shot["source_frame_start"] + 1 != duration:
            raise WorkflowError(f"{prefix} source range does not match duration_frames")
        if shot["record_frame"] != shot["timeline_frame_start"] - 1:
            raise WorkflowError(f"{prefix}.record_frame must equal timeline_frame_start - 1")
        if shot["hold_frames"] > duration:
            raise WorkflowError(f"{prefix}.hold_frames cannot exceed duration_frames")
        for field in ("frame_directory", "clip_path", "reference_path"):
            if shot[field]:
                validate_relative_path(shot[field], f"{prefix}.{field}")
        if not shot["frame_directory"] or not shot["clip_path"]:
            raise WorkflowError(f"{prefix} frame_directory and clip_path are required")
        if shot["frame_directory"] in frame_directories:
            raise WorkflowError(f"Duplicate frame directory: {shot['frame_directory']}")
        if shot["clip_path"] in clip_paths:
            raise WorkflowError(f"Duplicate clip path: {shot['clip_path']}")
        frame_directories.add(shot["frame_directory"])
        clip_paths.add(shot["clip_path"])
        if project_root is not None:
            project_path(project_root, shot["frame_directory"], f"{prefix}.frame_directory")
            project_path(project_root, shot["clip_path"], f"{prefix}.clip_path")
        expected_start = shot["timeline_frame_end"] + 1

    if expected_start - 1 != delivery["total_frames"]:
        raise WorkflowError("Shot timeline ranges must end at delivery.total_frames")
    for field in ("approved_blend",):
        validate_relative_path(value["project"][field], f"project.{field}")
    validate_relative_path(delivery["final_media"], "delivery.final_media")
    if project_root is not None:
        project_path(project_root, value["project"]["approved_blend"], "project.approved_blend")
        project_path(project_root, delivery["final_media"], "delivery.final_media")


def load_storyboard(project_root: Path, path: Path | None = None) -> tuple[dict, Path]:
    storyboard_path = path or project_root / "storyboard.json"
    value = read_json(storyboard_path)
    validate_storyboard(value, project_root)
    return value, storyboard_path


def expected_media(storyboard: dict) -> dict:
    delivery = storyboard["delivery"]
    fps = delivery["fps"]
    return {
        "width": delivery["width"],
        "height": delivery["height"],
        "fps": fps["numerator"] / fps["denominator"],
        "frames": delivery["total_frames"],
        "codec": delivery["codec"],
        "profile": delivery["profile"],
        "pix_fmt": delivery["pixel_format"],
        "color": delivery["color_space"],
        "color_range": delivery["color_range"],
        "codec_tag": delivery["codec_tag"],
        "audio_streams": delivery["audio_streams"],
    }


def source_plan(storyboard: dict, project_root: Path) -> list[dict]:
    plan = []
    for shot in storyboard["shots"]:
        plan.append({
            "shot_id": shot["id"],
            "source_path": str(project_path(project_root, shot["clip_path"], "clip_path")),
            "timeline_frame_start": shot["timeline_frame_start"],
            "timeline_frame_end": shot["timeline_frame_end"],
            "source_frame_start": shot["source_frame_start"],
            "source_frame_end": shot["source_frame_end"],
            "record_frame": shot["record_frame"],
            "duration_frames": shot["duration_frames"],
            "track_index": 1,
        })
    return plan


def require_executable(value: str, name: str) -> str:
    executable = shutil.which(value) if not Path(value).is_file() else str(Path(value).resolve())
    if not executable:
        raise WorkflowError(f"Cannot find {name} executable: {value}")
    return executable


def command_output(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(f"Command failed to complete: {command[0]}: {exc}") from exc


def tool_version(executable: str) -> str:
    result = command_output([executable, "-version"], timeout=20)
    return (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else "unknown"


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _fps(value):
    try:
        return float(Fraction(str(value)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_media(executable: str, path: Path) -> dict:
    entries = (
        "format=duration,size:stream=index,codec_type,codec_name,profile,width,height,"
        "r_frame_rate,avg_frame_rate,duration,pix_fmt,color_range,color_space,color_transfer,"
        "color_primaries,codec_tag_string,nb_read_frames"
    )
    result = command_output([
        executable, "-v", "error", "-count_frames", "-show_entries", entries,
        "-of", "json", str(path)
    ])
    if result.returncode != 0:
        raise WorkflowError(f"ffprobe failed for {path}: {result.stderr.strip()[:2000]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    if not video:
        raise WorkflowError(f"No video stream found in {path}")
    format_data = payload.get("format", {})
    return {
        "codec": video.get("codec_name"),
        "profile": video.get("profile"),
        "width": _integer(video.get("width")),
        "height": _integer(video.get("height")),
        "fps": _fps(video.get("r_frame_rate")),
        "fps_raw": video.get("r_frame_rate"),
        "avg_fps": _fps(video.get("avg_frame_rate")),
        "avg_fps_raw": video.get("avg_frame_rate"),
        "video_duration_seconds": _number(video.get("duration")),
        "audio_durations": [
            _number(item.get("duration")) for item in streams if item.get("codec_type") == "audio"
        ],
        "pix_fmt": video.get("pix_fmt"),
        "color_range": video.get("color_range"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "codec_tag": video.get("codec_tag_string"),
        "frames": _integer(video.get("nb_read_frames")),
        "duration_seconds": _number(format_data.get("duration")),
        "audio_streams": sum(1 for item in streams if item.get("codec_type") == "audio"),
    }


def media_spec_issues(actual: dict, expected: dict) -> list[str]:
    issues = []
    expected_codecs = {"hevc", "h265"} if expected["codec"] in {"hevc", "h265"} else {expected["codec"]}
    if actual.get("codec") not in expected_codecs:
        issues.append(f"codec: expected {expected['codec']}, got {actual.get('codec') or 'unknown'}")
    if str(actual.get("profile") or "").lower() != expected["profile"].lower():
        issues.append(f"profile: expected {expected['profile']}, got {actual.get('profile') or 'unknown'}")
    if (actual.get("width"), actual.get("height")) != (expected["width"], expected["height"]):
        issues.append(
            f"resolution: expected {expected['width']}x{expected['height']}, "
            f"got {actual.get('width')}x{actual.get('height')}"
        )
    if actual.get("fps") is None or abs(actual["fps"] - expected["fps"]) > 0.001:
        issues.append(f"fps: expected {expected['fps']:g}, got {actual.get('fps_raw') or 'unknown'}")
    if actual.get("avg_fps") is None or abs(actual["avg_fps"] - expected["fps"]) > 0.001:
        issues.append(f"average fps: expected {expected['fps']:g}, got {actual.get('avg_fps_raw') or 'unknown'}")
    if actual.get("frames") != expected["frames"]:
        issues.append(f"frames: expected {expected['frames']}, got {actual.get('frames')}")
    expected_duration = expected["frames"] / expected["fps"]
    tolerance = max(1.0 / expected["fps"], 0.05)
    container_duration = actual.get("duration_seconds")
    if container_duration is None or abs(container_duration - expected_duration) > tolerance:
        issues.append(
            f"container duration: expected about {expected_duration:.6f}s, got {container_duration}"
        )
    for key, label in (("video_duration_seconds", "video duration"),):
        value = actual.get(key)
        if value is not None and abs(value - expected_duration) > tolerance:
            issues.append(f"{label}: expected about {expected_duration:.6f}s, got {value}")
    for index, value in enumerate(actual.get("audio_durations") or []):
        if value is not None and abs(value - expected_duration) > tolerance:
            issues.append(f"audio stream {index} duration: expected about {expected_duration:.6f}s, got {value}")
    if actual.get("pix_fmt") != expected["pix_fmt"]:
        issues.append(f"pixel format: expected {expected['pix_fmt']}, got {actual.get('pix_fmt')}")
    for key in ("color_space", "color_transfer", "color_primaries"):
        if str(actual.get(key) or "").lower() != expected["color"]:
            issues.append(f"{key}: expected {expected['color']}, got {actual.get(key) or 'unknown'}")
    if actual.get("color_range") != expected["color_range"]:
        issues.append(f"color range: expected {expected['color_range']}, got {actual.get('color_range')}")
    if str(actual.get("codec_tag") or "").lower() != expected["codec_tag"]:
        issues.append(f"codec tag: expected {expected['codec_tag']}, got {actual.get('codec_tag')}")
    if actual.get("audio_streams") != expected["audio_streams"]:
        issues.append(f"audio streams: expected {expected['audio_streams']}, got {actual.get('audio_streams')}")
    return issues


def media_identity(path: Path, project_root: Path) -> dict:
    try:
        stat = path.stat()
    except OSError as exc:
        raise WorkflowError(f"Cannot stat final media {path}: {exc}") from exc
    try:
        relative = str(path.resolve().relative_to(project_root.resolve()))
    except ValueError as exc:
        raise WorkflowError("Final media must remain inside project_root") from exc
    return {
        "path": relative.replace(os.sep, "/"),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "inode": getattr(stat, "st_ino", 0),
        "sha256": sha256_file(path),
    }


def verify_final_media(
    storyboard: dict,
    storyboard_path: Path,
    project_root: Path,
    media_path: Path,
    ffprobe: str,
    ffmpeg: str,
    decode_timeout: int = 7200,
) -> dict:
    initial_identity = media_identity(media_path, project_root)
    expected = expected_media(storyboard)
    actual = probe_media(ffprobe, media_path)
    issues = media_spec_issues(actual, expected)
    decoded = command_output([
        ffmpeg, "-v", "error", "-xerror", "-i", str(media_path),
        "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"
    ], timeout=decode_timeout)
    decode_passed = decoded.returncode == 0
    if not decode_passed:
        issues.append("full decode: " + (decoded.stderr.strip()[:2000] or "unknown ffmpeg error"))
    final_identity = media_identity(media_path, project_root)
    stable_file = initial_identity == final_identity
    if not stable_file:
        issues.append("media changed during verification; repeat QA on a stable file")
    report = {
        "schema_version": 1,
        "status": "complete" if not issues and decode_passed and stable_file else "failed",
        "storyboard_sha256": sha256_file(storyboard_path),
        "media": final_identity,
        "expected": expected,
        "actual": actual,
        "verification": {
            "spec_passed": not media_spec_issues(actual, expected),
            "decode_passed": decode_passed,
            "stable_file": stable_file,
        },
        "tool_versions": {"ffprobe": tool_version(ffprobe), "ffmpeg": tool_version(ffmpeg)},
        "issues": issues,
        "created_at": utc_now(),
    }
    validate_document("delivery-report", report)
    return report


def validate_optional_states(project_root: Path, storyboard: dict, storyboard_path: Path) -> list[str]:
    messages = []
    shot_ids = {shot["id"] for shot in storyboard["shots"]}
    candidates = [
        ("shot-status", project_root / "shot-status.json"),
        ("render-state", project_root / "render" / "render-state.json"),
        ("resolve-run", project_root / "edit" / "resolve-run.json"),
        ("delivery-report", project_root / "delivery" / "delivery-report.json"),
    ]
    for kind, path in candidates:
        if not path.is_file():
            messages.append(f"SKIP {kind}: not present")
            continue
        value = read_json(path)
        validate_document(kind, value)
        if kind == "shot-status":
            actual_ids = {shot["shot_id"] for shot in value["shots"]}
            if len(actual_ids) != len(value["shots"]):
                raise WorkflowError("shot-status contains duplicate shot ids")
            if actual_ids != shot_ids:
                raise WorkflowError(f"shot-status shot ids differ from storyboard: {sorted(actual_ids ^ shot_ids)}")
        elif kind == "render-state":
            if value["storyboard_sha256"] != sha256_file(storyboard_path):
                raise WorkflowError("render-state belongs to a different storyboard revision")
            if value["current"]["shot_id"] not in shot_ids | {None}:
                raise WorkflowError("render-state current.shot_id is not in storyboard")
            if value["frames"]["expected_total"] != storyboard["delivery"]["total_frames"]:
                raise WorkflowError("render-state expected_total differs from storyboard")
            if set(value["frames"]["verified_by_shot"]) != shot_ids:
                raise WorkflowError("render-state verified shot ids differ from storyboard")
            for shot in storyboard["shots"]:
                valid_range = range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1)
                if any(frame not in valid_range for frame in value["frames"]["verified_by_shot"][shot["id"]]):
                    raise WorkflowError(f"render-state has out-of-range verified frames for {shot['id']}")
        elif kind == "resolve-run":
            expected_plan = source_plan(storyboard, project_root)
            if value["source_plan"] != expected_plan:
                raise WorkflowError("resolve-run source_plan differs from storyboard frame/source mapping")
        elif kind == "delivery-report":
            if value["storyboard_sha256"] != sha256_file(storyboard_path):
                raise WorkflowError("delivery-report belongs to a different storyboard revision")
            if value["expected"] != expected_media(storyboard):
                raise WorkflowError("delivery-report expected media differs from storyboard")
            final_path = project_path(project_root, storyboard["delivery"]["final_media"], "final_media")
            if not final_path.is_file():
                raise WorkflowError("delivery-report exists but final media is missing")
            if media_identity(final_path, project_root) != value["media"]:
                raise WorkflowError("delivery-report does not match the current final media identity or SHA-256")
        messages.append(f"OK {kind}: {path}")
    return messages
