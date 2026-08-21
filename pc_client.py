#!/usr/bin/env python3
"""
pc_client.py — PristineCam PC client (headless)
=================================================
Receives an MJPEG stream from the PristineCam Android app and publishes it
as a system virtual webcam via pyvirtualcam.

Architecture:
  - A dedicated **capture thread** runs cap.read() in a tight loop,
    continuously overwriting a single shared variable with the newest frame.
  - The **main thread** reads that variable at the target FPS cadence and
    pushes it to pyvirtualcam.  Any intermediate frames that arrived between
    two reads are silently dropped — this eliminates buffer delay.

Usage
-----
    python pc_client.py --url http://192.168.1.42:8080/video_feed

For USB/ADB port-forwarding (lower latency, no Wi-Fi needed):
    adb forward tcp:8080 tcp:8080
    python pc_client.py --url http://127.0.0.1:8080/video_feed
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from typing import Optional

import cv2
import numpy as np
import pyvirtualcam
from pyvirtualcam import PixelFormat

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pc_client")


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_FPS: int = 30
DEFAULT_WIDTH: int = 1280
DEFAULT_HEIGHT: int = 720

# How long (seconds) to wait between reconnection attempts.
RECONNECT_DELAY_INITIAL: float = 1.0
RECONNECT_DELAY_MAX: float = 16.0   # caps exponential back-off

# Consecutive empty reads before we consider the stream dead.
MAX_EMPTY_FRAMES: int = 30


# ---------------------------------------------------------------------------
# Capture thread — reads frames as fast as they arrive
# ---------------------------------------------------------------------------
class CaptureThread:
    """
    Runs cv2.VideoCapture.read() in a background daemon thread and stores
    the most recent frame in a single slot.  The consumer never blocks the
    capture loop; it simply reads whatever is in the slot.

    Thread safety:
      Under CPython's GIL a reference-swap (self._frame = frame) is atomic,
      so the consumer always sees either the old or the new frame — never a
      torn intermediate.  No locks needed.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._frame: Optional[np.ndarray] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture] = None
        self._connected = False
        self._empty_streak = 0

    @property
    def frame(self) -> Optional[np.ndarray]:
        """Latest captured frame (or None if nothing received yet)."""
        return self._frame

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._cap:
            self._cap.release()
            self._cap = None

    # ── Internal loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        reconnect_delay = RECONNECT_DELAY_INITIAL

        while self._running:
            # ── Connect / reconnect ───────────────────────────────────
            if self._cap is None:
                self._connected = False
                log.info("Connecting to stream: %s", self._url)

                cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
                # Minimise OpenCV's internal frame queue.
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                # Hint the demuxer to expect MJPEG.
                cap.set(cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter.fourcc(*"MJPG"))

                if not cap.isOpened():
                    cap.release()
                    log.warning(
                        "Could not open stream. Retrying in %.1f s…",
                        reconnect_delay,
                    )
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2,
                                          RECONNECT_DELAY_MAX)
                    continue

                self._cap = cap
                self._connected = True
                self._empty_streak = 0
                reconnect_delay = RECONNECT_DELAY_INITIAL
                log.info("Stream opened.")

            # ── Read one frame (blocking, but in this thread only) ────
            ok, frame = self._cap.read()

            if not ok or frame is None:
                self._empty_streak += 1
                if self._empty_streak >= MAX_EMPTY_FRAMES:
                    log.warning(
                        "Too many empty frames (%d). Reconnecting…",
                        MAX_EMPTY_FRAMES,
                    )
                    self._cap.release()
                    self._cap = None
                continue

            self._empty_streak = 0
            # Atomic under CPython GIL — consumer always sees a
            # complete ndarray reference, never a torn write.
            self._frame = frame

    def resolution(self) -> tuple[int, int]:
        """Return (width, height) of the latest captured frame, or (0, 0)."""
        f = self._frame
        if f is not None:
            return f.shape[1], f.shape[0]
        return 0, 0


# ---------------------------------------------------------------------------
# Core streaming loop (main thread)
# ---------------------------------------------------------------------------
def stream(url: str, target_fps: int, width: int, height: int) -> None:
    """
    Main loop: spin up the capture thread, then at a steady cadence
    grab the latest frame and push it to pyvirtualcam.
    """
    cap_thread = CaptureThread(url)
    cap_thread.start()

    cam: Optional[pyvirtualcam.Camera] = None
    cam_dims: tuple[int, int] = (0, 0)
    frame_interval = 1.0 / target_fps

    try:
        log.info("Waiting for first frame…")

        while True:
            t0 = time.perf_counter()

            frame = cap_thread.frame
            if frame is None:
                # No frame yet — wait briefly and retry.
                time.sleep(0.01)
                continue

            fh, fw = frame.shape[:2]

            # ── Create / recreate virtual camera on resolution change ─
            if cam is None or cam_dims != (fw, fh):
                if cam is not None:
                    cam.close()
                    cam = None
                log.info(
                    "Creating virtual camera (%dx%d @ %d fps)…",
                    fw, fh, target_fps,
                )
                cam = pyvirtualcam.Camera(
                    width=fw,
                    height=fh,
                    fps=target_fps,
                    fmt=PixelFormat.BGR,
                    print_fps=False,
                )
                cam_dims = (fw, fh)
                log.info("Virtual camera device: %s", cam.device)

            # ── Push to virtual cam ───────────────────────────────────
            # Resize if stream resolution changed mid-session.
            if fw != cam.width or fh != cam.height:
                frame = cv2.resize(
                    frame, (cam.width, cam.height),
                    interpolation=cv2.INTER_LINEAR,
                )

            cam.send(frame)

            # ── Pace to target FPS ────────────────────────────────────
            elapsed = time.perf_counter() - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        cap_thread.stop()
        if cam is not None:
            cam.close()
        log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Forward an MJPEG stream from an Android device to a virtual webcam.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=f"http://192.168.1.100:8080/video_feed",
        help="Full URL of the MJPEG stream on the Android device.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_FPS,
        help="Target frame-rate for the virtual camera.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help="Preferred capture width (hint only; stream may differ).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_HEIGHT,
        help="Preferred capture height (hint only; stream may differ).",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    log.info(
        "Starting PC client  url=%s  fps=%d  preferred=%dx%d",
        args.url, args.fps, args.width, args.height,
    )
    stream(
        url=args.url,
        target_fps=args.fps,
        width=args.width,
        height=args.height,
    )


if __name__ == "__main__":
    main()
