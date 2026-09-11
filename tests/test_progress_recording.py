"""Recording windows and durable exports with simulated Scheduler/GRM I/O."""

from copy import deepcopy
import csv
import hashlib
import io
import json
import time
from types import SimpleNamespace
import urllib.error
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from robot_runtime.api.app import create_app
from robot_runtime.recording import CAMERAS, ProgressRecorder, recording_name
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack


class Journal:
    url = "http://monitor.invalid"

    def __init__(self):
        self.rows, self.errors, self.calls = {}, {}, []
        self.image_errors = {}
        self.read_at = time.time() + 100

    def progress_records(self, mid, execution, cursor=0):
        self.calls.append((mid, cursor))
        if mid in self.errors:
            raise self.errors[mid]
        rows = self.rows.get(mid, [])
        end = min(cursor + 2, len(rows))  # Exercise pagination with short pages.
        return dict(monitor_id=mid, execution_id=execution, generation=mid,
                    records=deepcopy(rows[cursor:end]), next_cursor=end,
                    has_more=end < len(rows), read_at=self.read_at,
                    session_dir=f"/monitor/{mid}", complete=False)

    def progress_image(self, mid, execution, step, camera, frame_set_id=None):
        assert execution == "ex-" + mid
        if (step, camera) in self.image_errors:
            raise self.image_errors[step, camera]
        return image_bytes(step, camera)


def image_bytes(step, camera):
    data = io.BytesIO()
    Image.new("RGB", (5, 4), (step % 256, CAMERAS.index(camera) * 100, 42)).save(data, format="PNG")
    return data.getvalue()


class Scheduler:
    def __init__(self):
        self.state = {"recording": False}
        self.calls, self.error, self.closed = [], None, False

    def call(self, request):
        self.calls.append(request)
        assert request == {"cmd": "status"}  # Recording never issues controls.
        if self.error:
            raise self.error
        return {"status": "ok", "state": deepcopy(self.state)}

    def close(self):
        self.closed = True


def monitor(mid, created_at):
    return dict(monitor_id=mid, execution_id="ex-" + mid, subtask="抓取 carrot",
                created_at=created_at, target_queries=["carrot"], result={})


def score(step, stamp):
    return dict(inference_step=step, inference_updated_at=stamp, progress=step / 10,
                status="running", observation={"snapshot_requested_at": stamp - .25,
                    "cameras": {c: {"image_sha256": hashlib.sha256(image_bytes(step, c)).hexdigest()} for c in CAMERAS}},
                modes={"forward": {"score": .2, "progress": .3}},
                branches={"baseline": {"progress": .1, "modes": {
                    mode: {"score": .4, "progress": .5} for mode in ("forward", "incremental", "backward")}}},
                comparison={"difference": .2})


def state(key, started, ended=None, phase=None):
    info = dict(id=key, started_at=started, clock="scheduler_unix",
                state=phase or ("recording" if ended is None else "stopped"),
                episode_dir="/robot/" + key)
    if ended is not None:
        info.update(stopped_at=ended, archive="/robot/" + key + ".tar")
    return dict(recording=ended is None, recording_info=info)


@pytest.fixture
def stack(tmp_path):
    journal, scheduler, monitors = Journal(), Scheduler(), []
    runtime = SimpleNamespace(monitor_provider=journal, recording_monitors=lambda since: deepcopy(monitors),
                              robot_driver=SimpleNamespace(bridge=SimpleNamespace(scheduler_url="ws://scheduler.invalid")))
    recorder = ProgressRecorder(runtime, output_dir=tmp_path, poll_interval_s=.01,
                                finalize_timeout_s=.5, scheduler_client=scheduler)
    yield recorder, journal, scheduler, monitors
    recorder.close()


def export(recorder, key):
    session = recorder._sessions[key]
    for _ in range(30):
        if session["closed"]:
            break
        recorder._collect(session)
    assert session["closed"]
    with zipfile.ZipFile(recorder.archive(session["manifest"]["recording_id"])) as z:
        manifest = json.loads(z.read("manifest.json"))
        rows = [json.loads(line) for line in z.read("progress.jsonl").splitlines()]
        table = list(csv.DictReader(io.StringIO(z.read("progress.csv").decode())))
        image_paths = {name for name in z.namelist() if name.endswith(".png")}
        assert len(image_paths) == manifest["image_count"]
        assert image_paths == {path for row in rows for path in row["images"].values()}
        for row, cells in zip(rows, table):
            for camera, path in row["images"].items():
                data = z.read(path)
                assert data == image_bytes(row["inference_step"], camera)
                assert hashlib.sha256(data).hexdigest() == row["image_sha256"][camera]
                assert cells[f"{camera}_image"] == path
    assert manifest["record_count"] == len(rows) == len(table)
    assert all(row["instruction"] == "抓取 carrot" for row in rows)
    return manifest, rows, table


