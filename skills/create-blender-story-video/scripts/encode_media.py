#!/usr/bin/env python3
"""Encode shot intermediates or final HEVC using storyboard-owned specifications."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from production import (
    WorkflowError,
    command_output,
    expected_media,
    load_storyboard,
    media_identity,
    media_spec_issues,
    probe_media,
    project_path,
    require_executable,
    utc_now,
    validate_document,
    verify_final_media,
    write_json_atomic,
)
from verify_frames import inspect_shot_frames


def fps_text(storyboard: dict) -> str:
    fps = storyboard["delivery"]["fps"]
    return f"{fps['numerator']}/{fps['denominator']}"


def video_filter(storyboard: dict, override: dict | None = None) -> str:
    delivery = storyboard["delivery"]
    color = dict(delivery["input_color"])
    if override:
        color.update({key: value for key, value in override.items() if value is not None})
    output = delivery["color_space"]
    width = delivery["width"]
    height = delivery["height"]
    range_in = {"pc": "full", "tv": "limited"}[color["range"]]
    range_out = "limited" if delivery["color_range"] == "tv" else "full"
    if (
        color["matrix"] == "gbr"
        and color["transfer"] == output
        and color["primaries"] == output
    ):
        return (
            f"scale=w={width}:h={height}:flags=lanczos:out_color_matrix={output}:"
            f"out_range={delivery['color_range']}"
        )
    return (
        f"zscale=w={width}:h={height}:filter=lanczos:"
        f"primariesin={color['primaries']}:transferin={color['transfer']}:"
        f"matrixin={color['matrix']}:rangein={range_in}:"
        f"primaries={output}:transfer={output}:matrix={output}:range={range_out},"
        "format=yuv420p"
    )


def decode_or_error(ffmpeg: str, path: Path, timeout: int) -> None:
    result = command_output([
        ffmpeg, "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"
    ], timeout=timeout)
    if result.returncode != 0:
        raise WorkflowError(f"Encoded media failed full decode: {result.stderr.strip()[:2000]}")


def run_encode(command: list[str], dry_run: bool) -> None:
    if dry_run:
        print(json.dumps(command, ensure_ascii=False))
        return
    result = command_output(command, timeout=7200)
    if result.returncode != 0:
        raise WorkflowError(f"ffmpeg encoding failed: {result.stderr.strip()[-4000:]}")


def encode_shot(args, root: Path, storyboard: dict, ffmpeg: str, ffprobe: str) -> None:
    shot = next((item for item in storyboard["shots"] if item["id"] == args.shot_id), None)
    if shot is None:
        raise WorkflowError(f"Unknown shot id: {args.shot_id}")
    frames = inspect_shot_frames(storyboard, root, shot)
    if frames["status"] != "complete":
        raise WorkflowError(f"Frames are not verified for {shot['id']}: {', '.join(frames['issues'])}")
    destination = project_path(root, shot["clip_path"], "clip_path")
    if destination.exists() and not args.replace:
        raise WorkflowError(f"Refusing to replace existing clip without --replace: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp4")
    frame_directory = project_path(root, shot["frame_directory"], "frame_directory")
    fps = fps_text(storyboard)
    command = [
        ffmpeg, "-hide_banner", "-y", "-framerate", fps,
        "-start_number", str(shot["timeline_frame_start"]),
        "-i", str(frame_directory / "frame_%06d.png"),
        "-frames:v", str(shot["duration_frames"]),
        "-vf", video_filter(storyboard),
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", fps,
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-movflags", "+faststart", str(temporary),
    ]
    run_encode(command, args.dry_run)
    if args.dry_run:
        return
    actual = probe_media(ffprobe, temporary)
    expected = expected_media(storyboard) | {
        "frames": shot["duration_frames"], "codec": "h264", "profile": "High",
        "codec_tag": "avc1", "audio_streams": 0,
    }
    issues = media_spec_issues(actual, expected)
    if issues:
        raise WorkflowError(f"Shot validation failed; kept {temporary}: {'; '.join(issues)}")
    decode_or_error(ffmpeg, temporary, args.decode_timeout)
    os.replace(temporary, destination)
    print(f"OK shot clip: {destination}")


def encode_final(args, root: Path, storyboard: dict, storyboard_path: Path, ffmpeg: str, ffprobe: str) -> None:
    if not args.master:
        raise WorkflowError("final requires --master EDIT_MASTER")
    master = args.master.resolve()
    destination = project_path(root, storyboard["delivery"]["final_media"], "final_media")
    if destination.exists() and not args.replace:
        raise WorkflowError(f"Refusing to replace verified media without --replace: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp4")
    fps = fps_text(storyboard)
    override = {
        "primaries": args.input_primaries,
        "transfer": args.input_transfer,
        "matrix": args.input_matrix,
        "range": args.input_range,
    }
    command = [
        ffmpeg, "-hide_banner", "-y", "-i", str(master),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", video_filter(storyboard, override),
        "-c:v", "libx265", "-preset", "slow", "-crf", "18", "-profile:v", "main",
        "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-fps_mode", "cfr", "-r", fps,
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-x265-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
    ]
    if storyboard["delivery"]["audio_streams"]:
        command += ["-c:a", "aac", "-b:a", "320k"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(temporary)]
    run_encode(command, args.dry_run)
    if args.dry_run:
        return
    report = verify_final_media(
        storyboard, storyboard_path, root, temporary, ffprobe, ffmpeg, args.decode_timeout
    )
    if report["status"] != "complete":
        raise WorkflowError(f"Final validation failed; kept {temporary}: {'; '.join(report['issues'])}")
    os.replace(temporary, destination)
    report["media"] = media_identity(destination, root)
    report["created_at"] = utc_now()
    validate_document("delivery-report", report)
    output = root / "delivery" / "delivery-report.json"
    write_json_atomic(output, report)
    print(f"OK final media: {destination}")
    print(f"SHA-256: {report['media']['sha256']}")
    print(f"Delivery report: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["shot", "final"])
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--shot-id")
    parser.add_argument("--master", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--decode-timeout", type=int, default=7200)
    parser.add_argument("--input-primaries")
    parser.add_argument("--input-transfer")
    parser.add_argument("--input-matrix")
    parser.add_argument("--input-range", choices=["pc", "tv"])
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.mode == "shot" and not args.shot_id:
        parser.error("shot requires --shot-id")
    root = args.project_root.resolve()
    try:
        storyboard, storyboard_path = load_storyboard(root, args.storyboard)
        ffmpeg = require_executable(args.ffmpeg, "ffmpeg")
        ffprobe = require_executable(args.ffprobe, "ffprobe")
        if args.mode == "shot":
            encode_shot(args, root, storyboard, ffmpeg, ffprobe)
        else:
            encode_final(args, root, storyboard, storyboard_path, ffmpeg, ffprobe)
        return 0
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
