#!/usr/bin/env python3
"""
x2robot Bridge Server
=====================
Lightweight HTTP bridge running on the robot side (inside turtle2_release Docker).
Exposes REST endpoints for the remote RoboClaw Agent to:
  - Pull three-camera images (from video_streaming.py shared memory)
  - Control task execution (via rosparam /running_mode)
  - Query robot status (arm positions, running mode)
  - Reset the robot arms to home pose

Dependencies (install in the robot venv):
    pip install fastapi uvicorn

Usage:
    python x2robot_bridge_server.py [--port 8766]
"""

from __future__ import annotations

import argparse
import base64
import logging
import struct
import threading
import time
from typing import Optional

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from multiprocessing import shared_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [x2robot-bridge] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ROS imports (optional — gracefully degrade when running outside ROS)
# ---------------------------------------------------------------------------
try:
    import rospy
    from cv_bridge import CvBridge
    from sensor_msgs.msg import Image as ROSImage, CompressedImage
    from communicationPort.msg import PosCmd

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False
    logger.warning("rospy not available — running in headless/mock mode")

# ---------------------------------------------------------------------------
# Image source: shared memory or ROS topics
# ---------------------------------------------------------------------------

# Default ROS topic names (left_wrist, head, right_wrist)
DEFAULT_ROS_IMAGE_TOPICS = [
    "/camera1/usb_cam1/image_raw",   # left wrist
    "/camera2/usb_cam2/image_raw",   # head
    "/camera3/usb_cam3/image_raw",   # right wrist
]

SHM_NAMES = [
    "video_streaming_cam0",  # left wrist
    "video_streaming_cam1",  # head
    "video_streaming_cam2",  # right wrist
]


class ROSImageReader:
    """Read camera frames by subscribing to ROS image topics."""

    def __init__(self, topics: list[str] | None = None):
        self._topics = topics or DEFAULT_ROS_IMAGE_TOPICS
        self._bridge = CvBridge() if ROS_AVAILABLE else None
        self._frames: list[Optional[np.ndarray]] = [None, None, None]
        self._locks = [threading.Lock() for _ in range(3)]
        self._subs = []

        if not ROS_AVAILABLE:
            logger.warning("ROSImageReader: rospy not available")
            return

        for i, topic in enumerate(self._topics):
            if "/compressed" in topic:
                sub = rospy.Subscriber(topic, CompressedImage, self._make_compressed_cb(i), queue_size=1)
            else:
                sub = rospy.Subscriber(topic, ROSImage, self._make_raw_cb(i), queue_size=1)
            self._subs.append(sub)
            logger.info("ROSImageReader: subscribed to %s", topic)

    def _make_raw_cb(self, idx: int):
        def cb(msg):
            try:
                frame = self._bridge.imgmsg_to_cv2(msg, "bgr8")
                with self._locks[idx]:
                    self._frames[idx] = frame
            except Exception as e:
                logger.warning("ROSImageReader: decode failed for cam%d: %s", idx, e)
        return cb

    def _make_compressed_cb(self, idx: int):
        def cb(msg):
            try:
                np_arr = np.frombuffer(msg.data, np.uint8)
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    with self._locks[idx]:
                        self._frames[idx] = frame
            except Exception as e:
                logger.warning("ROSImageReader: compressed decode failed for cam%d: %s", idx, e)
        return cb

    def read_frame(self, idx: int) -> Optional[np.ndarray]:
        with self._locks[idx]:
            return self._frames[idx].copy() if self._frames[idx] is not None else None

    def read_all(self) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        return self.read_frame(0), self.read_frame(1), self.read_frame(2)

    def close(self):
        for sub in self._subs:
            try:
                sub.unregister()
            except Exception:
                pass


class SharedMemoryImageReader:
    """Read camera frames from shared memory written by video_streaming.py."""

    def __init__(self):
        self._shm: list[Optional[shared_memory.SharedMemory]] = [None, None, None]

    def _connect(self, idx: int) -> bool:
        if self._shm[idx] is not None:
            return True
        try:
            self._shm[idx] = shared_memory.SharedMemory(
                name=SHM_NAMES[idx], create=False
            )
            logger.info("Connected to shared memory %s", SHM_NAMES[idx])
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.warning("Failed to connect shared memory %d: %s", idx, e)
            return False

    def read_frame(self, idx: int) -> Optional[np.ndarray]:
        if self._shm[idx] is None and not self._connect(idx):
            return None
        shm = self._shm[idx]
        try:
            width = struct.unpack("<I", shm.buf[:4])[0]
            height = struct.unpack("<I", shm.buf[4:8])[0]
            if width == 0 or height == 0 or width > 10000 or height > 10000:
                return None
            frame_size = width * height * 3
            if frame_size + 8 > len(shm.buf):
                return None
            data = np.frombuffer(shm.buf[8 : 8 + frame_size], dtype=np.uint8)
            return data.reshape((height, width, 3))
        except Exception as e:
            logger.warning("Failed to read frame %d: %s", idx, e)
            try:
                self._shm[idx].close()
            except Exception:
                pass
            self._shm[idx] = None
            return None

    def read_all(self) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        return self.read_frame(0), self.read_frame(1), self.read_frame(2)

    def close(self):
        for i, shm in enumerate(self._shm):
            if shm is not None:
                try:
                    shm.close()
                except Exception:
                    pass
                self._shm[i] = None