def test_paged_journals_capture_all_steps_across_tasks_and_final_window(stack):
    r, j, _, monitors = stack
    t = time.time()
    monitors.extend([monitor("one", t - 2), monitor("two", t + 1)])
    j.rows = {"one": [score(i, t - 1 + i * .2) for i in range(1, 13)],
              "two": [score(i, t + 1 + i * .2) for i in range(1, 6)]}
    r._observe(state("video", t))
    for _ in range(8):
        r._collect(r._sessions["video"])
    assert r.status()["record_count"] == 13  # Includes rows later than stop until poll arrives.
    r._observe(state("video", t, t + 1.5))
    m, rows, table = export(r, "video")
    assert m["state"] == "complete" and m["capture_quality"] == "journal"
    assert len(rows) == 10
    assert {(row["monitor_id"], row["inference_step"]) for row in rows} == {
        *[("one", i) for i in range(5, 13)], ("two", 1), ("two", 2)}
    assert all(0 <= row["elapsed_s"] <= 1.5 for row in rows)
    assert table[0]["forward_score"] == "0.2" and table[0]["baseline_progress"] == "0.1"
    assert all(table[0][f"baseline_{mode}_{metric}"] == str(value)
               for mode in ("forward", "incremental", "backward") for metric, value in (("score", .4), ("progress", .5)))
    assert m["image_count"] == 30 and m["missing_image_count"] == 0
    assert all(row["grm"] == next(x for x in j.rows[row["monitor_id"]] if x["inference_step"] == row["inference_step"]) for row in rows)
    assert m["sources"]["one"]["target_queries"] == ["carrot"]
    assert m["video"]["archive"] == "/robot/video.tar"


def test_pending_stop_filters_scores_and_failed_stop_replays_them(stack):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    j.rows["one"] = [score(1, t + .1), score(2, t + .3)]
    r._observe(state("video", t))
    r._observe(state("video", t, t + .2, "stopping"))
    r._collect(r._sessions["video"])
    assert r.status()["record_count"] == 1 and not r.status()["download_ready"]
    r._observe(state("video", t))  # Robot rejected stop; recording continues.
    r._collect(r._sessions["video"])
    assert r.status()["record_count"] == 2
    r._observe(state("video", t, t + .4))
    assert len(export(r, "video")[1]) == 2


def test_rapid_sessions_use_scheduler_history_and_ignore_pre_runtime_history(stack):
    r, _, _, _ = stack
    t = time.time()
    r._observe(state("one", t, t + .1, "stopping"))
    second = state("two", t + .2, t + .3)
    second["recording_history"] = [state("old", t - 100, t - 90)["recording_info"],
                                    state("one", t, t + .1)["recording_info"]]
    r._observe(second)
    assert "old" not in r._sessions
    assert export(r, "one")[0]["video"]["archive"] == "/robot/one.tar"
    assert export(r, "two")[0]["state"] == "complete"
    assert r._sessions["one"]["dir"] != r._sessions["two"]["dir"]


def test_transient_source_failure_backfills_before_finalizing(stack):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    j.rows["one"] = [score(i, t + .01 * i) for i in range(1, 8)]
    j.errors["one"] = OSError("temporary monitor disconnect")
    r._observe(state("video", t, t + .1))
    r._collect(r._sessions["video"])
    assert "disconnect" in r.status()["error"] and not r.status()["download_ready"]
    j.errors.clear()
    m, rows, _ = export(r, "video")
    assert len(rows) == 7 and m["error"] is None and m["state"] == "complete"


@pytest.mark.parametrize("missing", ["journal", "watermark", "archive"])
def test_finalize_timeout_is_explicitly_incomplete(stack, missing):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    if missing == "journal":
        j.errors["one"] = OSError("offline")
    if missing == "watermark":
        j.read_at = t
    r._observe(state("video", t, t + .1, "stopping" if missing == "archive" else "stopped"))
    r._sessions["video"]["drain_started"] -= 10
    m, _, _ = export(r, "video")
    assert m["state"] == "incomplete" and m["error"]


