"""Record GRM journals alongside Scheduler video recording sessions.

The worker uses independent read-only connections. It never polls monitor
status (which advances some providers), sends motion, or holds Runtime locks
while doing network/file I/O. JSONL is authoritative; CSV is for plotting.
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import re
import csv
import json
import logging
import math
from pathlib import Path
import queue
import threading
import time
import urllib.error
from uuid import uuid4
import zipfile

from robot_runtime.adapters.robot_bridge.clients import BridgeClient

logger = logging.getLogger(__name__)
FIELDS = ["recording_id", "execution_id", "monitor_id", "instruction", "inference_step",
          "progress_time_unix_s", "elapsed_s", "observation_time_unix_s", "observation_elapsed_s",
          "progress", "status", "forward_score", "forward_progress", "incremental_score",
          "incremental_progress", "backward_score", "backward_progress", "baseline_progress",
          "branch_difference", "latency_s"]
CAMERAS = ("cam_high", "cam_left_wrist", "cam_right_wrist")
FIELDS += [f"baseline_{mode}_{metric}" for mode in ("forward", "incremental", "backward") for metric in ("score", "progress")]
FIELDS += [f"{camera}_image" for camera in CAMERAS]


def video_name(info):
    return info.get("episode_name") or (Path(info["episode_dir"]).name if info.get("episode_dir") else None)


def recording_name(info, started, recording_id):
    def part(text, limit):
        value = re.sub(r"[^\w@-]", "-", str(text).strip()).strip("-") or "未填写"
        raw = value.encode("utf-8")
        return value if len(raw) <= limit else raw[:limit-9].decode("utf-8", errors="ignore") + "-" + hashlib.sha256(raw).hexdigest()[:8]
    if video_name(info):
        return part(video_name(info), 230)
    if not info.get("person"):
        return "episode_" + datetime.fromtimestamp(started).strftime("%Y%m%d_%H%M%S")
    return "@".join((part(info["person"], 100), part(info.get("model_name") or "unknown-model", 100),
                     datetime.fromtimestamp(started).strftime("%Y_%m_%d_%H_%M_%S")))


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class ProgressRecorder:
    def __init__(self, runtime, *, output_dir="runs/recordings", poll_interval_s=.5,
                 finalize_timeout_s=30, scheduler_client=None):
        for value in (poll_interval_s, finalize_timeout_s):
            if number(value) is None or value <= 0:
                raise ValueError("recording intervals must be finite and positive")
        self.runtime = runtime
        self.root = Path(output_dir).expanduser().resolve()
        self.interval, self.finalize_timeout = poll_interval_s, finalize_timeout_s
        self.client = scheduler_client or BridgeClient(runtime.robot_driver.bridge.scheduler_url,
                                                       timeout_s=2, json_protocol=True)
        self._stop, self._wake = threading.Event(), threading.Event()
        self._hints = queue.SimpleQueue()
        self._sessions, self._last = {}, None
        self._legacy_key = None
        self._error = None
        self._lock = threading.RLock()
        self._thread = None
        self._started_at = time.time()
        self._image_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="record-images")

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="grm-recording")
                self._thread.start()

    def notify(self, result):
        # New Scheduler replies contain the session ID even if the user starts
        # and stops between status polls. Legacy Scheduler uses polling below.
        if result.get("recording_info"):
            self._hints.put(deepcopy(result))
        self._wake.set()

    def _observe(self, state):
        for info in state.get("recording_history", []):
            if info.get("id") in self._sessions or (number(info.get("started_at")) or 0) >= self._started_at:
                self._observe_one({"recording_info": info})
        self._observe_one(state)

    def _observe_one(self, state):
        info = deepcopy(state.get("recording_info") or {})
        for field in ("person", "model_name"):
            if field not in info and state.get(field) is not None:
                info[field] = state[field]
        key = info.get("id")
        if not key:
            if state.get("recording"):
                self._legacy_key = self._legacy_key or uuid4().hex
            if not self._legacy_key:
                return
            key = self._legacy_key
            info = {"id": key, "state": "recording" if state.get("recording") else "stopped",
                    "clock": "runtime_unix", "legacy_scheduler": True}
            if not state.get("recording"):
                self._legacy_key = None
        now = time.time()
        session = self._sessions.get(key)
        if session is None:
            if info.get("state") in {"stopped", "failed"} and (number(info.get("started_at")) or 0) < self._started_at:
                return  # Do not export old videos from before this Runtime run.
            recording_id = "rec-" + uuid4().hex
            started = number(info.get("started_at")) or now
            name = recording_name(info, started, recording_id)
            directory = self.root / name
            if directory.exists():
                name += "@" + recording_id[-8:]
                directory = self.root / name
            directory.mkdir(parents=True, exist_ok=False)
            manifest = {"schema_version": 2, "recording_id": recording_id, "name": name, "state": "recording",
                "instruction": info.get("instruction"), "person": info.get("person"), "model_name": info.get("model_name"),
                "image_count": 0, "missing_image_count": 0,
                "started_at": started, "stopped_at": None, "runtime_observed_at": now,
                "video": info, "scheduler_url": self.runtime.robot_driver.bridge.scheduler_url,
                "monitor_url": getattr(self.runtime.monitor_provider, "url", None),
                "time_alignment": {"progress_clock": "monitor_unix", "window_clock": info.get("clock"),
                    "basis": "GRM publication time", "requires_synchronized_clocks": True,
                    "video_start_bounds": [info.get("started_at"), info.get("start_confirmed_at")]},
                "sources": {}, "record_count": 0, "warnings": [], "error": None}
            if info.get("legacy_scheduler"):
                manifest["warnings"].append("Legacy Scheduler: video path unknown; recording bounds use Runtime observation time.")
            elif started < self._started_at:
                manifest["warnings"].append("Runtime attached after video started; tasks from an earlier Runtime process cannot be recovered automatically.")
            session = {"manifest": manifest, "dir": directory, "cursors": {}, "seen": set(),
                       "closed": False, "name_from_video": bool(video_name(info)),
                       "drain_started": None, "images_pending": {}, "images_saved": {}}
            with (directory / "progress.csv").open("w", encoding="utf-8", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()
            (directory / "progress.jsonl").touch()
            self._sessions[key] = session
        manifest = session["manifest"]
        if session["closed"]:
            return
        if not session["name_from_video"] and video_name(info):
            # Scheduler confirms its name asynchronously. Adopt that exact
            # episode name after collecting early scores under a provisional name.
            name = recording_name(info, manifest["started_at"], manifest["recording_id"])
            directory = self.root / name
            if directory != session["dir"]:
                if directory.exists():
                    name += "@" + manifest["recording_id"][-8:]
                    directory = self.root / name
                session["dir"].rename(directory)
                session["dir"] = directory
            manifest["name"] = name
            session["name_from_video"] = True
        manifest["video"].update(info)
        for field in ("instruction", "person", "model_name"):
            if manifest.get(field) is None and info.get(field) is not None:
                manifest[field] = info[field]
        manifest["time_alignment"]["video_start_bounds"] = [manifest["video"].get("started_at"),
                                                            manifest["video"].get("start_confirmed_at")]
        if info.get("state") in {"stopping", "stopped", "failed"}:
            manifest["stopped_at"] = number(info.get("stopped_at")) or now
            manifest["state"] = "stopping" if info["state"] == "stopping" else "finalizing"
            session["drain_started"] = session["drain_started"] or time.monotonic()
        elif manifest["stopped_at"] is not None:
            # A failed stop leaves video running. Replay journals to recover
            # rows filtered while that stop was pending.
            manifest.update(stopped_at=None, state="recording")
            manifest["video"].pop("stopped_at", None)
            session["cursors"].clear()
            session["drain_started"] = None
        if self._last is None or manifest["started_at"] >= self._last["manifest"]["started_at"]:
            self._last = session
        self._save(session)

    def _save(self, session):
        path = session["dir"] / "manifest.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(session["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _append(self, session, monitor, record, capture_mode):
        manifest = session["manifest"]
        step = record.get("inference_step")
        timestamp = number(record.get("inference_updated_at"))
        if type(step) is not int or step <= 0 or timestamp is None:
            return
        if timestamp < manifest["started_at"] or (manifest["stopped_at"] is not None and timestamp > manifest["stopped_at"]):
            return
        identity = (monitor["monitor_id"], step)
        if identity in session["seen"]:
            return
        source = manifest["sources"][monitor["monitor_id"]]
        source.setdefault("image_directory", f"frames/task_{list(manifest['sources']).index(monitor['monitor_id']) + 1:03d}")
        images = {camera: f"{source['image_directory']}/step_{step:06d}/{camera}.png" for camera in CAMERAS}
        for camera, relative in images.items():
            session["images_pending"].setdefault(relative, {
                "monitor_id": monitor["monitor_id"], "execution_id": monitor["execution_id"], "inference_step": step,
                "camera": camera, "frame_set_id": (record.get("preview") or {}).get("frame_set_id"),
                "expected_sha256": (record.get("observation") or {}).get("cameras", {}).get(camera, {}).get("image_sha256"),
                "timestamp": timestamp, "next_attempt": 0, "error": None})
        observation_at = number((record.get("observation") or {}).get("snapshot_requested_at"))
        row = {"recording_id": manifest["recording_id"], "execution_id": monitor["execution_id"],
               "monitor_id": monitor["monitor_id"], "instruction": monitor["subtask"], "inference_step": step,
               "progress_time_unix_s": timestamp, "elapsed_s": timestamp - manifest["started_at"],
               "observation_time_unix_s": observation_at,
               "observation_elapsed_s": None if observation_at is None else observation_at - manifest["started_at"],
               "progress": record.get("progress"), "status": record.get("status"),
               "baseline_progress": record.get("branches", {}).get("baseline", {}).get("progress"),
               "branch_difference": record.get("comparison", {}).get("difference"), "latency_s": record.get("latency_s")}
        for mode in ("forward", "incremental", "backward"):
            for metric in ("score", "progress"):
                row[f"{mode}_{metric}"] = record.get("modes", {}).get(mode, {}).get(metric)
                row[f"baseline_{mode}_{metric}"] = record.get("branches", {}).get("baseline", {}).get("modes", {}).get(mode, {}).get(metric)
        row.update({f"{camera}_image": relative for camera, relative in images.items()})
        entry = {**row, "received_at": time.time(), "capture_mode": capture_mode,
                 "planned_images": images, "images": images, "grm": record}
        with (session["dir"] / "progress.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # CSV is derived; an export can always rebuild it from authoritative JSONL.
        session["seen"].add(identity)
        manifest["record_count"] += 1
        with (session["dir"] / "progress.csv").open("a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)

    @staticmethod
    def _in_window(session, stamp):
        m = session["manifest"]
        return stamp >= m["started_at"] and (m["stopped_at"] is None or stamp <= m["stopped_at"])

    def _fetch_image(self, session, relative, job):
        from PIL import Image
        fetch = getattr(self.runtime.monitor_provider, "progress_image", None)
        if fetch is None:
            raise RuntimeError("Monitor has no scoring-image export API; update Monitor and Runtime")
        data = fetch(job["monitor_id"], job["execution_id"], job["inference_step"], job["camera"], job["frame_set_id"])
        digest = hashlib.sha256(data).hexdigest()
        if job["expected_sha256"] and digest != job["expected_sha256"]:
            raise ValueError("scoring image checksum differs from the recorded input")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("scoring image must be PNG")
            image.verify()
        path = session["dir"] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return {"sha256": digest, "bytes": len(data)}

    def _collect_images(self, session):
        jobs = [(relative, job) for relative, job in session["images_pending"].items()
                if self._in_window(session, job["timestamp"]) and job["next_attempt"] <= time.monotonic()][:3]
        futures = [(relative, job, self._image_pool.submit(self._fetch_image, session, relative, job)) for relative, job in jobs]
        downloaded = False
        for relative, job, future in futures:
            try:
                session["images_saved"][relative] = future.result()
                del session["images_pending"][relative]
                downloaded = True
            except Exception as exc:
                job.update(error=str(exc), next_attempt=time.monotonic() + 2)
        missing = [j for j in session["images_pending"].values() if self._in_window(session, j["timestamp"])]
        session["manifest"]["image_count"] = len(session["images_saved"])
        session["manifest"]["missing_image_count"] = len(missing)
        if downloaded and any(j["next_attempt"] <= time.monotonic() for j in missing):
            # Drain image backlogs without imposing a three-images-per-poll
            # throughput limit; failures still observe their retry delay.
            self._wake.set()
        return missing

    def _collect(self, session):
        manifest = session["manifest"]
        monitors = self.runtime.recording_monitors(manifest["started_at"])
        all_drained = True
        errors = []
        for monitor in monitors:
            if self._stop.is_set():
                return
            mid = monitor["monitor_id"]
            # A recording may cover multiple tasks. Keep original identities,
            # including completed tasks whose final journal was missed by polls.
            if manifest["stopped_at"] is not None and monitor["created_at"] > manifest["stopped_at"]:
                continue
            source = manifest["sources"].setdefault(mid, {"execution_id": monitor["execution_id"],
                "instruction": monitor["subtask"], "target_queries": monitor.get("target_queries"),
                "session_dir": monitor.get("result", {}).get("session_dir")})
            reader = getattr(self.runtime.monitor_provider, "progress_records", None)
            try:
                if reader is None:
                    raise NotImplementedError("Monitor provider has no progress journal API")
                # Drain a bounded page per tick so a long backlog cannot starve
                # stop/status handling; cursor advances only after local writes.
                result = reader(mid, monitor["execution_id"], session["cursors"].get(mid, 0))
                if result.get("monitor_id") != mid or result.get("execution_id") != monitor["execution_id"]:
                    raise ValueError("progress journal identity mismatch")
                if source.get("generation") not in (None, result.get("generation")):
                    raise ValueError("progress journal generation changed")
                source.update(generation=result.get("generation"), session_dir=result.get("session_dir"),
                              capture_mode="journal", read_at=result.get("read_at"))
                for record in result["records"]:
                    self._append(session, monitor, record, "journal")
                session["cursors"][mid] = result["next_cursor"]
                all_drained = all_drained and not result["has_more"]
                if manifest["stopped_at"] is not None:
                    # This watermark is taken under the Monitor's commit lock.
                    # An empty page alone cannot prove the stop window is drained.
                    all_drained = all_drained and (number(result.get("read_at")) or 0) >= manifest["stopped_at"]
            except (NotImplementedError, urllib.error.HTTPError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and (exc.code != 404 or source.get("capture_mode") == "journal"):
                    errors.append(f"{mid}: {exc}")
                    all_drained = False
                    continue
                if monitor.get("result", {}).get("warming_up") is True:
                    # Runtime registers a provisional monitor before remote
                    # start completes. Its journal does not exist yet.
                    errors.append(f"{mid}: waiting for monitor journal registration")
                    all_drained = False
                    continue
                source["capture_mode"] = "status_snapshots"
                warning = "Monitor journal unavailable: cached progress snapshots may skip steps; update Monitor for full capture."
                if warning not in manifest["warnings"]:
                    manifest["warnings"].append(warning)
                self._append(session, monitor, monitor.get("result") or {}, "status_snapshots")
            except Exception as exc:
                all_drained = False
                errors.append(f"{mid}: {exc}")
        missing = self._collect_images(session) if not self._stop.is_set() else list(session["images_pending"].values())
        all_drained = all_drained and not missing
        if missing:
            detail = next((j["error"] for j in missing if j["error"]), "downloading")
            errors.append(f"{len(missing)} scoring images pending: {detail}")
        manifest["error"] = "; ".join(errors) or None
        if manifest["state"] in {"stopping", "finalizing"}:
            timed_out = time.monotonic() - session["drain_started"] >= self.finalize_timeout
            # Wait for the archive response. When Scheduler is unreachable,
            # keep the data but make missing confirmation explicit on timeout.
            video_done = manifest["state"] == "finalizing"
            if (all_drained and video_done) or timed_out:
                if not video_done:
                    errors.append("Video stop/archive confirmation unavailable before finalize timeout")
                if not all_drained and not errors:
                    errors.append("Monitor journal did not drain through recording stop time before finalize timeout")
                manifest["error"] = "; ".join(errors) or None
                previous = manifest["state"]
                manifest["state"] = "complete" if all_drained and video_done and not manifest["video"].get("error") else "incomplete"
                try:
                    self._finish(session)
                except Exception:
                    manifest["state"] = previous
                    raise
                return
        self._save(session)

    def _finish(self, session):
        manifest = session["manifest"]
        # Reapply the final window: a score can arrive between the stop click
        # and our next Scheduler poll. JSONL remains the authoritative export.
        kept = 0
        included_images, missing_images = set(), []
        with (session["dir"] / "progress.jsonl").open(encoding="utf-8") as source, \
                (session["dir"] / "progress.tmp").open("w", encoding="utf-8") as filtered, \
                (session["dir"] / "progress.csv").open("w", encoding="utf-8", newline="") as table:
            writer = csv.DictWriter(table, fieldnames=FIELDS)
            writer.writeheader()
            for line in source:
                record = json.loads(line)
                timestamp = record["progress_time_unix_s"]
                if timestamp < manifest["started_at"] or (manifest["stopped_at"] is not None and timestamp > manifest["stopped_at"]):
                    continue
                available, hashes, missing = {}, {}, []
                for camera, relative in record.get("planned_images", record.get("images", {})).items():
                    if relative in session["images_saved"]:
                        available[camera] = relative
                        hashes[camera] = session["images_saved"][relative]["sha256"]
                        included_images.add(relative)
                        record[f"{camera}_image"] = relative
                    else:
                        missing.append(camera)
                        missing_images.append({"monitor_id": record["monitor_id"], "inference_step": record["inference_step"],
                            "camera": camera, "error": session["images_pending"].get(relative, {}).get("error") or "image was not downloaded"})
                        record[f"{camera}_image"] = None
                record.update(images=available, image_sha256=hashes, missing_images=missing)
                filtered.write(json.dumps(record, ensure_ascii=False) + "\n")
                writer.writerow({key: record.get(key) for key in FIELDS})
                kept += 1
        (session["dir"] / "progress.tmp").replace(session["dir"] / "progress.jsonl")
        manifest["record_count"] = kept
        manifest.update(image_count=len(included_images), missing_image_count=len(missing_images), missing_images=missing_images)
        if missing_images and manifest["state"] == "complete":
            manifest["state"] = "incomplete"
        manifest["capture_quality"] = "snapshot_only" if any(
            s.get("capture_mode") == "status_snapshots" for s in manifest["sources"].values()) else "journal"
        manifest["finished_at"] = time.time()
        self._save(session)
        archive = session["dir"] / (manifest["name"] + ".zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as f:
            for name in ("manifest.json", "progress.jsonl", "progress.csv", *sorted(included_images)):
                f.write(session["dir"] / name, arcname=name,
                        compress_type=zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED)
        session["closed"] = True
        session["seen"].clear()
        session["cursors"].clear()
        session["images_pending"].clear()
        session["images_saved"].clear()

    def _run(self):
        try:
            while not self._stop.is_set():
                scheduler_error = None
                try:
                    with self._lock:
                        while not self._hints.empty():
                            self._observe(self._hints.get())
                    try:
                        result = self.client.call({"cmd": "status"})
                        if result.get("status") != "ok":
                            raise RuntimeError(f"Scheduler recording status failed: {result}")
                        with self._lock:
                            self._observe(result["state"])
                    except Exception as exc:
                        scheduler_error = str(exc)
                    with self._lock:
                        sessions = [s for s in self._sessions.values() if not s["closed"]]
                    # No status lock during network I/O, including GRM history.
                    for session in sessions:
                        self._collect(session)
                    self._error = scheduler_error
                except Exception as exc:
                    self._error = str(exc)
                    logger.warning("Progress recording: %s", exc)
                self._wake.wait(self.interval)
                self._wake.clear()
        finally:
            for session in self._sessions.values():
                if not session["closed"]:
                    try:
                        session["manifest"].update(state="interrupted", error=self._error or "Runtime stopped before recording finalized")
                        self._finish(session)
                    except Exception:
                        logger.exception("Could not finalize progress recording")
            self._image_pool.shutdown(wait=True)
            self.client.close()

    def status(self):
        with self._lock:
            if self._last is None:
                return {"enabled": True, "state": "idle", "output_dir": str(self.root), "error": self._error}
            m = self._last["manifest"]
            return {"enabled": True, "state": m["state"], "recording_id": m["recording_id"],
                    "directory": str(self._last["dir"]), "name": m["name"], "record_count": m["record_count"],
                    "image_count": m["image_count"], "missing_image_count": m["missing_image_count"],
                    "video": deepcopy(m["video"]), "warnings": list(m["warnings"]),
                    "error": self._error or m["error"], "download_ready": self._last["closed"]}

    def archive(self, recording_id):
        with self._lock:
            for session in self._sessions.values():
                if session["manifest"]["recording_id"] == recording_id and session["closed"]:
                    return session["dir"] / (session["manifest"]["name"] + ".zip")
        raise KeyError("unknown or unfinished progress recording")

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        else:
            self._image_pool.shutdown(wait=True)
            self.client.close()
