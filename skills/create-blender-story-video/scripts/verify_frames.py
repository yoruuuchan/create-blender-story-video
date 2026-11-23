#!/usr/bin/env python3
"""Fully decode PNG sequences and optionally record the frames_validated gate."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pngcheck import PngError, validate_png
from production import (
    WorkflowError,
    load_storyboard,
    project_path,
    read_json,
    utc_now,
    validate_document,
    write_json_atomic,
)

FRAME_PATTERN = re.compile(r"^frame_(\d+)\.png$", re.IGNORECASE)
GATE_NAMES = ("reference_ready", "blockout", "lookdev", "camera_animation")


def inspect_shot_frames(
    storyboard: dict,
    project_root: Path,
    shot: dict,
    numbers: set[int] | None = None,
    quarantine_invalid: bool = False,
) -> dict:
    delivery = storyboard["delivery"]
    directory = project_path(project_root, shot["frame_directory"], "frame_directory")
    expected = set(range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1))
    requested = expected if numbers is None else set(numbers)
    if not requested <= expected:
        raise WorkflowError(f"Requested frames outside {shot['id']} timeline range")

    paths_by_number = {}
    if directory.is_dir():
        for entry in directory.iterdir():
            match = FRAME_PATTERN.match(entry.name)
            if match and entry.is_file():
                paths_by_number.setdefault(int(match.group(1)), []).append(entry)

    valid = set()
    invalid = []
    duplicates = []
    unexpected = []
    layouts = {}
    for number, paths in sorted(paths_by_number.items()):
        canonical = directory / f"frame_{number:06d}.png"
        if len(paths) > 1:
            duplicates.append({"frame": number, "files": [path.name for path in paths]})
        if number not in expected:
            unexpected.extend(path.name for path in paths)
        for path in paths:
            if path != canonical:
                unexpected.append(str(path.name))

    quarantine_root = project_root / "render" / "quarantine"
    for number in sorted(requested):
        path = directory / f"frame_{number:06d}.png"
        if not path.is_file():
            continue
        try:
            metadata = validate_png(path, delivery["source_width"], delivery["source_height"])
            layout = (metadata["bit_depth"], metadata["color_type"], metadata["interlace"])
            layouts.setdefault(layout, []).append(number)
            valid.add(number)
        except PngError as exc:
            record = {"frame": number, "error": str(exc)}
            if quarantine_invalid:
                destination = quarantine_root / shot["id"] / f"{utc_now().replace(':', '-')}-{path.name}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                path.replace(destination)
                record["quarantined_to"] = str(destination)
            invalid.append(record)

    issues = []
    missing = sorted(requested - valid - {item["frame"] for item in invalid})
    if missing:
        issues.append(f"missing {len(missing)} frames")
    if invalid:
        issues.append(f"invalid {len(invalid)} frames")
    if duplicates:
        issues.append(f"duplicate filenames for {len(duplicates)} frame numbers")
    if unexpected:
        issues.append(f"unexpected frame files: {len(unexpected)}")
    if len(layouts) > 1:
        issues.append("PNG color layout is inconsistent across frames")

    complete = (
        numbers is None
        and valid == expected
        and not invalid
        and not duplicates
        and not unexpected
        and len(layouts) == 1
    )
    layout = next(iter(layouts)) if len(layouts) == 1 else None
    return {
        "schema_version": 1,
        "shot_id": shot["id"],
        "status": "complete" if complete else "incomplete",
        "expected_range": [shot["timeline_frame_start"], shot["timeline_frame_end"]],
        "checked_range": [min(requested), max(requested)] if requested else [],
        "valid_frames": sorted(valid),
        "missing_frames": missing,
        "invalid_frames": invalid,
        "duplicates": duplicates,
        "unexpected_files": sorted(unexpected),
        "color_layout": {
            "bit_depth": layout[0], "color_type": layout[1], "interlace": layout[2]
        } if layout else None,
        "issues": issues,
        "checked_at": utc_now(),
    }


def pending_gate():
    return {"status": "pending", "evidence": [], "method": "", "checked_at": utc_now()}


def ensure_shot_status(project_root: Path, storyboard: dict) -> tuple[dict, Path]:
    path = project_root / "shot-status.json"
    if path.is_file():
        value = read_json(path)
    else:
        value = {
            "schema_version": 1,
            "shots": [
                {
                    "shot_id": shot["id"],
                    "gates": {
                        **{name: pending_gate() for name in GATE_NAMES},
                        "frames_validated": pending_gate(),
                    },
                    "issues": [],
                    "next_action": "",
                    "updated_at": utc_now(),
                }
                for shot in storyboard["shots"]
            ],
        }
    validate_document("shot-status", value)
    return value, path


def record_frame_gate(project_root: Path, storyboard: dict, report: dict, evidence: str) -> None:
    value, path = ensure_shot_status(project_root, storyboard)
    entry = next((item for item in value["shots"] if item["shot_id"] == report["shot_id"]), None)
    if entry is None:
        raise WorkflowError(f"shot-status has no entry for {report['shot_id']}")
    passed = report["status"] == "complete"
    entry["gates"]["frames_validated"] = {
        "status": "pass" if passed else "fail",
        "evidence": [evidence],
        "method": "PNG chunk CRC, full zlib decode, dimensions and consistent color layout",
        "checked_at": report["checked_at"],
    }
    entry["issues"] = list(report["issues"])
    entry["next_action"] = "encode_shot" if passed else "repair_or_rerender_frames"
    entry["updated_at"] = report["checked_at"]
    validate_document("shot-status", value)
    write_json_atomic(path, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--shot-id")
    parser.add_argument("--quarantine-invalid", action="store_true")
    parser.add_argument("--update-status", action="store_true")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    root = args.project_root.resolve()
    report_dir = args.report_dir or root / "render" / "validation"
    failed = False
    try:
        storyboard, _ = load_storyboard(root, args.storyboard)
        shots = [shot for shot in storyboard["shots"] if not args.shot_id or shot["id"] == args.shot_id]
        if args.shot_id and not shots:
            raise WorkflowError(f"Unknown shot id: {args.shot_id}")
        for shot in shots:
            report = inspect_shot_frames(
                storyboard, root, shot, quarantine_invalid=args.quarantine_invalid
            )
            report_path = report_dir / f"{shot['id']}.frames.json"
            write_json_atomic(report_path, report)
            evidence = str(report_path.relative_to(root)).replace("\\", "/")
            if args.update_status:
                record_frame_gate(root, storyboard, report, evidence)
            if report["status"] == "complete":
                print(f"OK {shot['id']}: {len(report['valid_frames'])} fully decoded frames; {report_path}")
            else:
                failed = True
                print(f"FAIL {shot['id']}: {', '.join(report['issues'])}; {report_path}", file=sys.stderr)
        return 1 if failed else 0
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