def test_initial_404_does_not_claim_legacy_but_lost_registered_journal_is_error(stack):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    monitors[0]["result"] = {"warming_up": True}
    j.errors["one"] = urllib.error.HTTPError("url", 404, "not found", {}, None)
    r._observe(state("video", t))
    r._collect(r._sessions["video"])
    assert not r.status()["warnings"]
    j.errors.clear()
    r._collect(r._sessions["video"])
    j.errors["one"] = urllib.error.HTTPError("url", 404, "not found", {}, None)
    r._collect(r._sessions["video"])
    assert "404" in r.status()["error"] and not r.status()["warnings"]


def test_legacy_snapshots_are_marked_and_never_advance_monitor_status(stack):
    r, _, _, monitors = stack
    t = time.time()
    r.runtime.monitor_provider = SimpleNamespace()  # No status() method either.
    monitors.append(monitor("one", t))
    monitors[0]["result"] = score(2, t + .1)
    r._observe(state("video", t, t + .2))
    r._sessions["video"]["drain_started"] -= 10
    m, rows, _ = export(r, "video")
    assert m["capture_quality"] == "snapshot_only" and m["warnings"]
    assert len(rows) == 1 and rows[0]["capture_mode"] == "status_snapshots"
    assert m["state"] == "incomplete" and m["missing_image_count"] == 3


def test_legacy_scheduler_uses_local_window_and_warns_video_path_is_unknown(stack):
    r, _, _, _ = stack
    r._observe({"recording": True})
    key = r._legacy_key
    r._observe({"recording": False})
    m, rows, _ = export(r, key)
    assert m["state"] == "complete" and not rows
    assert m["stopped_at"] >= m["started_at"]
    assert m["video"]["clock"] == "runtime_unix" and m["warnings"]
    assert m["video"].get("archive") is None


def test_worker_collects_during_scheduler_outage_and_shutdown_keeps_data(stack):
    r, j, s, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    s.error = OSError("scheduler offline")
    r.notify(state("video", t))
    r.start()
    eventually(lambda: r.status()["state"] == "recording")
    j.rows["one"] = [score(1, t + .01)]
    eventually(lambda: r.status().get("record_count") == 1)
    assert "scheduler offline" in r.status()["error"]
    r.close()
    assert not r._thread.is_alive() and s.closed
    assert export(r, "video")[0]["state"] == "interrupted"


def test_download_is_limited_to_finished_registered_artifacts(stack):
    r, _, _, _ = stack
    runtime, driver, _, _ = make_stack()
    runtime.recorder = r
    t = time.time()
    r._observe(state("video", t))
    rid = r.status()["recording_id"]
    client = TestClient(create_app(runtime))
    assert client.get(f"/manual/recordings/{rid}/download").status_code == 404
    r._observe(state("video", t, t + .1))
    export(r, "video")
    result = client.get(f"/manual/recordings/{rid}/download")
    assert result.status_code == 200 and result.headers["content-type"] == "application/zip"
    assert r.status()["name"] + ".zip" in unquote(result.headers["content-disposition"])
    assert zipfile.is_zipfile(io.BytesIO(result.content))
    assert client.get("/manual/recordings/progress.zip/download").status_code == 404
    assert client.get("/manual/recordings/%2e%2e%2fsecret/download").status_code == 404
    driver.close()


def test_export_retries_after_zip_failure(stack, monkeypatch):
    r, _, _, _ = stack
    t = time.time()
    r._observe(state("video", t, t + .1))
    real_zip = zipfile.ZipFile
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(zipfile, "ZipFile", fail)
    with pytest.raises(OSError):
        r._collect(r._sessions["video"])
    assert r.status()["state"] == "finalizing" and not r.status()["download_ready"]
    monkeypatch.setattr(zipfile, "ZipFile", real_zip)
    assert export(r, "video")[0]["state"] == "complete"


def test_missing_image_does_not_block_later_scores_and_retries_fill_it(stack):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    j.rows["one"] = [score(i, t + i * .01) for i in range(1, 8)]
    j.image_errors[1, "cam_high"] = OSError("temporary image disconnect")
    r._observe(state("video", t))
    session = r._sessions["video"]
    for _ in range(8):
        r._collect(session)
    assert r.status()["record_count"] == 7
    assert r.status()["image_count"] == 20 and r.status()["missing_image_count"] == 1
    j.image_errors.clear()
    for job in session["images_pending"].values():
        job["next_attempt"] = 0
    r._observe(state("video", t, t + .1))
    m, rows, _ = export(r, "video")
    assert m["state"] == "complete" and m["image_count"] == 21 and len(rows) == 7


