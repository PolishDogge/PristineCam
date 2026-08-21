#!/usr/bin/env python3
"""
PristineCam — Desktop GUI Client
=================================
Connects to the PristineCam Android app's MJPEG stream, shows a live
preview, and feeds frames into a system virtual webcam via pyvirtualcam.

Architecture:
  Capture thread (threading.Thread, daemon)
  ├── cv2.VideoCapture.read() in a tight loop
  ├── Overwrites a single-slot frame variable
  └── Signals the consumer via threading.Event (no busy-poll)

  Consumer loop (QThread)
  ├── Blocks on Event.wait() — zero CPU when idle
  ├── Pushes frame to pyvirtualcam at exactly 30 FPS
  ├── Preview at 10 FPS, downscaled to 480×270 (toggleable)
  └── Virtual camera auto-starts on connect

Usage
-----
    python gui_client.py

Dependencies
------------
    pip install PyQt6 opencv-python pyvirtualcam
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    from PyQt6.QtCore import (
        Qt, QObject, QThread, QTimer,
        pyqtSignal, pyqtSlot,
    )
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
    from PyQt6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
        QSizePolicy, QSlider, QStatusBar, QVBoxLayout, QWidget,
    )
except ImportError:
    print("ERROR: PyQt6 not installed.\n  pip install PyQt6")
    sys.exit(1)

try:
    import pyvirtualcam
    from pyvirtualcam import PixelFormat
    HAS_VCAM = True
except ImportError:
    HAS_VCAM = False


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TARGET_FPS         = 30
FRAME_INTERVAL     = 1.0 / TARGET_FPS          # ~33.33 ms
PREVIEW_FPS        = 30
PREVIEW_EVERY_N    = TARGET_FPS // PREVIEW_FPS  # emit preview every 3rd frame
PREVIEW_W, PREVIEW_H = 480, 270                # preview downscale resolution

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "pristinecam_settings.json"

_DEFAULTS: dict = {
    "ip":           "192.168.1.100",
    "port":         8080,
    "usb_mode":     False,
    "mirror_h":     False,
    "mirror_v":     False,
    "rotation_idx": 0,
    "backend_idx":  0,
    "show_preview": True,
}


def _load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            return {**_DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return _DEFAULTS.copy()


def _save_cfg(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ADB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _adb_forward(port: int) -> tuple[bool, str]:
    """Run `adb forward tcp:<port> tcp:<port>`. Returns (success, message)."""
    try:
        r = subprocess.run(
            ["adb", "forward", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            return True, f"ADB: port {port} forwarded successfully"
        return False, r.stderr.strip() or "adb forward failed"
    except FileNotFoundError:
        return False, (
            "ADB not found.\n\n"
            "Install Android platform-tools and add adb to your PATH:\n"
            "  https://developer.android.com/tools/releases/platform-tools"
        )
    except subprocess.TimeoutExpired:
        return False, "ADB command timed out (is the phone connected?)"
    except Exception as exc:
        return False, str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Virtual-camera backend table
# ─────────────────────────────────────────────────────────────────────────────

_BACKENDS: dict[str, Optional[str]] = {
    "Auto-detect":   None,
    "OBS":           "obs",
    "v4l2loopback":  "v4l2loopback",
    "Unity Capture": "unitycapture",
}

_ROT_ANGLES = [0, 90, 180, 270]

_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "connecting":   ("Connecting…",    "#fcc419"),
    "connected":    ("Connected",      "#40c057"),
    "reconnecting": ("Reconnecting…",  "#fd7e14"),
    "error":        ("Connection error", "#fa5252"),
    "disconnected": ("Disconnected",   "#5c6370"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Stream worker  (lives in a QThread)
# ─────────────────────────────────────────────────────────────────────────────

class StreamWorker(QObject):
    """
    Event-driven two-thread streaming engine.

      Capture thread (daemon):
        cv2.VideoCapture.read() → self._latest_frame → Event.set()

      Consumer loop (QThread):
        Event.wait() → read frame → transforms → vcam.send() → preview emit
        Paced to exactly 30 FPS.  Preview capped at 10 FPS / 480×270.

    No busy-polling anywhere.  The consumer blocks on the Event until the
    capture thread has new data, then paces itself to 30 FPS with sleep().
    """

    frame_ready        = pyqtSignal(QImage)
    fps_updated        = pyqtSignal(float)
    latency_updated    = pyqtSignal(float)
    resolution_updated = pyqtSignal(int, int)
    status_changed     = pyqtSignal(str)
    error_occurred     = pyqtSignal(str)

    def __init__(self, url: str, backend: Optional[str]) -> None:
        super().__init__()
        self._url     = url
        self._backend = backend
        self._running = False

        # Shared capture → consumer state (GIL-atomic reference swaps)
        self._latest_frame: Optional[np.ndarray] = None
        self._capture_latency: float = 0.0
        self._frame_event = threading.Event()

        # Writable from the main thread at any time
        self.mirror_h: bool     = False
        self.mirror_v: bool     = False
        self.rotation: int      = 0
        self.show_preview: bool = True

    def stop(self) -> None:
        self._running = False
        self._frame_event.set()  # unblock consumer if waiting

    # ── Capture thread ────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        reconnect_delay = 1.0
        cap: Optional[cv2.VideoCapture] = None
        empty_streak = 0

        while self._running:
            if cap is None:
                self.status_changed.emit("connecting")
                cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter.fourcc(*"MJPG"))

                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.status_changed.emit("error")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 16.0)
                    continue

                self.status_changed.emit("connected")
                reconnect_delay = 1.0
                empty_streak = 0

            t0 = time.perf_counter()
            ok, frame = cap.read()
            t1 = time.perf_counter()

            if not ok or frame is None:
                empty_streak += 1
                if empty_streak >= 45:
                    cap.release()
                    cap = None
                    self.status_changed.emit("reconnecting")
                continue

            empty_streak = 0
            self._latest_frame = frame
            self._capture_latency = (t1 - t0) * 1000.0
            self._frame_event.set()  # wake the consumer

        if cap:
            cap.release()

    # ── Consumer loop (QThread) ───────────────────────────────────────────

    @pyqtSlot()
    def run(self) -> None:
        self._running = True

        cam                       = None
        cam_dims: tuple[int, int] = (0, 0)
        frame_times: deque[float] = deque(maxlen=60)
        frame_count               = 0

        cap_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="pristinecam-cap"
        )
        cap_thread.start()

        try:
            while self._running:
                # ── Block until the capture thread has a new frame ─────
                # Timeout ensures we can still check self._running.
                if not self._frame_event.wait(timeout=0.1):
                    continue  # timeout — no new frame, re-check _running
                self._frame_event.clear()

                if not self._running:
                    break

                t_loop = time.perf_counter()
                frame  = self._latest_frame
                if frame is None:
                    continue

                # ── Stats ─────────────────────────────────────────────
                frame_times.append(t_loop)
                if len(frame_times) >= 2:
                    span = frame_times[-1] - frame_times[0]
                    if span > 0:
                        self.fps_updated.emit(
                            (len(frame_times) - 1) / span
                        )
                self.latency_updated.emit(self._capture_latency)
                h, w = frame.shape[:2]
                self.resolution_updated.emit(w, h)

                # ── Transforms ────────────────────────────────────────
                out = self._apply_transforms(frame)

                # ── Virtual camera (always on) ────────────────────────
                if HAS_VCAM:
                    oh, ow = out.shape[:2]
                    if cam is None or cam_dims != (ow, oh):
                        _vcam_close(cam)
                        cam = None
                        try:
                            kw: dict = {
                                "width": ow, "height": oh,
                                "fps": TARGET_FPS,
                                "fmt": PixelFormat.BGR,
                            }
                            if self._backend:
                                kw["backend"] = self._backend
                            cam = pyvirtualcam.Camera(**kw)
                            cam_dims = (ow, oh)
                        except Exception as exc:
                            self.error_occurred.emit(f"Virtual cam: {exc}")
                            cam = None

                    if cam:
                        try:
                            cam.send(out)
                        except Exception:
                            _vcam_close(cam)
                            cam = None
                            cam_dims = (0, 0)

                # ── Preview (10 FPS, downscaled, skippable) ───────────
                frame_count += 1
                if self.show_preview and frame_count % PREVIEW_EVERY_N == 0:
                    small = cv2.resize(
                        out, (PREVIEW_W, PREVIEW_H),
                        interpolation=cv2.INTER_NEAREST,
                    )
                    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    qimg = QImage(
                        rgb.data,
                        PREVIEW_W, PREVIEW_H,
                        rgb.strides[0],
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self.frame_ready.emit(qimg)

                # ── Pace to 30 FPS ────────────────────────────────────
                elapsed = time.perf_counter() - t_loop
                sleep_for = FRAME_INTERVAL - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)

        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self._running = False
            self._frame_event.set()  # unblock capture if it's setting
            cap_thread.join(timeout=3.0)
            _vcam_close(cam)
            self.status_changed.emit("disconnected")

    # ── Transform helper ──────────────────────────────────────────────────

    def _apply_transforms(self, frame: np.ndarray) -> np.ndarray:
        if self.mirror_h:
            frame = cv2.flip(frame, 1)
        if self.mirror_v:
            frame = cv2.flip(frame, 0)
        r = self.rotation
        if r == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif r == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif r == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame


def _vcam_close(cam) -> None:
    if cam is not None:
        try:
            cam.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Custom widgets
# ─────────────────────────────────────────────────────────────────────────────

class PreviewWidget(QWidget):
    """
    Aspect-ratio-preserving live frame viewer.
    Shows a 'NO SIGNAL' placeholder when no stream is active.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumSize(320, 240)
        self._px: Optional[QPixmap] = None

    def set_frame(self, img: QImage) -> None:
        self._px = QPixmap.fromImage(img)
        self.update()

    def clear_frame(self) -> None:
        self._px = None
        self.update()

    def paintEvent(self, _):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing |
            QPainter.RenderHint.SmoothPixmapTransform,
        )
        p.fillRect(self.rect(), QColor("#0c0d10"))

        if self._px and not self._px.isNull():
            scaled = self._px.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width()  - scaled.width())  // 2
            y = (self.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            self._draw_placeholder(p)

        p.end()

    def _draw_placeholder(self, p: QPainter) -> None:
        cx, cy = self.width() // 2, self.height() // 2

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#17191f"))
        p.drawRoundedRect(cx - 36, cy - 26, 72, 52, 8, 8)

        p.setBrush(QColor("#1e2028"))
        p.drawEllipse(cx - 14, cy - 14, 28, 28)
        p.setBrush(QColor("#17191f"))
        p.drawEllipse(cx - 9, cy - 9, 18, 18)

        f1 = QFont()
        f1.setPointSize(11)
        f1.setWeight(QFont.Weight.DemiBold)
        p.setFont(f1)
        p.setPen(QColor("#3a3f4d"))
        p.drawText(
            self.rect().adjusted(0, cy // 2 + 14, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "NO SIGNAL",
        )

        f2 = QFont()
        f2.setPointSize(9)
        p.setFont(f2)
        p.setPen(QColor("#272b36"))
        p.drawText(
            self.rect().adjusted(0, cy // 2 + 36, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Open PristineCam on your phone, then tap Connect",
        )


class _Div(QFrame):
    """Thin horizontal divider line."""

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFixedHeight(1)
        self.setStyleSheet("background:#22252e; border:none;")


class _SectionLabel(QLabel):
    """Small all-caps section heading."""

    def __init__(self, text: str) -> None:
        super().__init__(text.upper())
        self.setStyleSheet(
            "color:#444b5c; font-size:10px; font-weight:700;"
            "letter-spacing:1.3px; padding:4px 0 2px 0;"
        )


class _StatRow(QWidget):
    """Label → value display row used in the status panel."""

    def __init__(self, key: str, value: str = "—") -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(6)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFixedWidth(82)
        self._key_lbl.setStyleSheet("color:#444b5c; font-size:12px;")

        self._val_lbl = QLabel(value)
        self._val_lbl.setStyleSheet("color:#a8adb8; font-size:12px;")

        lay.addWidget(self._key_lbl)
        lay.addWidget(self._val_lbl)
        lay.addStretch()

    def set_text(self, text: str, color: str = "#a8adb8") -> None:
        self._val_lbl.setText(text)
        self._val_lbl.setStyleSheet(
            f"color:{color}; font-size:12px; font-weight:500;"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self._cfg      = _load_cfg()
        self._worker:  Optional[StreamWorker] = None
        self._thread:  Optional[QThread]      = None
        self._connected = False

        self._build_ui()
        self._restore_settings()
        self.setStyleSheet(_QSS)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setWindowTitle("PristineCam")
        self.setMinimumSize(900, 600)
        self.resize(1280, 720)

        root = QWidget()
        self.setCentralWidget(root)
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ── Left: preview ────────────────────────────────────────────────
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(20, 18, 12, 18)
        left_lay.setSpacing(0)

        # App header
        hdr = QWidget()
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(0, 0, 0, 14)
        hdr_lay.setSpacing(8)
        _dot = QLabel("●")
        _dot.setStyleSheet("color:#12b886; font-size:9px; padding-top:3px;")
        _name = QLabel("PristineCam")
        _name.setStyleSheet(
            "font-size:17px; font-weight:700; color:#dde0e8; letter-spacing:-0.4px;"
        )
        hdr_lay.addWidget(_dot)
        hdr_lay.addWidget(_name)
        hdr_lay.addStretch()
        left_lay.addWidget(hdr)

        self._preview = PreviewWidget()
        left_lay.addWidget(self._preview)

        # ── Right: controls ──────────────────────────────────────────────
        right = QWidget()
        right.setObjectName("CtrlPanel")
        right.setFixedWidth(284)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(14, 18, 16, 14)
        right_lay.setSpacing(12)

        # ··· Connection ·················································
        right_lay.addWidget(_SectionLabel("Connection"))

        self._ip = QLineEdit()
        self._ip.setPlaceholderText("192.168.1.100")
        self._ip.setToolTip("Local IP address of the Android device on Wi-Fi")
        self._mkrow(right_lay, "Phone IP", self._ip)

        self._port = QLineEdit()
        self._port.setPlaceholderText("8080")
        self._port.setMaximumWidth(72)
        self._mkrow(right_lay, "Port", self._port)

        self._usb = QCheckBox("USB Mode  (ADB port-forward)")
        self._usb.setToolTip(
            "Automatically runs:\n"
            "    adb forward tcp:8080 tcp:8080\n\n"
            "Requirements:\n"
            "  • USB cable between phone and PC\n"
            "  • ADB (Android platform-tools) installed and in PATH\n\n"
            "Benefit: lower latency than Wi-Fi; no network setup needed.\n"
            "The IP field is ignored in this mode."
        )
        self._usb.toggled.connect(self._on_usb_toggled)
        right_lay.addWidget(self._usb)

        self._conn_btn = QPushButton("▶   Connect")
        self._conn_btn.setObjectName("ConnBtn")
        self._conn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._conn_btn.clicked.connect(self._toggle_connection)
        right_lay.addWidget(self._conn_btn)

        right_lay.addWidget(_Div())

        # ··· Status ·····················································
        right_lay.addWidget(_SectionLabel("Status"))
        self._s_state = _StatRow("State",      "Disconnected")
        self._s_fps   = _StatRow("FPS",        "—")
        self._s_lat   = _StatRow("Latency",    "—")
        self._s_res   = _StatRow("Resolution", "—")
        for w in (self._s_state, self._s_fps, self._s_lat, self._s_res):
            right_lay.addWidget(w)


        right_lay.addWidget(_Div())

        # ··· Transform ··················································
        right_lay.addWidget(_SectionLabel("Transform"))

        mir = QWidget()
        mir_lay = QHBoxLayout(mir)
        mir_lay.setContentsMargins(0, 0, 0, 0)
        mir_lay.setSpacing(6)
        self._flip_h = QPushButton("↔  Mirror H")
        self._flip_h.setCheckable(True)
        self._flip_h.setToolTip("Flip frame horizontally (mirror mode)")
        self._flip_h.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flip_h.toggled.connect(self._on_flip_h)
        self._flip_v = QPushButton("↕  Mirror V")
        self._flip_v.setCheckable(True)
        self._flip_v.setToolTip("Flip frame vertically")
        self._flip_v.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flip_v.toggled.connect(self._on_flip_v)
        mir_lay.addWidget(self._flip_h)
        mir_lay.addWidget(self._flip_v)
        right_lay.addWidget(mir)

        self._rot = QComboBox()
        self._rot.addItems([
            "0°  (no rotation)",
            "90°  (clockwise)",
            "180°",
            "270°  (counter-clockwise)",
        ])
        self._rot.currentIndexChanged.connect(self._on_rotation)
        self._mkrow(right_lay, "Rotate", self._rot)

        right_lay.addWidget(_Div())

        # ··· Display & Output ·············································
        right_lay.addWidget(_SectionLabel("Output"))

        self._preview_chk = QCheckBox("Show Preview")
        self._preview_chk.setChecked(True)
        self._preview_chk.setToolTip(
            "Toggle the live preview in the main panel.\n"
            "Disabling saves ~5-10% CPU by skipping resize and render."
        )
        self._preview_chk.toggled.connect(self._on_preview_toggled)
        right_lay.addWidget(self._preview_chk)

        self._backend = QComboBox()
        for label in _BACKENDS:
            self._backend.addItem(label)
        self._backend.setToolTip(
            "Auto-detect: pyvirtualcam picks the available driver.\n"
            "OBS: requires OBS Studio (Windows / macOS).\n"
            "v4l2loopback: Linux kernel module.\n"
            "Unity Capture: Windows-only alternative driver."
        )
        self._mkrow(right_lay, "Backend", self._backend)

        if not HAS_VCAM:
            vcam_warn = QLabel("⚠  pyvirtualcam not installed")
            vcam_warn.setStyleSheet("color:#fa5252; font-size:11px; padding:2px 0;")
            vcam_warn.setToolTip(
                "pip install pyvirtualcam\n\n"
                "Also install the driver for your OS:\n"
                "  Windows/macOS → OBS Studio (or Unity Capture)\n"
                "  Linux → sudo modprobe v4l2loopback"
            )
            right_lay.addWidget(vcam_warn)

        right_lay.addStretch()

        footer = QLabel("No telemetry  ·  Local only  ·  Open source")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("color:#262a36; font-size:10px; padding-top:4px;")
        right_lay.addWidget(footer)

        root_lay.addWidget(left, stretch=1)
        root_lay.addWidget(right)

        # Status bar
        sb = QStatusBar()
        sb.setStyleSheet("background:#10111a; color:#444b5c; font-size:11px;")
        self.setStatusBar(sb)

    # ── Helper: labelled row ─────────────────────────────────────────────

    @staticmethod
    def _mkrow(parent: QVBoxLayout, label: str, widget: QWidget) -> None:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lbl = QLabel(label)
        lbl.setFixedWidth(70)
        lbl.setStyleSheet("color:#444b5c; font-size:12px;")
        lay.addWidget(lbl)
        lay.addWidget(widget)
        parent.addWidget(row)

    # ── Settings persistence ─────────────────────────────────────────────

    def _restore_settings(self) -> None:
        c = self._cfg
        self._ip.setText(c.get("ip", "192.168.1.100"))
        self._port.setText(str(c.get("port", 8080)))
        self._usb.setChecked(c.get("usb_mode", False))
        self._flip_h.setChecked(c.get("mirror_h", False))
        self._flip_v.setChecked(c.get("mirror_v", False))
        self._rot.setCurrentIndex(c.get("rotation_idx", 0))
        self._backend.setCurrentIndex(c.get("backend_idx", 0))
        self._preview_chk.setChecked(c.get("show_preview", True))

    def _persist_settings(self) -> None:
        self._cfg.update({
            "ip":           self._ip.text().strip(),
            "port":         self._port.text().strip(),
            "usb_mode":     self._usb.isChecked(),
            "mirror_h":     self._flip_h.isChecked(),
            "mirror_v":     self._flip_v.isChecked(),
            "rotation_idx": self._rot.currentIndex(),
            "backend_idx":  self._backend.currentIndex(),
            "show_preview": self._preview_chk.isChecked(),
        })
        _save_cfg(self._cfg)

    # ── Connection control ────────────────────────────────────────────────

    def _stream_url(self) -> str:
        ip   = "127.0.0.1" if self._usb.isChecked() else (
               self._ip.text().strip() or "192.168.1.100")
        port = self._port.text().strip() or "8080"
        return f"http://{ip}:{port}/video_feed"

    def _toggle_connection(self) -> None:
        if self._connected:
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        port = int(self._port.text().strip() or "8080")

        if self._usb.isChecked():
            ok, msg = _adb_forward(port)
            if not ok:
                QMessageBox.critical(self, "ADB Error", msg)
                return
            self.statusBar().showMessage(f"✔  {msg}", 4000)

        backend_key = self._backend.currentText()
        backend_val = _BACKENDS.get(backend_key)

        self._worker = StreamWorker(
            url=self._stream_url(),
            backend=backend_val,
        )
        self._worker.mirror_h     = self._flip_h.isChecked()
        self._worker.mirror_v     = self._flip_v.isChecked()
        self._worker.rotation     = _ROT_ANGLES[self._rot.currentIndex()]
        self._worker.show_preview = self._preview_chk.isChecked()

        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        self._worker.frame_ready.connect(self._preview.set_frame)
        self._worker.fps_updated.connect(self._on_fps)
        self._worker.latency_updated.connect(self._on_latency)
        self._worker.resolution_updated.connect(self._on_resolution)
        self._worker.status_changed.connect(self._on_status)
        self._worker.error_occurred.connect(self._on_error)

        self._thread.start()
        self._connected = True
        self._set_ui_connected(True)

    def _do_disconnect(self) -> None:
        self._connected = False
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self._worker = None
        self._thread = None
        self._preview.clear_frame()
        self._set_ui_connected(False)
        self._s_state.set_text("Disconnected", "#5c6370")
        for row in (self._s_fps, self._s_lat, self._s_res):
            row.set_text("—")

    # ── Worker slots ──────────────────────────────────────────────────────

    @pyqtSlot(float)
    def _on_fps(self, v: float) -> None:
        self._s_fps.set_text(f"{v:.1f} fps")

    @pyqtSlot(float)
    def _on_latency(self, v: float) -> None:
        color = "#40c057" if v < 80 else "#fcc419" if v < 250 else "#fa5252"
        self._s_lat.set_text(f"{v:.0f} ms", color)

    @pyqtSlot(int, int)
    def _on_resolution(self, w: int, h: int) -> None:
        self._s_res.set_text(f"{w} × {h}")

    @pyqtSlot(str)
    def _on_status(self, state: str) -> None:
        label, color = _STATUS_STYLE.get(state, ("Unknown", "#5c6370"))
        self._s_state.set_text(f"● {label}", color)

        if state == "disconnected" and self._connected:
            self._connected = False
            if self._thread:
                self._thread.quit()
                self._thread = None
            self._worker = None
            self._preview.clear_frame()
            self._set_ui_connected(False)
            for row in (self._s_fps, self._s_lat, self._s_res):
                row.set_text("—")

    @pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        self.statusBar().showMessage(f"⚠  {msg}", 6000)

    # ── Real-time control slots ───────────────────────────────────────────

    def _on_flip_h(self, v: bool) -> None:
        if self._worker:
            self._worker.mirror_h = v

    def _on_flip_v(self, v: bool) -> None:
        if self._worker:
            self._worker.mirror_v = v

    def _on_rotation(self, idx: int) -> None:
        if self._worker:
            self._worker.rotation = _ROT_ANGLES[idx]

    def _on_usb_toggled(self, checked: bool) -> None:
        self._ip.setEnabled(not checked)
        self._ip.setPlaceholderText(
            "127.0.0.1  (USB / ADB)" if checked else "192.168.1.100"
        )

    def _on_preview_toggled(self, checked: bool) -> None:
        if self._worker:
            self._worker.show_preview = checked
        if not checked:
            self._preview.clear_frame()

    # ── UI state helper ───────────────────────────────────────────────────

    def _set_ui_connected(self, connected: bool) -> None:
        if connected:
            self._conn_btn.setText("⏹   Disconnect")
            self._conn_btn.setStyleSheet(
                "QPushButton#ConnBtn {"
                "  background:#c92a2a; border:none; color:#fff;"
                "  font-size:13px; font-weight:600; padding:10px; border-radius:6px;"
                "}"
                "QPushButton#ConnBtn:hover { background:#a61e1e; }"
                "QPushButton#ConnBtn:pressed { background:#8e1818; }"
            )
        else:
            self._conn_btn.setText("▶   Connect")
            self._conn_btn.setStyleSheet("")

        for w in (self._ip, self._port, self._usb, self._backend):
            w.setEnabled(not connected)

        if not connected and self._usb.isChecked():
            self._ip.setEnabled(False)

    # ── Window close ─────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._persist_settings()
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# Global stylesheet  (dark, teal accent, matches the Android app palette)
# ─────────────────────────────────────────────────────────────────────────────

_QSS = """
/* ─ Base ─────────────────────────────────────────────────────────── */
QMainWindow, QWidget {
    background: #14161c;
    color: #b8bcc8;
    font-family: "Segoe UI", "Inter", "SF Pro Text", Helvetica, Arial, sans-serif;
    font-size: 13px;
}

/* ─ Controls panel ───────────────────────────────────────────────── */
#CtrlPanel {
    background: #191b22;
    border-left: 1px solid #20232c;
}

/* ─ Inputs ───────────────────────────────────────────────────────── */
QLineEdit {
    background: #1e2028;
    border: 1px solid #2a2e3c;
    border-radius: 5px;
    padding: 5px 9px;
    color: #dde0e8;
    selection-background-color: #12b886;
}
QLineEdit:focus { border-color: #12b886; }
QLineEdit:disabled { color: #343848; border-color: #1e2028; background: #181a20; }

/* ─ Combo boxes ──────────────────────────────────────────────────── */
QComboBox {
    background: #1e2028;
    border: 1px solid #2a2e3c;
    border-radius: 5px;
    padding: 5px 9px;
    color: #b8bcc8;
}
QComboBox:hover { border-color: #3a3f52; }
QComboBox:disabled { color: #343848; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow {
    width: 0; height: 0;
    border-left:  4px solid transparent;
    border-right: 4px solid transparent;
    border-top:   5px solid #5c6370;
    margin-right: 5px;
}
QComboBox QAbstractItemView {
    background: #1e2028;
    border: 1px solid #2a2e3c;
    selection-background-color: #12b886;
    selection-color: #fff;
    color: #b8bcc8;
    outline: none;
    padding: 2px;
}

/* ─ Checkboxes ───────────────────────────────────────────────────── */
QCheckBox { color: #b8bcc8; spacing: 7px; font-size: 12px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 3px;
    border: 1.5px solid #3a3f52;
    background: #1e2028;
}
QCheckBox::indicator:checked { background: #12b886; border-color: #12b886; }
QCheckBox::indicator:disabled { background: #181a20; border-color: #252932; }
QCheckBox:disabled { color: #343848; }

/* ─ Buttons (default) ────────────────────────────────────────────── */
QPushButton {
    background: #1e2028;
    border: 1px solid #2a2e3c;
    border-radius: 6px;
    padding: 7px 12px;
    color: #b8bcc8;
    font-size: 12px;
}
QPushButton:hover  { background: #252932; border-color: #3a3f52; }
QPushButton:pressed { background: #191b22; }
QPushButton:checked {
    background: #0b7a59;
    border-color: #0b7a59;
    color: #d0fff3;
}
QPushButton:checked:hover { background: #0a6d4f; }
QPushButton:disabled { color: #2e3244; border-color: #1e2028; background: #181a20; }

/* ─ Connect button ───────────────────────────────────────────────── */
QPushButton#ConnBtn {
    background: #12b886;
    border: none;
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    padding: 10px;
    border-radius: 6px;
}
QPushButton#ConnBtn:hover   { background: #0ca678; }
QPushButton#ConnBtn:pressed { background: #09926a; }
QPushButton#ConnBtn:disabled { background: #1e2028; color: #343848; }

/* ─ Scrollbars ───────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: #14161c; width: 8px; border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #2a2e3c; border-radius: 4px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

/* ─ Tool tips ────────────────────────────────────────────────────── */
QToolTip {
    background: #1e2028;
    border: 1px solid #3a3f52;
    color: #b8bcc8;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
}

/* ─ Status bar ───────────────────────────────────────────────────── */
QStatusBar { background: #10111a; color: #444b5c; font-size: 11px; border: none; }
QStatusBar::item { border: none; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("PristineCam")
    app.setOrganizationName("PristineCam")

    font = QFont("Segoe UI", 10)
    app.setFont(font)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
