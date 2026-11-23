#!/usr/bin/env python3
"""Probe and fully decode final media, then write a bound delivery report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from production import (
    WorkflowError,
    load_storyboard,
    project_path,
    require_executable,
    verify_final_media,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--media", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--decode-timeout", type=int, default=7200)
    args = parser.parse_args()
    root = args.project_root.resolve()
    try:
        storyboard, storyboard_path = load_storyboard(root, args.storyboard)
        media = args.media or project_path(root, storyboard["delivery"]["final_media"], "final_media")
        output = args.output or root / "delivery" / "delivery-report.json"
        report = verify_final_media(
            storyboard,
            storyboard_path,
            root,
            media.resolve(),
            require_executable(args.ffprobe, "ffprobe"),
            require_executable(args.ffmpeg, "ffmpeg"),
            decode_timeout=args.decode_timeout,
        )
        write_json_atomic(output, report)
        if report["status"] == "complete":
            print(f"OK final media: {media}")
            print(f"SHA-256: {report['media']['sha256']}")
            print(f"Delivery report: {output}")
            return 0
        print(f"FAIL final media: {'; '.join(report['issues'])}", file=sys.stderr)
        print(f"Failure report: {output}", file=sys.stderr)
        return 1
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