# ---------------------------------------------------------------------------
# ROS helpers
# ---------------------------------------------------------------------------

def _get_running_mode() -> int:
    if not ROS_AVAILABLE:
        return 0
    try:
        return rospy.get_param("/running_mode", 0)
    except Exception:
        return 0


def _set_running_mode(mode: int):
    if not ROS_AVAILABLE:
        logger.info("(mock) set /running_mode = %d", mode)
        return
    rospy.set_param("/running_mode", mode)
    logger.info("Set /running_mode = %d", mode)


def _get_arm_pos(topic: str, timeout: float = 2.0) -> Optional[list[float]]:
    if not ROS_AVAILABLE:
        return [0.0] * 7
    try:
        msg = rospy.wait_for_message(topic, PosCmd, timeout=timeout)
        return [msg.x, msg.y, msg.z, msg.roll, msg.pitch, msg.yaw, msg.gripper]
    except Exception:
        return None


def _reset_arms_blocking():
    """Reset arms to home pose (same logic as reset_arm.py)."""
    # [SAFE TEST] 跳过实际复位，仅打印
    logger.info("[SAFE TEST] reset_arms called but NOT executed — robot stays still")
    return True

    # --- 以下为真机复位代码，测试通过后取消注释 ---
    # if not ROS_AVAILABLE:
    #     logger.info("(mock) reset arms — no-op")
    #     return True
    #
    # pub1 = rospy.Publisher("/follow_pos_cmd_1", PosCmd, queue_size=10)
    # pub2 = rospy.Publisher("/follow_pos_cmd_2", PosCmd, queue_size=10)
    # rate = rospy.Rate(50)
    #
    # start1 = _get_arm_pos("/follow1_pos_back", timeout=5.0)
    # start2 = _get_arm_pos("/follow2_pos_back", timeout=5.0)
    # if start1 is None or start2 is None:
    #     logger.error("Cannot read current arm poses for reset")
    #     return False
    #
    # def _to_msg(values):
    #     msg = PosCmd()
    #     msg.x, msg.y, msg.z, msg.roll, msg.pitch, msg.yaw, msg.gripper = values
    #     msg.mode1 = 0
    #     msg.mode2 = 0
    #     return msg
    #
    # start1[6] = 5.0
    # start2[6] = 5.0
    # pub1.publish(_to_msg(start1))
    # pub2.publish(_to_msg(start2))
    # rospy.sleep(1.0)
    #
    # target = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0]
    # steps = 75
    # for i in range(1, steps + 1):
    #     ratio = float(i) / float(steps)
    #     pose1 = [s + (t - s) * ratio for s, t in zip(start1, target)]
    #     pose2 = [s + (t - s) * ratio for s, t in zip(start2, target)]
    #     pub1.publish(_to_msg(pose1))
    #     pub2.publish(_to_msg(pose2))
    #     rate.sleep()
    #
    # target[6] = 0.0
    # pub1.publish(_to_msg(target))
    # pub2.publish(_to_msg(target))
    # logger.info("Arm reset complete")
    # return True


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="x2robot Bridge Server")
image_reader = None  # initialized in main()

JPEG_QUALITY = 85


def _encode_frame_b64(frame: Optional[np.ndarray]) -> Optional[str]:
    if frame is None:
        return None
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _ok(data=None, message="ok"):
    return JSONResponse({"success": True, "data": data, "message": message})


def _fail(message: str, status: int = 400):
    return JSONResponse(
        {"success": False, "data": None, "message": message},
        status_code=status,
    )


# ---- Image endpoints ----

@app.get("/cameras/latest")
async def cameras_latest():
    left, head, right = image_reader.read_all()
    if left is None or head is None or right is None:
        return _fail(
            "Camera data unavailable. Check image source (ROS topics or shared memory).",
            status=503,
        )
    return _ok({
        "left_wrist": _encode_frame_b64(left),
        "head": _encode_frame_b64(head),
        "right_wrist": _encode_frame_b64(right),
        "timestamp": time.time(),
    })


