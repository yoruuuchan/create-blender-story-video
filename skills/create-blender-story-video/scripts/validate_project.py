#!/usr/bin/env python3
"""Validate production JSON contracts and cross-file frame/source mappings."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from production import WorkflowError, load_storyboard, validate_optional_states


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    args = parser.parse_args()
    root = args.project_root.resolve()
    try:
        storyboard, path = load_storyboard(root, args.storyboard)
        print(f"OK storyboard: {len(storyboard['shots'])} shots, {storyboard['delivery']['total_frames']} frames")
        print(f"OK storyboard path: {path}")
        for message in validate_optional_states(root, storyboard, path):
            print(message)
        print("Project contracts are valid.")
        return 0
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
