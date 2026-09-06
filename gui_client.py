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
    from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
    from PyQt6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
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

try:
    from zeroconf_discovery import DiscoveryWorker, is_available as _zc_available
    HAS_ZEROCONF = _zc_available()
except ImportError:
    HAS_ZEROCONF = False


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TARGET_FPS         = 30
FRAME_INTERVAL     = 1.0 / TARGET_FPS          # ~33.33 ms
PREVIEW_FPS        = 30
PREVIEW_EVERY_N    = TARGET_FPS // PREVIEW_FPS  # emit preview every 3rd frame
PREVIEW_W, PREVIEW_H = 480, 270                # preview downscale resolution

TIMEOUT_THREAD_JOIN = 3000       # ms
TIMEOUT_CAP_JOIN    = 3.0        # seconds
MAX_EMPTY_STREAK    = 45         # frames
FPS_WINDOW_SIZE     = 60         # frames for moving average
LATENCY_GOOD_MS     = 80
LATENCY_WARN_MS     = 250

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & Paths
# ─────────────────────────────────────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    # Running as compiled PyInstaller executable — save settings alongside the .exe
    _base_dir = Path(sys.executable).parent
else:
    # Running as a script
    _base_dir = Path(__file__).parent

def resource_path(relative_path: str) -> Path:
    """Get absolute path to bundled resource (works in dev and PyInstaller sys._MEIPASS)."""
    try:
        base_path = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    except AttributeError:
        base_path = Path(__file__).parent
    return base_path / relative_path

CONFIG_FILE = _base_dir / "pristinecam_settings.json"
_DEFAULTS: dict = {
    "ip": "192.168.1.100",
    "port": 8080,
    "usb_mode": False,
    "mirror_h": False,
    "mirror_v": False,
    "rotation_idx": 0,
    "backend_idx": 0,
    "show_preview": True,
    "preview_fps": 30,
    "preview_res": "480x270",
    "show_stats": False,
}

def _load_cfg() -> dict:
    cfg = _DEFAULTS.copy()
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text())
            for k, v in _DEFAULTS.items():
                if k in loaded and isinstance(loaded[k], type(v)):
                    cfg[k] = loaded[k]
        except Exception:
            pass
    return cfg


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