@app.get("/cameras/concatenated")
async def cameras_concatenated():
    """Return a single concatenated image (left | head | right) as JPEG base64."""
    left, head, right = image_reader.read_all()
    if left is None or head is None or right is None:
        return _fail("Camera data unavailable", status=503)

    min_h = min(left.shape[0], head.shape[0], right.shape[0])

    def _resize_h(img, h):
        oh, ow = img.shape[:2]
        return cv2.resize(img, (int(ow * h / oh), h))

    concat = cv2.hconcat([_resize_h(left, min_h), _resize_h(head, min_h), _resize_h(right, min_h)])
    ok, buf = cv2.imencode(".jpg", concat, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return _fail("Image encoding failed", status=500)
    return _ok({
        "concatenated_image": base64.b64encode(buf.tobytes()).decode("utf-8"),
        "timestamp": time.time(),
    })


# ---------------------------------------------------------------------------
# Human-loop task state (used when --human-loop is enabled)
# ---------------------------------------------------------------------------

_human_loop_enabled = False
_human_loop_timeout = 30.0  # seconds before auto-complete

class _HumanLoopTask:
    def __init__(self):
        self.state = "idle"   # idle / running / completed
        self.prompt = ""
        self.start_time = 0.0
        self.current_step = 0
        self.total_steps = 0

    def start(self, prompt: str, timeout: float):
        self.state = "running"
        self.prompt = prompt
        self.start_time = time.time()
        self.current_step = 0
        self.total_steps = int(timeout)
        logger.info("[HumanLoop] Task started: %r (auto-complete in %ds)", prompt, int(timeout))

    def stop(self):
        self.state = "idle"
        self.prompt = ""
        logger.info("[HumanLoop] Task stopped")

    def complete(self):
        self.state = "completed"
        logger.info("[HumanLoop] Task marked completed")

    def check_auto_complete(self, timeout: float):
        if self.state == "running" and self.start_time > 0:
            elapsed = time.time() - self.start_time
            self.current_step = min(int(elapsed), self.total_steps)
            if elapsed >= timeout:
                self.state = "completed"
                logger.info("[HumanLoop] Task auto-completed after %.0fs", elapsed)

_hl_task = _HumanLoopTask()


# ---- Task control endpoints ----

@app.post("/task/start")
async def task_start():
    if _human_loop_enabled:
        prompt = ""
        if ROS_AVAILABLE:
            prompt = rospy.get_param("/task_prompt", "")
        _hl_task.start(prompt, _human_loop_timeout)
        return _ok(f"[HumanLoop] Task started — auto-complete in {int(_human_loop_timeout)}s. "
                    f"POST /task/human_done to complete early.")
    _set_running_mode(1)
    return _ok("running_mode set to 1 (autonomous)")


@app.post("/task/stop")
async def task_stop():
    if _human_loop_enabled:
        _hl_task.stop()
        return _ok("[HumanLoop] Task stopped")
    _set_running_mode(0)
    return _ok("running_mode set to 0 (idle)")


@app.post("/task/reset")
async def task_reset():
    if _human_loop_enabled:
        _hl_task.stop()
        return _ok("[HumanLoop] Task reset (no arm movement in human-loop mode)")
    _set_running_mode(0)
    success = _reset_arms_blocking()
    if success:
        return _ok("running_mode set to 0 and arms reset to home pose")
    return _fail("running_mode set to 0 but arm reset failed", status=500)


@app.post("/task/human_done")
async def task_human_done():
    """Manual endpoint: human signals that the current subtask is done."""
    if not _human_loop_enabled:
        return _fail("human-loop mode is not enabled", status=400)
    _hl_task.complete()
    return _ok("[HumanLoop] Task marked as completed by human")


@app.post("/task/set_params")
async def task_set_params(body: dict):
    params = body.get("evaluate_params", {})
    if not params:
        return _fail("missing evaluate_params")

    prompt = params.get("prompt", "")
    if ROS_AVAILABLE:
        rospy.set_param("/task_prompt", prompt)

    policy = params.get("policy", {})
    if policy:
        if ROS_AVAILABLE:
            if policy.get("host"):
                rospy.set_param("/inference_server_ip", policy["host"])
            if policy.get("port"):
                rospy.set_param("/inference_server_port", int(policy["port"]))

    step_interval = params.get("step_interval")
    if step_interval is not None and ROS_AVAILABLE:
        rospy.set_param("/step_interval", float(step_interval))

    logger.info(
        "set_params: prompt=%r  policy=%s  step_interval=%s",
        prompt, policy, step_interval,
    )
    return _ok({
        "prompt": prompt,
        "policy": policy,
        "step_interval": step_interval,
    })


@app.post("/task/set_prompt")
async def task_set_prompt(body: dict):
    prompt = body.get("prompt", "")
    if not prompt:
        return _fail("missing 'prompt'")
    if ROS_AVAILABLE:
        rospy.set_param("/task_prompt", prompt)
    logger.info("set_prompt: %r", prompt)
    return _ok({"prompt": prompt})


@app.get("/task/prompt")
async def task_get_prompt():
    prompt = ""
    if ROS_AVAILABLE:
        prompt = rospy.get_param("/task_prompt", "")
    return _ok({"prompt": prompt})


# ---- Status endpoints ----

@app.get("/status")
async def get_status():
    if _human_loop_enabled:
        _hl_task.check_auto_complete(_human_loop_timeout)
        elapsed = time.time() - _hl_task.start_time if _hl_task.start_time > 0 else 0
        return _ok({
            "state": _hl_task.state,
            "prompt": _hl_task.prompt,
            "current_step": _hl_task.current_step,
            "total_steps": _hl_task.total_steps,
            "elapsed_s": round(elapsed, 1),
            "mode": "human-loop",
            "hint": "POST /task/human_done to complete early" if _hl_task.state == "running" else "",
        })

    mode = _get_running_mode()
    mode_text = {0: "idle", 1: "autonomous", 2: "teleop"}.get(mode, f"unknown({mode})")

    left_pos = _get_arm_pos("/follow1_pos_back", timeout=1.0)
    right_pos = _get_arm_pos("/follow2_pos_back", timeout=1.0)

    inference_ip = ""
    inference_port = 0
    prompt = ""
    if ROS_AVAILABLE:
        inference_ip = rospy.get_param("/inference_server_ip", "")
        inference_port = rospy.get_param("/inference_server_port", 0)
        prompt = rospy.get_param("/task_prompt", "")

    return _ok({
        "state": mode_text,
        "running_mode": mode,
        "prompt": prompt,
        "inference_server": f"{inference_ip}:{inference_port}" if inference_ip else "",
        "left_arm_pos": left_pos,
        "right_arm_pos": right_pos,
    })


@app.post("/task/emergency_stop")
async def emergency_stop():
    _set_running_mode(0)
    logger.warning("EMERGENCY STOP triggered")
    return _ok("emergency stop executed — running_mode set to 0")


@app.get("/health")
async def health():
    reader_type = type(image_reader).__name__ if image_reader else "None"
    return _ok({
        "ros_available": ROS_AVAILABLE,
        "image_source": reader_type,
        "running_mode": _get_running_mode(),
    })


@app.get("/")
async def root():
    return JSONResponse({
        "service": "x2robot_bridge_server",
        "status": "running",
        "endpoints": [
            "GET  /cameras/latest",
            "GET  /cameras/concatenated",
            "POST /task/start",
            "POST /task/stop",
            "POST /task/reset",
            "POST /task/set_params",
            "POST /task/set_prompt",
            "GET  /task/prompt",
            "GET  /status",
            "POST /task/emergency_stop",
            "GET  /health",
        ],
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global image_reader, _human_loop_enabled, _human_loop_timeout

    parser = argparse.ArgumentParser(description="x2robot Bridge Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8766, help="Listen port")
    parser.add_argument(
        "--image-source", choices=["ros", "shm"], default="ros",
        help="Image source: 'ros' for ROS topics (default), 'shm' for shared memory",
    )
    parser.add_argument(
        "--ros-topics", nargs=3, metavar=("LEFT", "HEAD", "RIGHT"),
        default=None,
        help="Override ROS image topic names (left_wrist, head, right_wrist)",
    )
    parser.add_argument(
        "--human-loop", action="store_true",
        help="Enable human-in-the-loop mode: tasks auto-complete after timeout, "
             "human can POST /task/human_done to complete early",
    )
    parser.add_argument(
        "--human-loop-timeout", type=float, default=10.0,
        help="Seconds before a task auto-completes in human-loop mode (default: 30)",
    )
    args = parser.parse_args()

    _human_loop_enabled = args.human_loop
    _human_loop_timeout = args.human_loop_timeout

    if ROS_AVAILABLE:
        rospy.init_node("x2robot_bridge_server", disable_signals=True)
        logger.info("ROS node initialized")

    if args.image_source == "ros":
        topics = args.ros_topics or DEFAULT_ROS_IMAGE_TOPICS
        image_reader = ROSImageReader(topics=topics)
        logger.info("Image source: ROS topics %s", topics)
    else:
        image_reader = SharedMemoryImageReader()
        logger.info("Image source: shared memory")

    if _human_loop_enabled:
        logger.info("*** HUMAN-LOOP MODE enabled (auto-complete: %ds) ***", int(_human_loop_timeout))
        logger.info("*** Robot will NOT move. You manually change objects, then POST /task/human_done ***")

    logger.info("Starting x2robot Bridge Server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()