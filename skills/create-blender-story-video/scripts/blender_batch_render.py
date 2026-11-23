#!/usr/bin/env python3
"""Run inside Blender: render a frame batch with atomic PNG commits and JSON events."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import bpy

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pngcheck import PngError, validate_png


def emit(event: str, **fields) -> None:
    print(f"{event} " + json.dumps(fields, separators=(",", ":")), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--shot-id", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--color-mode", choices=["RGB", "RGBA"], default="RGB")
    parser.add_argument("--color-depth", choices=["8", "16"], default="8")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])

    root = args.project_root.resolve()
    storyboard_path = args.storyboard or root / "storyboard.json"
    storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
    shot = next((item for item in storyboard["shots"] if item["id"] == args.shot_id), None)
    if shot is None:
        raise ValueError(f"Unknown shot id: {args.shot_id}")
    start = max(args.start, shot["timeline_frame_start"])
    end = min(args.end, shot["timeline_frame_end"])
    if start > end:
        raise ValueError("Batch range does not intersect the shot")

    scene = bpy.context.scene
    camera = bpy.data.objects.get(shot["camera"])
    if camera is None or camera.type != "CAMERA":
        raise ValueError(f"Camera not found: {shot['camera']}")
    scene.camera = camera
    delivery = storyboard["delivery"]
    scene.render.resolution_x = delivery["source_width"]
    scene.render.resolution_y = delivery["source_height"]
    scene.render.resolution_percentage = 100
    fps = delivery["fps"]["numerator"] / delivery["fps"]["denominator"]
    scene.render.fps = max(1, round(fps))
    scene.render.fps_base = fps / scene.render.fps
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = args.color_mode
    scene.render.image_settings.color_depth = args.color_depth
    scene.render.use_file_extension = True
    scene.render.threads_mode = "FIXED"
    scene.render.threads = storyboard["render"]["threads"]

    output_dir = root / shot["frame_directory"]
    pending_dir = root / "render" / ".pending" / shot["id"]
    quarantine_dir = root / "render" / "quarantine" / shot["id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    for frame in range(start, end + 1):
        destination = output_dir / f"frame_{frame:06d}.png"
        if destination.is_file():
            try:
                validate_png(destination, delivery["source_width"], delivery["source_height"])
                emit("POOL_PROGRESS", shot_id=shot["id"], frame=frame, skipped=True)
                continue
            except PngError as exc:
                quarantine = quarantine_dir / f"{int(time.time())}-{destination.name}"
                destination.replace(quarantine)
                emit("POOL_QUARANTINE", shot_id=shot["id"], frame=frame, path=str(quarantine), error=str(exc))

        temporary = pending_dir / destination.name
        scene.frame_set(frame)
        scene.render.filepath = str(temporary)
        started = time.monotonic()
        bpy.ops.render.render(write_still=True)
        elapsed = time.monotonic() - started
        metadata = validate_png(temporary, delivery["source_width"], delivery["source_height"])
        temporary.replace(destination)
        emit(
            "POOL_PROGRESS",
            shot_id=shot["id"],
            frame=frame,
            skipped=False,
            seconds=round(elapsed, 6),
            color_type=metadata["color_type"],
            bit_depth=metadata["bit_depth"],
        )

    verified = []
    for frame in range(start, end + 1):
        validate_png(
            output_dir / f"frame_{frame:06d}.png",
            delivery["source_width"],
            delivery["source_height"],
        )
        verified.append(frame)
    emit("POOL_COMPLETE", shot_id=shot["id"], start=start, end=end, verified_frames=verified)


if __name__ == "__main__":
    main()
