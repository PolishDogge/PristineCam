"""
PristineCam — mDNS / Zeroconf Device Discovery
===============================================
Listens for _pristinecam._tcp.local. service records on the local network
and emits Qt signals when a device appears or disappears.

The signals are guaranteed to be delivered on the Qt main thread via
Qt's queued connection mechanism, so UI slots can be connected directly.

Usage
-----
    worker = DiscoveryWorker()
    worker.device_found.connect(lambda host, port, name: ...)
    worker.device_lost.connect(lambda name: ...)
    worker.start()
    # ... later ...
    worker.stop()
"""
from __future__ import annotations

import socket
from typing import Optional

try:
    from zeroconf import ServiceBrowser, Zeroconf
    _HAS_ZEROCONF = True
except ImportError:
    _HAS_ZEROCONF = False

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

SERVICE_TYPE = "_pristinecam._tcp.local."


def is_available() -> bool:
    """Return True if both zeroconf and PyQt6 are importable."""
    return _HAS_ZEROCONF and _HAS_QT


if _HAS_QT:
    class DiscoveryWorker(QObject):
        """
        Manages the Zeroconf browser lifecycle.

        Emits Qt signals — guaranteed to be delivered on the main thread
        via Qt's queued-connection mechanism even though Zeroconf's callbacks
        fire on its internal daemon thread.

        Signals
        -------
        device_found(host: str, port: int, name: str)
            Emitted when a new PristineCam device is discovered.
        device_lost(name: str)
            Emitted when a previously discovered device disappears.
        """

        device_found = pyqtSignal(str, int, str)   # host, port, name
        device_lost  = pyqtSignal(str)              # name

        def __init__(self, parent: Optional[QObject] = None) -> None:
            super().__init__(parent)
            if not _HAS_ZEROCONF:
                raise ImportError(
                    "zeroconf package is not installed.\n"
                    "  pip install zeroconf>=0.131.0"
                )
            self._zc:      Optional[Zeroconf]     = None
            self._browser: Optional[ServiceBrowser] = None

        def start(self) -> None:
            """Start listening for PristineCam devices on the LAN."""
            if self._zc is not None:
                return  # already running
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, self)  # type: ignore[arg-type]

        def stop(self) -> None:
            """Stop browsing and release multicast socket."""
            browser, self._browser = self._browser, None
            zc,      self._zc      = self._zc,      None
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass

        # ── ServiceListener protocol (called on Zeroconf's daemon thread) ──

        def add_service(self, zc: "Zeroconf", stype: str, name: str) -> None:  # type: ignore[override]
            try:
                info = zc.get_service_info(stype, name, timeout=3000)
                if info and info.addresses:
                    host = socket.inet_ntoa(info.addresses[0])
                    # pyqtSignal.emit() is thread-safe and uses Qt's queued
                    # connection to deliver on the main thread.
                    self.device_found.emit(host, info.port, name)
            except Exception:
                pass

        def remove_service(self, zc: "Zeroconf", stype: str, name: str) -> None:  # type: ignore[override]
            try:
                self.device_lost.emit(name)
            except Exception:
                pass

        def update_service(self, zc: "Zeroconf", stype: str, name: str) -> None:  # type: ignore[override]
            self.add_service(zc, stype, name)

else:
    # Stub so the rest of the codebase can import without crashing
    class DiscoveryWorker:  # type: ignore[no-redef]
        def __init__(self, *_, **__) -> None:
            raise ImportError("PyQt6 or zeroconf is not installed.")
        def start(self) -> None: pass
        def stop(self)  -> None: pass