def _adb_remove_forward(port: int) -> None:
    """Run `adb forward --remove tcp:<port>` to cleanup."""
    try:
        subprocess.run(
            ["adb", "forward", "--remove", f"tcp:{port}"],
            capture_output=True, timeout=2,
        )
    except Exception:
        pass


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
    "connecting":   ("Connecting…",    "#eab308"),
    "connected":    ("Connected",      "#10b981"),
    "reconnecting": ("Reconnecting…",  "#f97316"),
    "error":        ("Connection error", "#ef4444"),
    "disconnected": ("Disconnected",   "#71717a"),
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
        self.preview_fps: int   = 10
        self.preview_w: int     = 480
        self.preview_h: int     = 270
        
        self._backend: Optional[str] = backend
        self._force_vcam_recreate: bool = False

    def set_backend(self, backend: Optional[str]) -> None:
        if self._backend != backend:
            self._backend = backend
            self._force_vcam_recreate = True

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
                if empty_streak >= MAX_EMPTY_STREAK:
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
        frame_times: deque[float] = deque(maxlen=FPS_WINDOW_SIZE)
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
                    if cam is None or cam_dims != (ow, oh) or self._force_vcam_recreate:
                        self._force_vcam_recreate = False
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
                            if self._backend:
                                self.error_occurred.emit(f"VCam backend '{self._backend}' failed: {exc}. Falling back...")
                                try:
                                    del kw["backend"]
                                    cam = pyvirtualcam.Camera(**kw)
                                    cam_dims = (ow, oh)
                                except Exception as exc2:
                                    self.error_occurred.emit(f"Virtual cam failed: {exc2}")
                                    cam = None
                            else:
                                self.error_occurred.emit(f"Virtual cam failed: {exc}")
                                cam = None

                    if cam:
                        try:
                            cam.send(out)
                        except Exception:
                            _vcam_close(cam)
                            cam = None
                            cam_dims = (0, 0)

                # ── Preview (dynamic FPS, dynamic res) ───────────
                frame_count += 1
                if self.show_preview:
                    preview_every_n = max(1, TARGET_FPS // self.preview_fps) if self.preview_fps > 0 else 1
                    if frame_count % preview_every_n == 0:
                        if self.preview_w > 0 and self.preview_h > 0:
                            small = cv2.resize(
                                out, (self.preview_w, self.preview_h),
                                interpolation=cv2.INTER_NEAREST,
                            )
                        else:
                            small = out
                            
                        ph, pw = small.shape[:2]
                        qimg = QImage(
                            small.data,
                            pw, ph,
                            small.strides[0],
                            QImage.Format.Format_BGR888,
                        ).copy()
                        self.frame_ready.emit(qimg)

                # ── Pace to 30 FPS ────────────────────────────────────
                if cam is not None and hasattr(cam, "sleep_until_next_frame"):
                    try:
                        cam.sleep_until_next_frame()
                    except Exception:
                        pass
                else:
                    elapsed = time.perf_counter() - t_loop
                    sleep_for = FRAME_INTERVAL - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)

        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self._running = False
            self._frame_event.set()  # unblock capture if it's setting
            cap_thread.join(timeout=TIMEOUT_CAP_JOIN)
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
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#09090b"))
        p.drawRect(self.rect())
        
        f1 = QFont()
        f1.setPointSize(14)
        f1.setWeight(QFont.Weight.DemiBold)
        p.setFont(f1)
        p.setPen(QColor("#71717a"))
        p.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "NO SIGNAL"
        )
        
        f2 = QFont()
        f2.setPointSize(10)
        p.setFont(f2)
        p.setPen(QColor("#52525b"))
        p.drawText(
            self.rect().adjusted(0, 40, 0, 0),
            Qt.AlignmentFlag.AlignCenter,
            "Open PristineCam on your phone, then tap Connect"
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
            "color:#71717a; font-size:11px; font-weight:700;"
            "letter-spacing:1px; background:transparent; padding-top:8px;"
        )