@pytest.mark.parametrize("problem", ["missing", "checksum", "invalid_png"])
def test_image_integrity_failure_is_explicit_and_survives_zip_retry(stack, monkeypatch, problem):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    j.rows["one"] = [score(1, t + .01)]
    if problem == "missing":
        j.image_errors[1, "cam_high"] = FileNotFoundError("missing original")
    elif problem == "checksum":
        j.rows["one"][0]["observation"]["cameras"]["cam_high"]["image_sha256"] = "bad"
    else:
        j.rows["one"][0]["observation"]["cameras"]["cam_high"].clear()
        original = j.progress_image
        j.progress_image = lambda mid, ex, step, cam, fid: b"invalid PNG" if cam == "cam_high" else original(mid, ex, step, cam, fid)
    r._observe(state("video", t, t + .1))
    session = r._sessions["video"]
    session["drain_started"] -= 10
    real_zip = zipfile.ZipFile
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("disk unavailable")
        patch.setattr(zipfile, "ZipFile", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            r._collect(session)
    assert zipfile.ZipFile is real_zip
    m, rows, table = export(r, "video")
    assert m["state"] == "incomplete" and m["image_count"] == 2 and m["missing_image_count"] == 1
    assert m["missing_images"][0]["camera"] == "cam_high"
    assert m["missing_images"][0]["inference_step"] == 1 and m["missing_images"][0]["error"]
    assert rows[0]["missing_images"] == ["cam_high"] and table[0]["cam_high_image"] == ""


def test_readable_frozen_names_and_utf8_filename_limits(stack):
    r, _, _, _ = stack
    t = time.time()
    info = state("one", t, t + .1)
    info["recording_info"].update(person="张三", model_name="run-1000", instruction="把胡萝卜放进盒子")
    info["recording_info"].pop("episode_dir")
    r._observe(info)
    first = r.status()["name"]
    assert first.startswith("张三@run-1000@") and len(first.split("@")) == 3
    assert "胡萝卜" not in first
    info["recording_info"]["instruction"] = "下一轮指令"
    r._observe(info)
    assert r.status()["name"] == first
    m, _, _ = export(r, "one")
    assert m["instruction"] == "把胡萝卜放进盒子"
    assert r.archive(m["recording_id"]).name == first + ".zip"
    name = recording_name(dict(person="人" * 64, model_name="模型" * 100,
        instruction="/../../\\任务\n" * 200), t, "rec-12345678")
    assert len((name + ".zip").encode()) < 256 and "/" not in name and "\\" not in name and "\n" not in name
    # A repeated Scheduler episode name must not overwrite an existing export.
    for key in ("two", "three"):
        repeat = state(key, t + 1, t + 2)
        repeat["recording_info"]["episode_name"] = first
        r._observe(repeat)
        export(r, key)
    assert len({s["dir"] for s in r._sessions.values()}) == 3


@pytest.mark.parametrize("person", ["张三", ""])
def test_async_scheduler_name_replaces_provisional_name_without_losing_scores(stack, person):
    r, j, _, monitors = stack
    t = time.time()
    monitors.append(monitor("one", t))
    j.rows["one"] = [score(1, t + .01)]
    starting = {"recording": True, "person": person, "model_name": "run-1000",
        "recording_info": {"id": "video", "state": "starting", "started_at": t, "clock": "scheduler_unix"}}
    r._observe(starting)
    r._collect(r._sessions["video"])
    old_directory = r._sessions["video"]["dir"]
    # Name timestamp can differ from request time; use Scheduler's actual result.
    name = "张三@run-1000@2026_09_11_15_30_01" if person else "episode_20260911_153001"
    confirmed = state("video", t, t + .1)
    confirmed["recording_info"].update(episode_name=name if person else None, episode_dir="/robot/" + name)
    r._observe(confirmed)
    m, rows, _ = export(r, "video")
    assert m["name"] == name and r.archive(m["recording_id"]).name == name + ".zip"
    assert not old_directory.exists() and len(rows) == 1 and m["image_count"] == 3
