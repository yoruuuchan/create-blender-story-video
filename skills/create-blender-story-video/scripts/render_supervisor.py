#!/usr/bin/env python3
"""Run resumable Blender batches independently of the interactive agent session."""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from production import (
    WorkflowError,
    load_storyboard,
    project_path,
    read_json,
    require_executable,
    sha256_file,
    utc_now,
    validate_document,
    write_json_atomic,
)
from verify_frames import GATE_NAMES, ensure_shot_status, inspect_shot_frames, record_frame_gate


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise WorkflowError("Another render supervisor already owns this project") from exc
        return self

    def __exit__(self, *_):
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=3, check=False,
            )
            if result.returncode != 0:
                return True
            rows = list(csv.reader(result.stdout.splitlines()))
            return any(len(row) > 1 and row[1].strip() == str(pid) for row in rows)
        except (OSError, subprocess.TimeoutExpired):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class StateStore:
    def __init__(self, path: Path, value: dict):
        self.path = path
        self.value = value
        self.lock = threading.Lock()

    def write(self) -> None:
        with self.lock:
            self.value["updated_at"] = utc_now()
            validate_document("render-state", self.value)
            write_json_atomic(self.path, self.value)

    def heartbeat(self) -> None:
        with self.lock:
            self.value["health"]["heartbeat_at"] = utc_now()
            self.value["updated_at"] = utc_now()
            validate_document("render-state", self.value)
            write_json_atomic(self.path, self.value)

    def mark_valid(self, shot_id: str, frame: int, seconds=None) -> None:
        with self.lock:
            frames = self.value["frames"]["verified_by_shot"].setdefault(shot_id, [])
            if frame not in frames:
                frames.append(frame)
                frames.sort()
            self.value["current"]["last_valid_frame"] = max(
                frame, self.value["current"]["last_valid_frame"] or 0
            )
            if seconds is not None:
                samples = self.value["current"]["recent_frame_durations_seconds"]
                samples.append(round(float(seconds), 6))
                del samples[:-20]
            self.value["health"]["last_progress_at"] = utc_now()
            self.value["updated_at"] = utc_now()
            validate_document("render-state", self.value)
            write_json_atomic(self.path, self.value)


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.root = args.project_root.resolve()
        self.storyboard, self.storyboard_path = load_storyboard(self.root, args.storyboard)
        try:
            self.storyboard_path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise WorkflowError("storyboard must be inside project_root") from exc
        self.blend_path = project_path(
            self.root, self.storyboard["project"]["approved_blend"], "approved_blend"
        )
        if not self.blend_path.is_file():
            raise WorkflowError(f"Approved blend does not exist: {self.blend_path}")
        self.blender = require_executable(args.blender, "Blender")
        self.state_path = self.root / "render" / "render-state.json"
        self.logs_dir = self.root / "render" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.stop_heartbeat = threading.Event()
        self.heartbeat_thread = None
        self.store = None
        self.verified = {}
        self.shot_by_id = {shot["id"]: shot for shot in self.storyboard["shots"]}

    def initial_state(self) -> dict:
        previous = read_json(self.state_path) if self.state_path.is_file() else None
        if previous is not None:
            validate_document("render-state", previous)
            if previous["storyboard_sha256"] != sha256_file(self.storyboard_path):
                raise WorkflowError("Refusing to resume with a different storyboard revision")
            if previous["blend_sha256"] != sha256_file(self.blend_path):
                raise WorkflowError("Approved blend SHA-256 changed; create a new render state")
            active = previous["current"]["active_process"]
            if active and pid_exists(active["pid"]):
                raise WorkflowError(
                    f"Previous active_pid {active['pid']} is still alive. Verify its executable and command; "
                    "the supervisor will not kill or render alongside an unidentified process."
                )
        settings = self.storyboard["render"]
        recovery = previous["recovery"] if previous else {
            "batch_retry_count": 0,
            "total_crash_count": 0,
            "reboot_resume_count": 0,
            "auto_resume_enabled": False,
            "last_error_signature": "",
        }
        if self.args.enable_auto_resume:
            recovery["auto_resume_enabled"] = True
        if self.args.reboot_resume:
            if not recovery["auto_resume_enabled"]:
                raise WorkflowError("--reboot-resume requires prior explicit --enable-auto-resume")
            if recovery["reboot_resume_count"] >= settings["max_reboot_resumes"]:
                message = "Automatic reboot resume budget is exhausted"
                previous["status"] = "failed"
                previous["stage"] = "render"
                previous["current"]["active_process"] = None
                previous["recovery"]["auto_resume_enabled"] = False
                previous["recovery"]["last_error_signature"] = message
                previous["updated_at"] = utc_now()
                validate_document("render-state", previous)
                write_json_atomic(self.state_path, previous)
                raise WorkflowError(message)
            recovery["reboot_resume_count"] += 1
        return {
            "schema_version": 1,
            "supervisor_id": uuid.uuid4().hex,
            "storyboard_sha256": sha256_file(self.storyboard_path),
            "blend_path": self.storyboard["project"]["approved_blend"],
            "blend_sha256": sha256_file(self.blend_path),
            "stage": "render",
            "status": "idle",
            "current": {
                "shot_id": None,
                "batch_id": None,
                "batch_start": None,
                "batch_end": None,
                "next_frame": 1,
                "last_valid_frame": None,
                "recent_frame_durations_seconds": (
                    previous["current"]["recent_frame_durations_seconds"] if previous else []
                ),
                "active_process": None,
            },
            "frames": {
                "expected_total": self.storyboard["delivery"]["total_frames"],
                "verified_by_shot": {shot["id"]: [] for shot in self.storyboard["shots"]},
            },
            "health": {
                "heartbeat_at": utc_now(),
                "last_progress_at": None,
                "stale_after_seconds": settings["stale_after_seconds"],
            },
            "recovery": recovery,
            "updated_at": utc_now(),
        }

    def check_creative_gates(self) -> None:
        status, _ = ensure_shot_status(self.root, self.storyboard)
        entries = {entry["shot_id"]: entry for entry in status["shots"]}
        if set(entries) != set(self.shot_by_id):
            raise WorkflowError("shot-status shot ids must exactly match storyboard before rendering")
        for shot_id, entry in entries.items():
            for name in GATE_NAMES:
                gate = entry["gates"][name]
                if gate["status"] not in {"pass", "accepted_deviation"}:
                    raise WorkflowError(f"{shot_id} gate {name} is {gate['status']}; rendering is blocked")

    def inventory(self) -> None:
        for shot in self.storyboard["shots"]:
            report = inspect_shot_frames(self.storyboard, self.root, shot, quarantine_invalid=True)
            if report["duplicates"] or report["unexpected_files"]:
                raise WorkflowError(
                    f"{shot['id']} has duplicate or unexpected frame files; resolve them before resume"
                )
            if "PNG color layout is inconsistent across frames" in report["issues"]:
                raise WorkflowError(f"{shot['id']} mixes PNG color layouts; rebuild that shot explicitly")
            self.verified[shot["id"]] = set(report["valid_frames"])
            self.store.value["frames"]["verified_by_shot"][shot["id"]] = sorted(self.verified[shot["id"]])
        self.store.write()

    def first_missing(self, shot: dict) -> int | None:
        valid = self.verified[shot["id"]]
        return next((
            frame for frame in range(shot["timeline_frame_start"], shot["timeline_frame_end"] + 1)
            if frame not in valid
        ), None)

    def update_next(self) -> None:
        for shot in self.storyboard["shots"]:
            missing = self.first_missing(shot)
            if missing is not None:
                self.store.value["current"]["shot_id"] = shot["id"]
                self.store.value["current"]["next_frame"] = missing
                return
        self.store.value["current"]["shot_id"] = None
        self.store.value["current"]["next_frame"] = None

    def start_heartbeat(self) -> None:
        interval = self.storyboard["render"]["heartbeat_seconds"]
        def run():
            while not self.stop_heartbeat.wait(interval):
                try:
                    self.store.heartbeat()
                except Exception as exc:
                    print(f"ERROR heartbeat: {exc}", file=sys.stderr, flush=True)
                    return
        self.heartbeat_thread = threading.Thread(target=run, name="render-heartbeat", daemon=True)
        self.heartbeat_thread.start()

    def stop_heartbeats(self) -> None:
        self.stop_heartbeat.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=5)

    def command(self, shot: dict, start: int, end: int) -> list[str]:
        script = Path(__file__).resolve().with_name("blender_batch_render.py")
        command = [self.blender, "--background"]
        if not self.args.no_factory_startup:
            command.append("--factory-startup")
        command += [
            "--python-exit-code", "1", str(self.blend_path),
            "--threads", str(self.storyboard["render"]["threads"]),
            "--python", str(script), "--",
            "--project-root", str(self.root), "--storyboard", str(self.storyboard_path),
            "--shot-id", shot["id"], "--start", str(start), "--end", str(end),
        ]
        return command

    def run_batch(self, shot: dict, start: int, end: int) -> None:
        batch_id = uuid.uuid4().hex
        command = self.command(shot, start, end)
        stdout_path = self.logs_dir / f"{batch_id}.stdout.log"
        stderr_path = self.logs_dir / f"{batch_id}.stderr.log"
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NEW_PROCESS_GROUP
        with stdout_path.open("w", encoding="utf-8") as stdout_log, stderr_path.open("w", encoding="utf-8") as stderr_log:
            try:
                child = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=stderr_log,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise WorkflowError(f"Cannot start Blender: {exc}") from exc
            self.store.value["current"].update({
                "batch_id": batch_id,
                "batch_start": start,
                "batch_end": end,
                "active_process": {
                    "pid": child.pid,
                    "executable": self.blender,
                    "command": command,
                    "started_at": utc_now(),
                },
            })
            self.store.write()
            events = queue.Queue()
            def read_output():
                for line in child.stdout:
                    stdout_log.write(line)
                    stdout_log.flush()
                    event = None
                    for prefix in ("POOL_PROGRESS", "POOL_COMPLETE", "POOL_QUARANTINE"):
                        if line.startswith(prefix + " "):
                            try:
                                event = (prefix, json.loads(line[len(prefix) + 1:]))
                            except json.JSONDecodeError:
                                event = ("POOL_PROTOCOL_ERROR", {"line": line.strip()[:500]})
                            break
                    events.put((time.monotonic(), event))
                events.put((time.monotonic(), None))

            reader = threading.Thread(target=read_output, name="blender-stdout", daemon=True)
            reader.start()
            last_activity = time.monotonic()
            complete_event = False
            timed_out = False
            try:
                while True:
                    try:
                        observed_at, event = events.get(timeout=1)
                        last_activity = observed_at
                    except queue.Empty:
                        observed_at = None
                        event = "wait"
                    if event is None:
                        break
                    if event == "POOL_PROTOCOL_ERROR":
                        raise WorkflowError("Blender emitted an invalid pool event")
                    if isinstance(event, tuple):
                        prefix, payload = event
                        if prefix == "POOL_PROGRESS" and not payload.get("skipped"):
                            frame = int(payload["frame"])
                            destination = project_path(
                                self.root, shot["frame_directory"], "frame_directory"
                            ) / f"frame_{frame:06d}.png"
                            from pngcheck import validate_png
                            validate_png(
                                destination,
                                self.storyboard["delivery"]["source_width"],
                                self.storyboard["delivery"]["source_height"],
                            )
                            self.store.mark_valid(shot["id"], frame, payload.get("seconds"))
                            self.verified[shot["id"]].add(frame)
                        elif prefix == "POOL_COMPLETE":
                            complete_event = True
                    samples = self.store.value["current"]["recent_frame_durations_seconds"]
                    timeout = self.args.frame_timeout
                    if samples:
                        ordered = sorted(samples)
                        middle = len(ordered) // 2
                        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
                        timeout = min(timeout, max(300, int(median * 4 + 60)))
                    if child.poll() is None and time.monotonic() - last_activity > timeout:
                        timed_out = True
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                        raise WorkflowError(f"Blender exceeded dynamic timeout {timeout}s")
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                return_code = child.wait(timeout=30)
                reader.join(timeout=5)
                self.store.value["current"]["active_process"] = None
                self.store.write()
            if timed_out:
                raise WorkflowError("Blender batch timed out")
            if return_code != 0:
                raise WorkflowError(f"Blender exited {return_code}; see {stderr_path}")
            if not complete_event:
                raise WorkflowError(f"Blender exited without POOL_COMPLETE; see {stdout_path}")

    def capture_batch(self, shot: dict, start: int, end: int) -> None:
        numbers = set(range(start, end + 1))
        report = inspect_shot_frames(self.storyboard, self.root, shot, numbers=numbers)
        for frame in report["valid_frames"]:
            self.verified[shot["id"]].add(frame)
            self.store.value["frames"]["verified_by_shot"][shot["id"]] = sorted(self.verified[shot["id"]])
        self.store.write()
        if not numbers <= self.verified[shot["id"]]:
            missing = sorted(numbers - self.verified[shot["id"]])
            raise WorkflowError(f"Batch ended with invalid or missing frames: {missing}")

    def finish_shot(self, shot: dict) -> None:
        report = inspect_shot_frames(self.storyboard, self.root, shot)
        report_path = self.root / "render" / "validation" / f"{shot['id']}.frames.json"
        write_json_atomic(report_path, report)
        evidence = str(report_path.relative_to(self.root)).replace("\\", "/")
        record_frame_gate(self.root, self.storyboard, report, evidence)
        if report["status"] != "complete":
            raise WorkflowError(f"Full-shot verification failed for {shot['id']}")
        print(f"OK verified {shot['id']}: {shot['duration_frames']} frames", flush=True)

    def cooldown(self, seconds: int) -> None:
        if seconds <= 0:
            return
        self.store.value["status"] = "cooldown"
        self.store.write()
        time.sleep(seconds)
        self.store.value["status"] = "running"
        self.store.write()

    def run(self) -> None:
        self.check_creative_gates()
        self.store = StateStore(self.state_path, self.initial_state())
        self.store.value["status"] = "running"
        self.store.write()
        self.inventory()
        self.start_heartbeat()
        for shot in self.storyboard["shots"]:
            batch_size = self.storyboard["render"]["batch_size"]
            while self.first_missing(shot) is not None:
                self.update_next()
                start = self.first_missing(shot)
                missing = [
                    frame for frame in range(start, shot["timeline_frame_end"] + 1)
                    if frame not in self.verified[shot["id"]]
                ]
                batch = missing[:batch_size]
                end = max(batch)
                self.store.value["current"].update({
                    "shot_id": shot["id"], "batch_start": start, "batch_end": end
                })
                self.store.write()
                try:
                    self.run_batch(shot, start, end)
                    self.capture_batch(shot, start, end)
                    self.store.value["recovery"]["batch_retry_count"] = 0
                    self.store.write()
                    batch_size = self.storyboard["render"]["batch_size"]
                    self.cooldown(self.storyboard["render"]["cooldown_seconds"])
                except Exception as exc:
                    try:
                        self.capture_batch(shot, start, end)
                    except Exception as capture_error:
                        print(f"WARN batch recovery: {capture_error}", file=sys.stderr, flush=True)
                    recovery = self.store.value["recovery"]
                    recovery["batch_retry_count"] += 1
                    recovery["total_crash_count"] += 1
                    recovery["last_error_signature"] = str(exc)[:500]
                    self.store.write()
                    if recovery["batch_retry_count"] > self.storyboard["render"]["max_batch_retries"]:
                        raise WorkflowError(
                            f"Batch retry budget exhausted for {shot['id']} {start}-{end}: {exc}"
                        ) from exc
                    batch_size = max(1, batch_size // 2)
                    print(f"WARN {exc}; retrying with batch_size={batch_size}", file=sys.stderr, flush=True)
                    self.cooldown(self.storyboard["render"]["cooldown_seconds"] * (recovery["batch_retry_count"] + 1))
            self.finish_shot(shot)
        self.update_next()
        self.store.value.update({
            "stage": "render",
            "status": "complete",
            "recovery": self.store.value["recovery"] | {"auto_resume_enabled": False},
        })
        self.store.value["current"].update({
            "batch_id": None, "batch_start": None, "batch_end": None, "active_process": None
        })
        self.store.write()
        print("OK render complete: all shot frames fully decoded and verified.", flush=True)

    def execute(self) -> int:
        lock_path = self.root / "render" / ".render-supervisor.lock"
        try:
            with SingleInstanceLock(lock_path):
                try:
                    self.run()
                    return 0
                except KeyboardInterrupt:
                    if self.store:
                        self.store.value["status"] = "idle"
                        self.store.value["recovery"]["last_error_signature"] = "Interrupted by user"
                        self.store.write()
                    print("Render interrupted; verified frames and resume state were preserved.", file=sys.stderr)
                    return 130
                except Exception as exc:
                    if self.store:
                        self.store.value["status"] = "failed"
                        self.store.value["stage"] = "render"
                        self.store.value["current"]["active_process"] = None
                        self.store.value["recovery"]["auto_resume_enabled"] = False
                        self.store.value["recovery"]["last_error_signature"] = str(exc)[:500]
                        self.store.write()
                    print(f"ERROR: {exc}", file=sys.stderr)
                    return 1
                finally:
                    self.stop_heartbeats()
        except WorkflowError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--storyboard", type=Path)
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--frame-timeout", type=int, default=3600)
    parser.add_argument("--no-factory-startup", action="store_true")
    parser.add_argument("--reboot-resume", action="store_true")
    parser.add_argument("--enable-auto-resume", action="store_true")
    args = parser.parse_args()
    try:
        supervisor = Supervisor(args)
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return supervisor.execute()


if __name__ == "__main__":
    raise SystemExit(main())