class _StatRow(QWidget):
    """Label → value display row used in the status panel."""

    def __init__(self, key: str, value: str = "—") -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(12)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFixedWidth(82)
        self._key_lbl.setStyleSheet("color:#a1a1aa; font-size:12px; background:transparent;")

        self._val_lbl = QLabel(value)
        self._val_lbl.setStyleSheet("color:#f4f4f5; font-size:12px; font-family:'Consolas', 'JetBrains Mono', monospace; font-weight:500; background:transparent;")

        lay.addWidget(self._key_lbl)
        lay.addWidget(self._val_lbl)
        lay.addStretch()

    def set_text(self, text: str, color: str = "#f4f4f5") -> None:
        self._val_lbl.setText(text)
        self._val_lbl.setStyleSheet(
            f"color:{color}; font-size:12px; font-family:'Consolas', 'JetBrains Mono', monospace; font-weight:500; background:transparent;"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Settings Dialog
# ─────────────────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(380)
        
        lay = QVBoxLayout(self)
        
        form = QFormLayout()
        
        # Preview FPS
        self.fps_cb = QComboBox()
        self.fps_cb.addItems(["5 FPS", "10 FPS", "15 FPS", "30 FPS (Match stream)"])
        self.fps_cb.setToolTip("Lower FPS reduces CPU usage.")
        form.addRow("Preview FPS:", self.fps_cb)
        
        # Preview Resolution
        self.res_cb = QComboBox()
        self.res_cb.addItems(["480×270", "640×360", "960×540", "Native / Source"])
        self.res_cb.setToolTip("Lower resolution increases performance.")
        form.addRow("Preview Res:", self.res_cb)
        
        # Virtual Camera Backend
        self.backend_cb = QComboBox()
        for label in _BACKENDS:
            self.backend_cb.addItem(label)
        self.backend_cb.setToolTip("Select virtual camera backend driver.")
        form.addRow("VCam Backend:", self.backend_cb)
        
        # Show Stats
        self.stats_chk = QCheckBox("Show Performance Stats")
        self.stats_chk.setToolTip("Toggle visibility of FPS, Latency, and Resolution.")
        form.addRow("", self.stats_chk)

        lay.addLayout(form)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)
        lay.addWidget(bbox)

        # Load values
        fps = cfg.get("preview_fps", 30)
        idx = {5: 0, 10: 1, 15: 2, 30: 3}.get(fps, 3)
        self.fps_cb.setCurrentIndex(idx)

        res = cfg.get("preview_res", "480x270")
        idx = {"480x270": 0, "640x360": 1, "960x540": 2}.get(res, 3)
        self.res_cb.setCurrentIndex(idx)

        self.backend_cb.setCurrentIndex(cfg.get("backend_idx", 0))
        self.stats_chk.setChecked(cfg.get("show_stats", False))

    def accept(self):
        fps_val = [5, 10, 15, 30][self.fps_cb.currentIndex()]
        self.cfg["preview_fps"] = fps_val

        res_val = ["480x270", "640x360", "960x540", "Native"][self.res_cb.currentIndex()]
        self.cfg["preview_res"] = res_val

        self.cfg["backend_idx"] = self.backend_cb.currentIndex()
        self.cfg["show_stats"] = self.stats_chk.isChecked()
        super().accept()



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

        self._discovered: dict[str, tuple[str, int]] = {}  # name -> (host, port)
        self._discovery: Optional[DiscoveryWorker] = None
        self._build_ui()
        self._restore_settings()
        self.setStyleSheet(_QSS)
        
        icon_path = resource_path("assets/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._start_discovery()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setWindowTitle("PristineCam")
        self.setMinimumSize(945, 630)
        self.resize(1344, 756)

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
        right.setFixedWidth(280)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(20, 24, 20, 24)
        right_lay.setSpacing(12)

        # ··· Connection ·················································
        right_lay.addWidget(_SectionLabel("Connection"))

        ip_port_lay = QHBoxLayout()
        ip_port_lay.setContentsMargins(0, 0, 0, 0)
        ip_port_lay.setSpacing(8)
        self._ip = QLineEdit()
        self._ip.setPlaceholderText("192.168.1.100")
        self._ip.setToolTip("Local IP address of the Android device on Wi-Fi")
        ip_port_lay.addWidget(self._ip, stretch=3)
        self._port = QLineEdit()
        self._port.setPlaceholderText("8080")
        self._port.setMaximumWidth(60)
        ip_port_lay.addWidget(self._port, stretch=1)
        right_lay.addLayout(ip_port_lay)
        # Auto-discovery row — only built when zeroconf is available
        if HAS_ZEROCONF:
            self._discover_cb = QComboBox()
            self._discover_cb.setPlaceholderText("Found devices will appear here")
            self._discover_cb.setToolTip("PristineCam devices found on your network — select one to connect instantly")
            self._discover_cb.currentIndexChanged.connect(self._on_discover_selected)
            self._discover_cb.setVisible(False)   # hidden until devices appear
            right_lay.addWidget(self._discover_cb)

            self._scan_btn = QPushButton("Scan for devices")
            self._scan_btn.setToolTip("Restart mDNS scan for PristineCam devices on the local network")
            self._scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._scan_btn.clicked.connect(self._rescan_devices)
            right_lay.addWidget(self._scan_btn)

        self._usb = QCheckBox("USB Mode (ADB port-forward)")
        self._usb.setToolTip("Use ADB over USB instead of Wi-Fi for lower latency.")
        self._usb.toggled.connect(self._on_usb_toggled)
        right_lay.addWidget(self._usb)

        self._conn_btn = QPushButton("Connect")
        self._conn_btn.setObjectName("ConnBtn")
        self._conn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._conn_btn.clicked.connect(self._toggle_connection)
        right_lay.addWidget(self._conn_btn)
        
        right_lay.addSpacing(12)

        # ··· Status ·····················································
        right_lay.addWidget(_SectionLabel("Status"))
        self._s_state = _StatRow("State",      "Disconnected")
        self._s_fps   = _StatRow("FPS",        "—")
        self._s_lat   = _StatRow("Latency",    "—")
        self._s_res   = _StatRow("Resolution", "—")
        for w in (self._s_state, self._s_fps, self._s_lat, self._s_res):
            right_lay.addWidget(w)
            
        right_lay.addSpacing(12)

        # ··· Transform ··················································
        right_lay.addWidget(_SectionLabel("Transform"))
        
        mir_lay = QHBoxLayout()
        mir_lay.setContentsMargins(0, 0, 0, 0)
        mir_lay.setSpacing(8)
        self._flip_h = QPushButton("Mirror H")
        self._flip_h.setCheckable(True)
        self._flip_h.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flip_h.toggled.connect(self._on_flip_h)
        self._flip_v = QPushButton("Mirror V")
        self._flip_v.setCheckable(True)
        self._flip_v.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flip_v.toggled.connect(self._on_flip_v)
        mir_lay.addWidget(self._flip_h)
        mir_lay.addWidget(self._flip_v)
        right_lay.addLayout(mir_lay)

        rot_lay = QHBoxLayout()
        rot_lay.setContentsMargins(0, 0, 0, 0)
        rot_lay.setSpacing(8)
        rot_lbl = QLabel("Rotate")
        rot_lbl.setStyleSheet("color:#a1a1aa; font-size:13px;")
        rot_lay.addWidget(rot_lbl)
        self._rot = QComboBox()
        self._rot.addItems(["0°", "90°", "180°", "270°"])
        self._rot.currentIndexChanged.connect(self._on_rotation)
        rot_lay.addWidget(self._rot, stretch=1)
        right_lay.addLayout(rot_lay)

        right_lay.addSpacing(12)

        # ··· Display & Output ·············································
        right_lay.addWidget(_SectionLabel("Output"))
        
        self._preview_chk = QCheckBox("Show Preview")
        self._preview_chk.setChecked(True)
        self._preview_chk.toggled.connect(self._on_preview_toggled)
        right_lay.addWidget(self._preview_chk)

        self._settings_btn = QPushButton("Settings")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.clicked.connect(self._open_settings)
        right_lay.addWidget(self._settings_btn)

        if not HAS_VCAM:
            vcam_warn = QLabel("⚠ pyvirtualcam not installed")
            vcam_warn.setStyleSheet("color:#ef4444; font-size:12px; padding:2px 0;")
            right_lay.addWidget(vcam_warn)

        right_lay.addStretch()

        footer = QLabel("No telemetry · Local only · Open source")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("color:#52525b; font-size:11px; padding-top:4px;")
        right_lay.addWidget(footer)

        root_lay.addWidget(left, stretch=1)
        root_lay.addWidget(right)

        # Status bar
        sb = QStatusBar()
        self.setStatusBar(sb)

    # ── Settings persistence ─────────────────────────────────────────────

    def _restore_settings(self) -> None:
        c = self._cfg
        self._ip.setText(c.get("ip", "192.168.1.100"))
        self._port.setText(str(c.get("port", 8080)))
        self._usb.setChecked(c.get("usb_mode", False))
        self._flip_h.setChecked(c.get("mirror_h", False))
        self._flip_v.setChecked(c.get("mirror_v", False))
        self._rot.setCurrentIndex(c.get("rotation_idx", 0))
        self._preview_chk.setChecked(c.get("show_preview", True))
        self._apply_stats_visibility()

    def _persist_settings(self) -> None:
        try:
            port_val = int(self._port.text().strip() or "8080")
        except ValueError:
            port_val = 8080

        self._cfg.update({
            "ip":           self._ip.text().strip(),
            "port":         port_val,
            "usb_mode":     self._usb.isChecked(),
            "mirror_h":     self._flip_h.isChecked(),
            "mirror_v":     self._flip_v.isChecked(),
            "rotation_idx": self._rot.currentIndex(),
            "show_preview": self._preview_chk.isChecked(),
        })
        _save_cfg(self._cfg)

    # ── mDNS Discovery ───────────────────────────────────────────────

    def _start_discovery(self) -> None:
        if not HAS_ZEROCONF:
            return
        try:
            self._discovery = DiscoveryWorker(parent=self)
            # Qt signals: delivered on main thread automatically, no QTimer needed
            self._discovery.device_found.connect(self._on_device_discovered)
            self._discovery.device_lost.connect(self._on_device_lost)
            self._discovery.start()
        except Exception as exc:
            self.statusBar().showMessage(f"Discovery unavailable: {exc}", 5000)

    def _rescan_devices(self) -> None:
        """Stop and restart the mDNS scanner (triggered by the Scan button)."""
        if not HAS_ZEROCONF:
            return
        self._discovered.clear()
        self._refresh_device_dropdown()
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("Scanning…")
        if self._discovery:
            self._discovery.stop()
            self._discovery = None
        self._start_discovery()
        # Re-enable the button after 4 s regardless of results
        QTimer.singleShot(4000, self._scan_done)

    def _scan_done(self) -> None:
        if not HAS_ZEROCONF:
            return
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("Scan for devices")

    def _on_device_discovered(self, host: str, port: int, name: str) -> None:
        """Slot — called on main thread via Qt queued connection."""
        self._discovered[name] = (host, port)
        self._refresh_device_dropdown()

    def _on_device_lost(self, name: str) -> None:
        """Slot — called on main thread via Qt queued connection."""
        self._discovered.pop(name, None)
        self._refresh_device_dropdown()

    def _refresh_device_dropdown(self) -> None:
        if not HAS_ZEROCONF:
            return
        cb = self._discover_cb
        cb.blockSignals(True)
        cb.clear()
        if self._discovered:
            for name, (host, port) in self._discovered.items():
                # Show short label — strip the service-type suffix for readability
                short = name.replace("._pristinecam._tcp.local.", "")
                label = f"{short}  ({host}:{port})"
                cb.addItem(label, (host, port))
            cb.setCurrentIndex(-1)   # nothing pre-selected
            cb.setVisible(True)
        else:
            cb.setVisible(False)
        cb.blockSignals(False)

    def _on_discover_selected(self, index: int) -> None:
        if not HAS_ZEROCONF or index < 0:
            return
        data = self._discover_cb.itemData(index)
        if data:
            host, port = data
            self._ip.setText(host)
            self._port.setText(str(port))
            # Reset combo immediately so the user can re-select after disconnect
            self._discover_cb.blockSignals(True)
            self._discover_cb.setCurrentIndex(-1)
            self._discover_cb.blockSignals(False)
            # Auto-connect
            if not self._connected:
                self._do_connect()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._cfg, self)
        if dlg.exec():
            self._persist_settings()
            self._apply_settings_to_worker()
            self._apply_stats_visibility()

    def _apply_settings_to_worker(self) -> None:
        if not self._worker:
            return
            
        fps = self._cfg.get("preview_fps", 30)
        self._worker.preview_fps = fps
        
        res = self._cfg.get("preview_res", "480x270")
        if res == "Native":
            w, h = 0, 0
        else:
            try:
                w, h = map(int, res.split("x"))
            except ValueError:
                w, h = 480, 270
        self._worker.preview_w = w
        self._worker.preview_h = h
        
        b_idx = self._cfg.get("backend_idx", 0)
        b_keys = list(_BACKENDS.keys())
        if b_idx < len(b_keys):
            self._worker.set_backend(_BACKENDS[b_keys[b_idx]])

    def _apply_stats_visibility(self) -> None:
        show = self._cfg.get("show_stats", True)
        self._s_fps.setVisible(show)
        self._s_lat.setVisible(show)
        self._s_res.setVisible(show)

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
        try:
            port = int(self._port.text().strip() or "8080")
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Port must be a valid number.")
            return

        if self._usb.isChecked():
            ok, msg = _adb_forward(port)
            if not ok:
                QMessageBox.critical(self, "ADB Error", msg)
                return
            self.statusBar().showMessage(f"✔  {msg}", 4000)

        b_idx = self._cfg.get("backend_idx", 0)
        b_keys = list(_BACKENDS.keys())
        backend_val = _BACKENDS[b_keys[b_idx]] if b_idx < len(b_keys) else None

        self._worker = StreamWorker(
            url=self._stream_url(),
            backend=backend_val,
        )
        self._worker.mirror_h     = self._flip_h.isChecked()
        self._worker.mirror_v     = self._flip_v.isChecked()
        self._worker.rotation     = _ROT_ANGLES[self._rot.currentIndex()]
        self._worker.show_preview = self._preview_chk.isChecked()
        self._apply_settings_to_worker()

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
            self._thread.wait(TIMEOUT_THREAD_JOIN)
        self._worker = None
        self._thread = None
        if self._usb.isChecked():
            port = int(self._port.text().strip() or "8080")
            _adb_remove_forward(port)
        self._preview.clear_frame()
        self._set_ui_connected(False)
        self._s_state.set_text("Disconnected", "#71717a")
        for row in (self._s_fps, self._s_lat, self._s_res):
            row.set_text("—")

    # ── Worker slots ──────────────────────────────────────────────────────

    @pyqtSlot(float)
    def _on_fps(self, v: float) -> None:
        self._s_fps.set_text(f"{v:.1f} fps")

    @pyqtSlot(float)
    def _on_latency(self, v: float) -> None:
        color = "#10b981" if v < LATENCY_GOOD_MS else "#eab308" if v < LATENCY_WARN_MS else "#ef4444"
        self._s_lat.set_text(f"{v:.0f} ms", color)

    @pyqtSlot(int, int)
    def _on_resolution(self, w: int, h: int) -> None:
        self._s_res.set_text(f"{w} × {h}")

    @pyqtSlot(str)
    def _on_status(self, state: str) -> None:
        label, color = _STATUS_STYLE.get(state, ("Unknown", "#71717a"))
        self._s_state.set_text(f"● {label}", color)

        if state == "disconnected" and self._connected:
            self._do_disconnect()

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
            self._conn_btn.setText("Disconnect")
            self._conn_btn.setStyleSheet(
                "QPushButton#ConnBtn {"
                "  background:#ef4444; border:none; color:#ffffff;"
                "  font-size:14px; font-weight:600; min-height:36px; border-radius:6px;"
                "}"
                "QPushButton#ConnBtn:hover { background:#dc2626; }"
                "QPushButton#ConnBtn:pressed { background:#b91c1c; }"
            )
        else:
            self._conn_btn.setText("Connect")
            self._conn_btn.setStyleSheet("")

        for w in (self._ip, self._port, self._usb, self._settings_btn):
            w.setEnabled(not connected)

        # Discovery controls: disable while connected; restore on disconnect
        if HAS_ZEROCONF:
            self._scan_btn.setEnabled(not connected)
            if connected:
                self._discover_cb.setVisible(False)
            else:
                # Restore combo visibility if we still know about devices
                self._discover_cb.setVisible(bool(self._discovered))

        if not connected and self._usb.isChecked():
            self._ip.setEnabled(False)

    # ── Window close ─────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._persist_settings()
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(TIMEOUT_THREAD_JOIN)
        if self._usb.isChecked():
            port = int(self._port.text().strip() or "8080")
            _adb_remove_forward(port)
        if self._discovery:
            self._discovery.stop()
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# Global stylesheet  (dark, teal accent, matches the Android app palette)
# ─────────────────────────────────────────────────────────────────────────────

