#!/usr/bin/env python3
"""
pc_client.py — PristineCam PC client (headless / CLI)
======================================================

Connects to the MJPEG stream broadcast by the Android app and forwards
every frame into a virtual webcam so any video-conferencing tool can use it.

Usage
-----
    # Auto-detect port and path — just give the phone's IP:
    python pc_client.py --ip 192.168.1.42

    # With explicit port:
    python pc_client.py --ip 192.168.1.42 --port 8080

    # USB / ADB mode (lower latency, no Wi-Fi required):
    adb forward tcp:8080 tcp:8080
    python pc_client.py --ip 127.0.0.1

    # Full URL override if you need something non-standard:
    python pc_client.py --url http://192.168.1.42:8080/video_feed
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import cv2
import numpy as np

try:
    import pyvirtualcam
    from pyvirtualcam import PixelFormat
    HAS_VCAM = True
except ImportError:
    HAS_VCAM = False

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
DEFAULT_IP:   str = "192.168.1.100"
DEFAULT_PORT: int = 8080
DEFAULT_FPS:  int = 30

# How long (seconds) to wait between reconnection attempts (exponential back-off).
RECONNECT_DELAY_INITIAL: float = 1.0
RECONNECT_DELAY_MAX:     float = 16.0

# Consecutive empty reads before we consider the stream dead.
MAX_EMPTY_FRAMES: int = 30


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------
def build_url(ip: str, port: int) -> str:
    """
    Construct the stream URL from an IP (or hostname) and port.
    Handles missing scheme gracefully and always appends /video_feed.

    Examples
    --------
    >>> build_url("192.168.1.42", 8080)
    'http://192.168.1.42:8080/video_feed'
    """
    host = ip.strip()
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    parsed = urlparse(host)
    netloc = parsed.netloc or parsed.path  # handles bare hostnames with no path
    # Strip any stray path fragment so we can append cleanly
    if ":" not in netloc:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}/video_feed"


def normalize_url(raw: str, port: int = DEFAULT_PORT) -> str:
    """
    Normalize a raw URL/IP string from --url.
    - Prepends http:// if scheme is missing.
    - Appends :PORT if no port is present.
    - Appends /video_feed if path is missing or just '/'.
    """
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    parsed = urlparse(raw)
    netloc = parsed.netloc
    path   = parsed.path or "/video_feed"
    if parsed.port is None:
        netloc = f"{netloc}:{port}"
    if not path or path == "/":
        path = "/video_feed"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}"


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
        self._url    = url
        self._frame: Optional[np.ndarray] = None
        self._running      = False
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture]    = None
        self._connected    = False
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
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._cap:
            self._cap.release()
            self._cap = None

    def resolution(self) -> tuple[int, int]:
        """Return (width, height) of the latest captured frame, or (0, 0)."""
        f = self._frame
        return (f.shape[1], f.shape[0]) if f is not None else (0, 0)

    # ── Internal loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        reconnect_delay = RECONNECT_DELAY_INITIAL

        while self._running:
            # ── Connect / reconnect ────────────────────────────────────
            if self._cap is None:
                self._connected = False
                log.info("Connecting to %s …", self._url)

                cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))

                if not cap.isOpened():
                    cap.release()
                    log.warning("Could not open stream. Retrying in %.1f s…", reconnect_delay)
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, RECONNECT_DELAY_MAX)
                    continue

                self._cap          = cap
                self._connected    = True
                self._empty_streak = 0
                reconnect_delay    = RECONNECT_DELAY_INITIAL
                log.info("Stream opened.")

            # ── Read one frame ─────────────────────────────────────────
            ok, frame = self._cap.read()

            if not ok or frame is None:
                self._empty_streak += 1
                if self._empty_streak >= MAX_EMPTY_FRAMES:
                    log.warning("Too many empty frames — reconnecting…")
                    self._cap.release()
                    self._cap = None
                continue

            self._empty_streak = 0
            self._frame = frame   # atomic under CPython GIL


# ---------------------------------------------------------------------------
# Core streaming loop (main thread)
# ---------------------------------------------------------------------------
def stream(url: str, target_fps: int) -> None:
    """
    Main loop: spin up the capture thread, then at a steady cadence
    grab the latest frame and push it to pyvirtualcam.
    If pyvirtualcam is not installed, frames are displayed in a window instead.
    """
    cap_thread = CaptureThread(url)
    cap_thread.start()

    cam: Optional["pyvirtualcam.Camera"] = None  # type: ignore[name-defined]
    cam_dims: tuple[int, int] = (0, 0)
    frame_interval = 1.0 / target_fps
    use_window     = not HAS_VCAM

    if use_window:
        log.warning("pyvirtualcam not installed — displaying stream in a window instead.")
        log.warning("Install with:  pip install pyvirtualcam")

    try:
        log.info("Waiting for first frame…")

        while True:
            t0    = time.perf_counter()
            frame = cap_thread.frame

            if frame is None:
                time.sleep(0.01)
                continue

            fh, fw = frame.shape[:2]

            if use_window:
                # ── Fallback: show in OpenCV window ───────────────────
                cv2.imshow("PristineCam — press Q to quit", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            else:
                # ── Create / recreate virtual camera on resolution change
                if cam is None or cam_dims != (fw, fh):
                    if cam is not None:
                        cam.close()
                        cam = None
                    log.info("Creating virtual camera (%dx%d @ %d fps)…", fw, fh, target_fps)
                    cam = pyvirtualcam.Camera(
                        width=fw, height=fh, fps=target_fps,
                        fmt=PixelFormat.BGR, print_fps=False,
                    )
                    cam_dims = (fw, fh)
                    log.info("Virtual camera device: %s", cam.device)

                if fw != cam.width or fh != cam.height:
                    frame = cv2.resize(frame, (cam.width, cam.height),
                                       interpolation=cv2.INTER_LINEAR)
                cam.send(frame)

            # ── Pace to target FPS ─────────────────────────────────────
            elapsed   = time.perf_counter() - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        cap_thread.stop()
        if cam is not None:
            cam.close()
        cv2.destroyAllWindows()
        log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Forward a PristineCam MJPEG stream from an Android device "
            "to a virtual webcam (or an OpenCV window if pyvirtualcam is not installed)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python pc_client.py --ip 192.168.1.42\n"
            "  python pc_client.py --ip 192.168.1.42 --port 8080\n"
            "  python pc_client.py --ip 127.0.0.1               # USB/ADB mode\n"
            "  python pc_client.py --url http://192.168.1.42:8080/video_feed  # full override\n"
        ),
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--ip",
        default=None,
        metavar="ADDRESS",
        help="IP address or hostname of the Android device (e.g. 192.168.1.42 or 127.0.0.1 for USB).",
    )
    src.add_argument(
        "--url",
        default=None,
        metavar="URL",
        help="Full stream URL override. Overrides --ip/--port when specified.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port the Android app is listening on.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_FPS,
        help="Target frame-rate for the virtual camera.",
    )
    return parser


def main() -> None:
    args   = _build_parser().parse_args()

    if args.url:
        url = normalize_url(args.url, port=args.port)
    elif args.ip:
        url = build_url(args.ip, args.port)
    else:
        # Neither --ip nor --url provided — use default IP
        url = build_url(DEFAULT_IP, args.port)
        log.info("No --ip specified, using default: %s", url)

    log.info("Starting PristineCam CLI client")
    log.info("  Stream URL : %s", url)
    log.info("  Target FPS : %d", args.fps)
    log.info("  Virtual cam: %s", "yes" if HAS_VCAM else "no (window fallback)")

    stream(url=url, target_fps=args.fps)


if __name__ == "__main__":
    main()
