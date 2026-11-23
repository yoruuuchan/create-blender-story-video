#!/usr/bin/env python3
"""Generate the minimal dashboard config from a validated storyboard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from production import WorkflowError, load_storyboard, validate_relative_path, write_json_atomic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ffprobe", default="")
    args = parser.parse_args()
    root = args.project_root.resolve()
    output = args.output or root / "monitor" / "monitor-config.json"
    try:
        _, storyboard_path = load_storyboard(root, args.storyboard)
        storyboard_reference = str(storyboard_path.resolve().relative_to(root)).replace("\\", "/")
        validate_relative_path(storyboard_reference, "storyboard")
        write_json_atomic(output, {
            "schema_version": 1,
            "storyboard": storyboard_reference,
            "ffprobe": args.ffprobe,
        })
        print(f"OK monitor config: {output}")
        print("Shot ranges and delivery expectations remain owned by storyboard.json.")
        return 0
    except (WorkflowError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