_QSS = """
/* ─ Base ─────────────────────────────────────────────────────────── */
QMainWindow, QDialog, QWidget {
    background: #09090b;
    color: #f4f4f5;
    font-family: "Segoe UI", "Inter", "SF Pro Text", Helvetica, Arial, sans-serif;
    font-size: 13px;
}

/* ─ Controls panel ───────────────────────────────────────────────── */
#CtrlPanel {
    background: #121316;
    border-left: 1px solid #27272a;
}

/* ─ Cards / Groups ───────────────────────────────────────────────── */
QFrame#Card {
    background: #18181b;
    border: 1px solid #27272a;
    border-radius: 8px;
}
QLabel {
    color: #a1a1aa;
    background: transparent;
}

/* ─ Inputs ───────────────────────────────────────────────────────── */
QLineEdit {
    background: #27272a;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f4f4f5;
    selection-background-color: #3b82f6;
    min-height: 22px;
}
QLineEdit:focus { border-color: #3b82f6; }
QLineEdit:disabled { color: #71717a; background: #18181b; }

/* ─ Combo boxes ──────────────────────────────────────────────────── */
QComboBox {
    background: #27272a;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 6px 10px;
    color: #f4f4f5;
    min-height: 22px;
}
QComboBox:hover { border-color: #52525b; }
QComboBox:disabled { color: #71717a; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow {
    width: 0; height: 0;
    border-left:  4px solid transparent;
    border-right: 4px solid transparent;
    border-top:   5px solid #a1a1aa;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: #27272a;
    border: 1px solid #3f3f46;
    selection-background-color: #3b82f6;
    selection-color: #fff;
    color: #f4f4f5;
    outline: none;
    padding: 2px;
}

/* ─ Checkboxes ───────────────────────────────────────────────────── */
QCheckBox { color: #f4f4f5; spacing: 8px; font-size: 13px; background: transparent; }
QCheckBox::indicator {
    width: 18px; height: 18px;
    border-radius: 4px;
    border: 1px solid #3f3f46;
    background: #18181b;
}
QCheckBox::indicator:hover { border-color: #52525b; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QCheckBox::indicator:disabled { background: #09090b; border-color: #27272a; }
QCheckBox:disabled { color: #52525b; }

/* ─ Buttons (default) ────────────────────────────────────────────── */
QPushButton {
    background: #27272a;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 7px 14px;
    color: #e4e4e7;
    font-size: 13px;
    font-weight: 500;
    min-height: 22px;
}
QPushButton:hover  { background: #3f3f46; border-color: #52525b; }
QPushButton:pressed { background: #18181b; }
QPushButton:checked {
    background: #3b82f6;
    border-color: #3b82f6;
    color: #ffffff;
}
QPushButton:checked:hover { background: #2563eb; }
QPushButton:disabled { color: #71717a; border-color: #3f3f46; background: #18181b; }

/* ─ Connect button ───────────────────────────────────────────────── */
QPushButton#ConnBtn {
    background: #10b981;
    border: none;
    color: #ffffff;
    font-size: 14px;
    font-weight: 600;
    min-height: 36px;
    border-radius: 6px;
}
QPushButton#ConnBtn:hover   { background: #059669; }
QPushButton#ConnBtn:pressed { background: #047857; }
QPushButton#ConnBtn:disabled { background: #27272a; color: #52525b; }

/* ─ Scrollbars ───────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: #09090b; width: 10px; border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #27272a; border-radius: 5px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #3f3f46; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

/* ─ Tool tips ────────────────────────────────────────────────────── */
QToolTip {
    background: #18181b;
    border: 1px solid #3f3f46;
    color: #f4f4f5;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 12px;
}

/* ─ Status bar ───────────────────────────────────────────────────── */
QStatusBar { background: #09090b; color: #71717a; font-size: 12px; border: none; border-top: 1px solid #27272a; }
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
