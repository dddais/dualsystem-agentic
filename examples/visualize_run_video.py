#!/usr/bin/env python3
"""Render a dualsystem-agentic run log into an annotated MP4 video.

The script reads a run directory produced by JsonlRunLogger, such as:

    runs/run_20260611_173450

It renders one video frame per logged step:

    header: run/session/task/subtask/controller state
    middle: cam_left_wrist | cam_high | cam_right_wrist
    bottom: events, subtask/monitor/reasoning status strips

Only Pillow and ffmpeg are required. Pillow is already a core project
dependency; ffmpeg is used through its command line binary.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


DEFAULT_RUN_DIR = "runs/run_20260611_173450"
DEFAULT_CAMERA_ORDER = ("cam_left_wrist", "cam_high", "cam_right_wrist")

BG = (12, 18, 32)
PANEL_BG = (17, 24, 39)
PANEL_EDGE = (51, 65, 85)
TEXT = (229, 231, 235)
MUTED = (156, 163, 175)
SUBTLE = (100, 116, 139)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

SUBTASK_COLORS = [
    (59, 130, 246),
    (249, 115, 22),
    (34, 197, 94),
    (236, 72, 153),
    (168, 85, 247),
    (20, 184, 166),
    (234, 179, 8),
    (239, 68, 68),
]

STATUS_COLORS = {
    "running": (245, 158, 11),
    "success": (34, 197, 94),
    "failed": (239, 68, 68),
    "timeout": (239, 68, 68),
    "error": (239, 68, 68),
    "none": (100, 116, 139),
}

PHASE_COLORS = {
    "reason": (96, 165, 250),
    "act": (168, 85, 247),
    "response": (20, 184, 166),
    "done": (34, 197, 94),
    "error": (239, 68, 68),
    "ready": (100, 116, 139),
    "init": (100, 116, 139),
}


@dataclass
class SessionInfo:
    session_id: str
    index: int
    task: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    stop_reason: str | None = None
    task_complete: bool = False
    error: str | None = None


@dataclass
class VideoFrame:
    raw: dict[str, Any]
    global_index: int
    session_index: int
    session_count: int
    session_id: str
    session_step_index: int
    task: str
    image_paths: dict[str, Path] = field(default_factory=dict)


@dataclass
class RunData:
    run_dir: Path
    run_id: str
    sessions: dict[str, SessionInfo]
    frames: list[VideoFrame]


def main() -> int:
    parser = argparse.ArgumentParser(description="Visualize a dualsystem-agentic run as an MP4 video.")
    parser.add_argument("--run-dir", default=DEFAULT_RUN_DIR, help="Run directory containing events.jsonl.")
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output MP4 path for one video, or output directory when rendering multiple "
            "sessions. Defaults to <run-dir>/visualization_<session_id>.mp4."
        ),
    )
    parser.add_argument(
        "--session",
        action="append",
        default=None,
        help="Render only this session_id. Can be provided multiple times.",
    )
    parser.add_argument("--fps", type=float, default=1.0, help="Output video FPS.")
    parser.add_argument("--panel-width", type=int, default=360, help="Camera panel width in pixels.")
    parser.add_argument("--panel-height", type=int, default=270, help="Camera panel height in pixels.")
    parser.add_argument("--header-height", type=int, default=164, help="Header height in pixels.")
    parser.add_argument("--timeline-height", type=int, default=176, help="Bottom timeline height in pixels.")
    parser.add_argument(
        "--camera-order",
        default=",".join(DEFAULT_CAMERA_ORDER),
        help="Comma-separated camera keys to show in order.",
    )
    parser.add_argument("--max-frames", type=int, default=None, help="Optional cap for quick previews.")
    parser.add_argument("--hold-final-s", type=float, default=2.0, help="Seconds to hold the final frame.")
    parser.add_argument(
        "--combine-run",
        action="store_true",
        help="Render selected sessions into one combined run-level video.",
    )
    parser.add_argument("--summary-only", action="store_true", help="Print run summary and do not render video.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    camera_order = tuple(key.strip() for key in args.camera_order.split(",") if key.strip())

    data = load_run(run_dir, selected_sessions=set(args.session or []))
    if args.max_frames is not None:
        data.frames = data.frames[: max(0, args.max_frames)]
    print_summary(data)

    if args.summary_only:
        return 0
    if not data.frames:
        raise SystemExit("No step frames found in the selected run/session.")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg was not found on PATH; install ffmpeg to render MP4.")

    size = frame_size(args.panel_width, args.panel_height, args.header_height, args.timeline_height)
    if args.combine_run:
        output = Path(args.output).expanduser().resolve() if args.output else run_dir / "visualization.mp4"
        render_one(
            data,
            output=output,
            camera_order=camera_order,
            panel_size=(args.panel_width, args.panel_height),
            header_h=args.header_height,
            timeline_h=args.timeline_height,
            size=size,
            fps=args.fps,
            hold_final_s=args.hold_final_s,
        )
    else:
        outputs = session_outputs(data, args.output)
        for session_data, output in outputs:
            render_one(
                session_data,
                output=output,
                camera_order=camera_order,
                panel_size=(args.panel_width, args.panel_height),
                header_h=args.header_height,
                timeline_h=args.timeline_height,
                size=size,
                fps=args.fps,
                hold_final_s=args.hold_final_s,
            )
    return 0


def load_run(run_dir: Path, *, selected_sessions: set[str] | None = None) -> RunData:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(f"Missing events.jsonl: {events_path}")

    events = load_jsonl(events_path)
    run_id = run_dir.name
    sessions: dict[str, SessionInfo] = {}
    session_order: list[str] = []
    frames: list[VideoFrame] = []
    last_images_by_session: dict[str, dict[str, Path]] = {}

    for event in events:
        run_id = str(event.get("run_id") or run_id)
        event_type = event.get("event")
        session_id = event.get("session_id")
        if event_type == "session_started" and session_id:
            if session_id not in sessions:
                session_order.append(session_id)
                sessions[session_id] = SessionInfo(session_id=session_id, index=len(session_order))
            sessions[session_id].task = str(event.get("task") or "")
            sessions[session_id].started_at = _float_or_none(event.get("time"))
            continue
        if event_type == "session_error" and session_id:
            info = _session_info(sessions, session_order, session_id)
            info.error = f"{event.get('error_type') or 'Error'}: {event.get('error') or ''}".strip()
            continue
        if event_type == "session_finished" and session_id:
            info = _session_info(sessions, session_order, session_id)
            info.finished_at = _float_or_none(event.get("time"))
            info.stop_reason = _optional_str(event.get("stop_reason"))
            info.task_complete = bool(event.get("task_complete"))
            continue
        if event_type != "step" or not session_id:
            continue
        if selected_sessions and session_id not in selected_sessions:
            continue
        info = _session_info(sessions, session_order, session_id)
        image_paths = _step_image_paths(run_dir, event)
        if image_paths:
            last_images_by_session[session_id] = image_paths
        else:
            image_paths = dict(last_images_by_session.get(session_id, {}))
        frames.append(
            VideoFrame(
                raw=event,
                global_index=len(frames),
                session_index=info.index,
                session_count=0,
                session_id=session_id,
                session_step_index=int(event.get("step_index") or 0),
                task=str(event.get("task") or info.task or _nested(event, "planner_input", "task") or ""),
                image_paths=image_paths,
            )
        )

    session_count = len(sessions)
    for frame in frames:
        frame.session_count = session_count
    return RunData(run_dir=run_dir, run_id=run_id, sessions=sessions, frames=frames)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def print_summary(data: RunData) -> None:
    print(f"Run: {data.run_id}")
    print(f"Run dir: {data.run_dir}")
    print(f"Sessions: {len(data.sessions)}")
    for info in data.sessions.values():
        steps = sum(1 for frame in data.frames if frame.session_id == info.session_id)
        suffix = f" stop={info.stop_reason or '-'} complete={info.task_complete}"
        if info.error:
            suffix += f" error={ellipsize(info.error, 80)}"
        print(f"  {info.index}. {info.session_id}: steps={steps} task={ellipsize(info.task, 70)}{suffix}")
    print(f"Frames: {len(data.frames)}")


def render_one(
    data: RunData,
    *,
    output: Path,
    camera_order: tuple[str, ...],
    panel_size: tuple[int, int],
    header_h: int,
    timeline_h: int,
    size: tuple[int, int],
    fps: float,
    hold_final_s: float,
) -> None:
    print(f"Rendering {len(data.frames)} frames -> {output}")
    render_video(
        data,
        output=output,
        camera_order=camera_order,
        panel_size=panel_size,
        header_h=header_h,
        timeline_h=timeline_h,
        size=size,
        fps=fps,
        hold_final_s=hold_final_s,
    )
    print(f"Done: {output}")


def session_outputs(data: RunData, output_arg: str | None) -> list[tuple[RunData, Path]]:
    output_base = Path(output_arg).expanduser().resolve() if output_arg else data.run_dir
    selected_session_ids = list(dict.fromkeys(frame.session_id for frame in data.frames))
    outputs: list[tuple[RunData, Path]] = []
    single_output_file = output_arg is not None and output_base.suffix.lower() == ".mp4"
    if single_output_file and len(selected_session_ids) > 1:
        raise SystemExit("--output may be an .mp4 file only when rendering exactly one session; use a directory otherwise.")
    for session_id in selected_session_ids:
        session_data = data_for_session(data, session_id)
        if not session_data.frames:
            continue
        output = output_base if single_output_file else output_base / f"visualization_{safe_filename(session_id)}.mp4"
        outputs.append((session_data, output))
    return outputs


def data_for_session(data: RunData, session_id: str) -> RunData:
    session_frames = [frame for frame in data.frames if frame.session_id == session_id]
    sessions = {session_id: data.sessions[session_id]} if session_id in data.sessions else {}
    count = len(session_frames)
    frames = [
        replace(
            frame,
            global_index=index,
            session_index=1,
            session_count=1,
        )
        for index, frame in enumerate(session_frames)
    ]
    for frame in frames:
        frame.session_count = 1
    if count == 0:
        return RunData(run_dir=data.run_dir, run_id=data.run_id, sessions=sessions, frames=[])
    return RunData(run_dir=data.run_dir, run_id=data.run_id, sessions=sessions, frames=frames)


def render_video(
    data: RunData,
    *,
    output: Path,
    camera_order: tuple[str, ...],
    panel_size: tuple[int, int],
    header_h: int,
    timeline_h: int,
    size: tuple[int, int],
    fps: float,
    hold_final_s: float,
) -> None:
    width, height = size
    fonts = load_fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-vcodec",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]

    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert process.stdin is not None
    last_frame: Image.Image | None = None
    try:
        for i, frame in enumerate(data.frames):
            image = compose_frame(
                data,
                frame,
                camera_order=camera_order,
                panel_size=panel_size,
                header_h=header_h,
                timeline_h=timeline_h,
                size=size,
                fonts=fonts,
            )
            last_frame = image
            process.stdin.write(image.convert("RGB").tobytes())
            if (i + 1) % 25 == 0 or i == len(data.frames) - 1:
                print(f"  [{i + 1}/{len(data.frames)}]")
        if last_frame is not None and hold_final_s > 0:
            for _ in range(max(0, int(round(fps * hold_final_s)))):
                process.stdin.write(last_frame.convert("RGB").tobytes())
    except BrokenPipeError as exc:
        raise RuntimeError("ffmpeg stopped before all frames were written") from exc
    finally:
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {return_code}")


def compose_frame(
    data: RunData,
    frame: VideoFrame,
    *,
    camera_order: tuple[str, ...],
    panel_size: tuple[int, int],
    header_h: int,
    timeline_h: int,
    size: tuple[int, int],
    fonts: dict[str, ImageFont.ImageFont],
) -> Image.Image:
    width, height = size
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, header_h), fill=PANEL_BG)
    draw.rectangle((0, header_h + panel_size[1], width, height), fill=(15, 23, 42))

    draw_header(draw, data, frame, width, header_h, fonts)
    draw_cameras(canvas, frame, camera_order, panel_size, y=header_h, fonts=fonts)
    draw_timeline(draw, data, frame, y=header_h + panel_size[1], width=width, height=timeline_h, fonts=fonts)
    return canvas


def draw_header(
    draw: ImageDraw.ImageDraw,
    data: RunData,
    frame: VideoFrame,
    width: int,
    header_h: int,
    fonts: dict[str, ImageFont.ImageFont],
) -> None:
    event = frame.raw
    info = data.sessions.get(frame.session_id)
    monitor = monitor_status(event)
    phase = str(event.get("phase") or _nested(event, "planner_input", "phase") or "-")
    decision = str(_nested(event, "planner_output", "decision") or ("monitor_poll" if not event.get("vlm_called") else "-"))
    vlm = "VLM" if event.get("vlm_called") else "monitor-only"

    x = 16
    y = 12
    title = (
        f"{data.run_id}  |  frame {frame.global_index + 1}/{len(data.frames)}  |  "
        f"session {short_session_id(frame.session_id)}  |  step {frame.session_step_index}"
    )
    draw.text((x, y), title, fill=TEXT, font=fonts["bold"])
    y += 28

    task = frame.task or (info.task if info else "")
    subtask = current_subtask(event)
    plan_line = subtask_index_text(event)
    draw_wrapped(draw, f"Task: {task}", (x, y), max_width=width - 420, font=fonts["regular"], fill=TEXT, max_lines=2)
    y += 44
    draw_wrapped(
        draw,
        f"Current: {plan_line} {subtask}".strip(),
        (x, y),
        max_width=width - 420,
        font=fonts["regular"],
        fill=(209, 213, 219),
        max_lines=2,
    )

    draw_status_grid(
        draw,
        width - 390,
        14,
        [
            ("phase", phase, PHASE_COLORS.get(phase, SUBTLE)),
            ("monitor", monitor, STATUS_COLORS.get(monitor, SUBTLE)),
            ("reason", vlm, (99, 102, 241) if event.get("vlm_called") else SUBTLE),
            ("decision", decision, (14, 165, 233)),
        ],
        fonts,
    )

    if info and (info.stop_reason or info.error):
        footer = f"session stop: {info.stop_reason or '-'}"
        if info.error:
            footer += f" | {info.error}"
        draw_wrapped(draw, footer, (16, header_h - 24), max_width=width - 32, font=fonts["tiny"], fill=MUTED, max_lines=1)


def draw_cameras(
    canvas: Image.Image,
    frame: VideoFrame,
    camera_order: tuple[str, ...],
    panel_size: tuple[int, int],
    *,
    y: int,
    fonts: dict[str, ImageFont.ImageFont],
) -> None:
    margin = 16
    gap = 8
    panel_w, panel_h = panel_size
    draw = ImageDraw.Draw(canvas)
    for i, camera in enumerate(camera_order):
        x = margin + i * (panel_w + gap)
        box = (x, y, x + panel_w, y + panel_h)
        draw.rectangle(box, fill=BLACK, outline=PANEL_EDGE, width=1)
        image = load_camera_image(frame.image_paths, camera, panel_size)
        canvas.paste(image, (x, y))
        label = camera.replace("cam_", "")
        label_w = int(draw.textlength(label, font=fonts["small"])) + 16
        draw.rectangle((x + 8, y + 8, x + 8 + label_w, y + 32), fill=(0, 0, 0))
        draw.text((x + 16, y + 13), label, fill=WHITE, font=fonts["small"])


def draw_timeline(
    draw: ImageDraw.ImageDraw,
    data: RunData,
    frame: VideoFrame,
    *,
    y: int,
    width: int,
    height: int,
    fonts: dict[str, ImageFont.ImageFont],
) -> None:
    x0 = 16
    x1 = width - 16
    bar_w = x1 - x0
    total = max(1, len(data.frames))
    text_y = y + 14

    events_text = ", ".join(event_types(frame.raw)) or "-"
    tool_text = tool_summary(frame.raw)
    parse = "ok" if frame.raw.get("parse_ok", True) else f"parse error: {frame.raw.get('parse_error') or ''}"
    draw_wrapped(
        draw,
        f"Events: {events_text}  |  Tools: {tool_text}  |  Parse: {parse}",
        (x0, text_y),
        max_width=bar_w,
        font=fonts["small"],
        fill=TEXT,
        max_lines=2,
    )

    label_w = 88
    track_x0 = x0 + label_w
    track_w = x1 - track_x0
    subtask_y = y + 62
    monitor_y = y + 92
    reasoning_y = y + 122
    for label, ty in (("Subtask", subtask_y), ("Monitor", monitor_y), ("Reasoning", reasoning_y)):
        draw.text((x0, ty + 2), label, fill=TEXT, font=fonts["tiny"])
        draw.rectangle((track_x0, ty, x1, ty + 16), fill=(30, 41, 59))

    for idx, item in enumerate(data.frames):
        left = track_x0 + int(idx * track_w / total)
        right = track_x0 + int((idx + 1) * track_w / total)
        if right <= left:
            right = left + 1
        subtask_color = SUBTASK_COLORS[subtask_color_index(item.raw) % len(SUBTASK_COLORS)]
        status = monitor_status(item.raw)
        draw.rectangle((left, subtask_y, right, subtask_y + 18), fill=subtask_color)
        draw.rectangle((left, monitor_y, right, monitor_y + 18), fill=STATUS_COLORS.get(status, SUBTLE))
        reason_color = (99, 102, 241) if item.raw.get("vlm_called") else (71, 85, 105)
        draw.rectangle((left, reasoning_y, right, reasoning_y + 18), fill=reason_color)

    marker_x = track_x0 + int(frame.global_index * track_w / total)
    draw.line((marker_x, subtask_y - 8, marker_x, reasoning_y + 28), fill=WHITE, width=2)
    draw.ellipse((marker_x - 4, subtask_y - 12, marker_x + 4, subtask_y - 4), fill=WHITE)

    legend_y = y + height - 28
    legend_items = [
        ("subtask color = current subtask index", SUBTASK_COLORS[0]),
        ("monitor running", STATUS_COLORS["running"]),
        ("monitor success", STATUS_COLORS["success"]),
        ("monitor failed/error", STATUS_COLORS["failed"]),
        ("VLM reasoning", (99, 102, 241)),
        ("monitor-only poll", (71, 85, 105)),
    ]
    lx = x0
    for text, color in legend_items:
        draw.rectangle((lx, legend_y + 4, lx + 14, legend_y + 18), fill=color)
        draw.text((lx + 20, legend_y + 2), text, fill=MUTED, font=fonts["tiny"])
        lx += int(draw.textlength(text, font=fonts["tiny"])) + 48


def draw_status_grid(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    items: list[tuple[str, str, tuple[int, int, int]]],
    fonts: dict[str, ImageFont.ImageFont],
) -> None:
    cell_w = 184
    cell_h = 46
    gap = 8
    for idx, (label, value, color) in enumerate(items):
        col = idx % 2
        row = idx // 2
        x0 = x + col * (cell_w + gap)
        y0 = y + row * (cell_h + gap)
        draw.rounded_rectangle((x0, y0, x0 + cell_w, y0 + cell_h), radius=8, fill=(30, 41, 59))
        draw.rectangle((x0, y0, x0 + 6, y0 + cell_h), fill=color)
        draw.text((x0 + 14, y0 + 7), label.upper(), fill=MUTED, font=fonts["tiny"])
        draw.text((x0 + 14, y0 + 23), ellipsize(value, 22), fill=TEXT, font=fonts["small"])


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    *,
    max_width: int,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    max_lines: int,
) -> None:
    x, y = xy
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
    line_h = text_height(draw, font) + 3
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_h


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int,
) -> list[str]:
    wrapped: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            wrapped.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                wrapped.append(line)
                line = word
                if len(wrapped) >= max_lines:
                    break
        if len(wrapped) < max_lines:
            wrapped.append(line)
        if len(wrapped) >= max_lines:
            break
    if len(wrapped) == max_lines and draw.textlength(wrapped[-1], font=font) > max_width:
        wrapped[-1] = ellipsize(wrapped[-1], 80)
    elif len(wrapped) == max_lines:
        wrapped[-1] = ellipsize(wrapped[-1], 100)
    return wrapped[:max_lines]


def load_camera_image(image_paths: dict[str, Path], camera: str, panel_size: tuple[int, int]) -> Image.Image:
    path = image_paths.get(camera)
    if path is None and camera == "cam_high":
        path = image_paths.get("main")
    if path is None:
        path = image_paths.get("main")
    if path and path.is_file():
        try:
            image = Image.open(path).convert("RGB")
            return ImageOps.pad(image, panel_size, color=BLACK, centering=(0.5, 0.5))
        except OSError:
            pass
    placeholder = Image.new("RGB", panel_size, BLACK)
    draw = ImageDraw.Draw(placeholder)
    draw.text((16, panel_size[1] // 2 - 8), f"missing {camera}", fill=MUTED, font=ImageFont.load_default())
    return placeholder


def load_fonts() -> dict[str, ImageFont.ImageFont]:
    regular_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    regular_path = first_existing(regular_candidates)
    bold_path = first_existing(bold_candidates) or regular_path
    if regular_path is None:
        default = ImageFont.load_default()
        return {"bold": default, "regular": default, "small": default, "tiny": default}
    return {
        "bold": ImageFont.truetype(str(bold_path or regular_path), 20),
        "regular": ImageFont.truetype(str(regular_path), 16),
        "small": ImageFont.truetype(str(regular_path), 13),
        "tiny": ImageFont.truetype(str(regular_path), 11),
    }


def first_existing(paths: list[str]) -> Path | None:
    for path in paths:
        candidate = Path(path)
        if candidate.exists():
            return candidate
    return None


def frame_size(panel_w: int, panel_h: int, header_h: int, timeline_h: int) -> tuple[int, int]:
    width = 16 * 2 + 3 * panel_w + 2 * 8
    height = header_h + panel_h + timeline_h
    return even(width), even(height)


def even(value: int) -> int:
    return value if value % 2 == 0 else value + 1


def _session_info(sessions: dict[str, SessionInfo], order: list[str], session_id: str) -> SessionInfo:
    if session_id not in sessions:
        order.append(session_id)
        sessions[session_id] = SessionInfo(session_id=session_id, index=len(order))
    return sessions[session_id]


def _step_image_paths(run_dir: Path, event: dict[str, Any]) -> dict[str, Path]:
    images = _nested(event, "planner_input", "images")
    paths: dict[str, Path] = {}
    if not isinstance(images, dict):
        return paths
    for label, payload in images.items():
        if not isinstance(payload, dict):
            continue
        rel = payload.get("path")
        if isinstance(rel, str) and rel:
            path = Path(rel)
            paths[str(label)] = path if path.is_absolute() else run_dir / path
    return paths


def current_subtask(event: dict[str, Any]) -> str:
    return str(event.get("current_subtask") or _nested(event, "planner_input", "current_subtask") or "")


def subtask_index_text(event: dict[str, Any]) -> str:
    idx = event.get("subtask_index")
    subtasks = _nested(event, "planner_input", "subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        subtasks = _nested(event, "planner_output", "subtasks")
    total = len(subtasks) if isinstance(subtasks, list) else 0
    if idx is None:
        return ""
    if total:
        return f"[{int(idx) + 1}/{total}]"
    return f"[{idx}]"


def subtask_color_index(event: dict[str, Any]) -> int:
    idx = event.get("subtask_index")
    if idx is None:
        idx = _nested(event, "planner_input", "subtask_index")
    if idx is not None:
        try:
            return int(idx)
        except (TypeError, ValueError):
            pass
    subtask = current_subtask(event)
    if not subtask:
        return 0
    return sum(ord(char) for char in subtask) % len(SUBTASK_COLORS)


def monitor_status(event: dict[str, Any]) -> str:
    status = event.get("monitor_status")
    if status:
        return str(status)
    active = event.get("active_execution")
    if isinstance(active, dict) and active.get("status"):
        return str(active["status"])
    event_types = event_types_list(event)
    if "monitor_timeout" in event_types:
        return "timeout"
    if event.get("phase") == "error" or event.get("parse_ok") is False:
        return "error"
    return "none"


def event_types(event: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(event_types_list(event)))


def event_types_list(event: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in event.get("events") or []:
        if isinstance(item, dict) and item.get("event_type"):
            values.append(str(item["event_type"]))
    return values


def tool_summary(event: dict[str, Any]) -> str:
    names = []
    for item in event.get("tool_results") or []:
        if isinstance(item, dict):
            name = item.get("tool_name")
            status = item.get("status")
            if name:
                names.append(f"{name}:{status or '-'}")
    if not names:
        return "-"
    return ellipsize(", ".join(names), 120)


def _nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def ellipsize(text: str, max_chars: int) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def safe_filename(text: str) -> str:
    safe = []
    for char in text:
        if char.isalnum() or char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "session"


def short_session_id(session_id: str) -> str:
    parts = session_id.split("_")
    if len(parts) >= 2 and parts[0] == "session":
        return parts[1]
    return ellipsize(session_id, 18)


def text_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
