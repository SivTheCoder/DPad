"""
serial_handler.py

Robust ESP32 serial communication module.

Provides :class:`ESP32Handler`, a background-threaded, auto-detecting,
auto-reconnecting serial link to an ESP32 (or compatible USB-serial
board). Incoming newline-delimited messages are forwarded to a
user-supplied callback; the handler transparently detects disconnects
and keeps retrying until a device is found again.

Multiple independent ``ESP32Handler`` instances can be created side by
side (each claims its own port via a shared, thread-safe registry) as a
foundation for supporting several simultaneous devices in the future.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional, Set

import serial
import serial.tools.list_ports
from serial.tools.list_ports_common import ListPortInfo

logger = logging.getLogger(__name__)

#: Default baud rate used if none is specified.
DEFAULT_BAUD_RATE = 115200

#: How often (seconds) the worker loop polls for incoming data.
POLL_INTERVAL = 0.05

#: How long (seconds) to wait between reconnect attempts while
#: disconnected, so scanning for ports doesn't spin-loop.
RECONNECT_INTERVAL = 1.0

#: Known USB vendor IDs for common ESP32 USB-serial bridge chips:
#: Silicon Labs CP210x, WCH CH340/CH341, FTDI, and Espressif's native
#: USB-JTAG/serial. Checking VID is far more reliable than description
#: text, which varies by OS and driver.
KNOWN_VENDOR_IDS: Set[int] = {0x10C4, 0x1A86, 0x0403, 0x303A}

#: Description/manufacturer substrings used as a fallback when VID/PID
#: information isn't available on a given platform.
KNOWN_DESCRIPTION_KEYWORDS: List[str] = [
    "CP210",
    "CH340",
    "CH341",
    "USB Serial",
    "Silicon Labs",
    "Espressif",
]


class ESP32Detector:
    """Locates attached serial ports that are likely an ESP32 device."""

    @classmethod
    def find_candidate_ports(
        cls, exclude: Optional[Set[str]] = None
    ) -> List[ListPortInfo]:
        """Return all currently attached ports that look like an ESP32.

        Args:
            exclude: Device path strings (e.g. ``"COM3"``) to skip. Used
                so multiple handlers never race for the same port.

        Returns:
            Matching ports, in the order reported by the OS.
        """
        exclude = exclude or set()
        return [
            port
            for port in serial.tools.list_ports.comports()
            if port.device not in exclude and cls._looks_like_esp32(port)
        ]

    @staticmethod
    def _looks_like_esp32(port: ListPortInfo) -> bool:
        """Heuristically decide whether a port is an ESP32-family device.

        Args:
            port: Port info as reported by ``pyserial``.

        Returns:
            ``True`` if the port's VID or description matches a known
            ESP32 USB-serial bridge.
        """
        if port.vid in KNOWN_VENDOR_IDS:
            return True

        haystack = f"{port.description or ''} {port.manufacturer or ''}"
        return any(keyword in haystack for keyword in KNOWN_DESCRIPTION_KEYWORDS)


class ESP32Handler:
    """Maintains a resilient serial connection to a single ESP32 device.

    On construction, a daemon background thread starts immediately (by
    default) and:

    1. While disconnected, scans for a candidate port and connects to it.
    2. While connected, reads newline-delimited messages, forwards them
       to :attr:`callback`, and detects disconnection so it can fall back
       to scanning again.

    The public attributes ``connected`` and ``port`` mirror the original
    module's API, so existing callers (e.g. ``gui.py``'s connection
    indicator, ``main.py``'s serial command router) keep working
    unmodified.
    """

    #: Ports currently claimed by *any* handler instance in this process,
    #: so multiple handlers (future multi-device support) never both try
    #: to open the same port.
    _claimed_ports: Set[str] = set()
    _claimed_ports_lock = threading.Lock()

    def __init__(
        self,
        baud_rate: int = DEFAULT_BAUD_RATE,
        preferred_port: Optional[str] = None,
        auto_start: bool = True,
    ) -> None:
        """Initialize the handler and (by default) start its worker thread.

        Args:
            baud_rate: Serial baud rate to use for the connection.
            preferred_port: If given, only this exact device path is
                considered during detection. Useful once multi-device
                support assigns specific ports to specific handlers.
            auto_start: Whether to start the background worker thread
                immediately. Set to ``False`` to call :meth:`start`
                manually after further setup.
        """
        self.baud_rate = baud_rate
        self.preferred_port = preferred_port

        #: Called with each received message (a stripped, decoded line).
        #: May be reassigned at any time by the owner of this handler.
        self.callback: Optional[Callable[[str], None]] = None

        self._serial: Optional[serial.Serial] = None
        self._connected = False
        self._port: Optional[str] = None

        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if auto_start:
            self.start()

    # ------------------------------------------------------------------
    # Public status properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether a device is currently connected."""
        with self._state_lock:
            return self._connected

    @property
    def port(self) -> Optional[str]:
        """The device path of the currently connected port, if any."""
        with self._state_lock:
            return self._port

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background connect/read worker thread.

        Safe to call even if a worker is already running; the call is
        simply ignored in that case.
        """
        if self._thread and self._thread.is_alive():
            logger.debug("Worker thread already running; ignoring start().")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="ESP32HandlerWorker", daemon=True
        )
        self._thread.start()
        logger.info("ESP32Handler worker thread started.")

    def close(self) -> None:
        """Stop the worker thread and release the serial port cleanly.

        Safe to call multiple times, and safe to call even if the handler
        never successfully connected.
        """
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        self._disconnect()
        logger.info("ESP32Handler closed.")

    def send(self, data: str) -> bool:
        """Send a line of text to the connected device, if any.

        Args:
            data: Text to send. A trailing newline is appended if missing.

        Returns:
            ``True`` if the data was written successfully, ``False`` if
            there was no active connection or the write failed.
        """
        with self._write_lock:
            connection = self._serial
            if connection is None:
                logger.warning("Cannot send; no device connected.")
                return False

            try:
                payload = data if data.endswith("\n") else data + "\n"
                connection.write(payload.encode("utf-8"))
                return True
            except (serial.SerialException, OSError) as exc:
                logger.error("Failed to send data: %s", exc)
                self._handle_disconnect()
                return False

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Main loop: connect when needed, read continuously while connected."""
        last_scan_time = 0.0

        while not self._stop_event.is_set():
            if not self.connected:
                now = time.monotonic()
                if now - last_scan_time >= RECONNECT_INTERVAL:
                    last_scan_time = now
                    self._try_connect()
            else:
                self._read_available()

            time.sleep(POLL_INTERVAL)

    def _try_connect(self) -> None:
        """Attempt to find and connect to a candidate ESP32 port."""
        if self.preferred_port:
            candidates = [
                p
                for p in serial.tools.list_ports.comports()
                if p.device == self.preferred_port
            ]
        else:
            with self._claimed_ports_lock:
                claimed = set(self._claimed_ports)
            candidates = ESP32Detector.find_candidate_ports(exclude=claimed)

        for candidate in candidates:
            if self._connect_to(candidate.device):
                return

    def _connect_to(self, device_path: str) -> bool:
        """Attempt to open a serial connection to a specific device path.

        Args:
            device_path: OS device path, e.g. ``"COM3"`` or
                ``"/dev/ttyUSB0"``.

        Returns:
            ``True`` if the connection was established successfully.
        """
        try:
            connection = serial.Serial(device_path, self.baud_rate, timeout=0.1)
        except (serial.SerialException, OSError) as exc:
            logger.debug("Could not open %s: %s", device_path, exc)
            return False

        with self._claimed_ports_lock:
            self._claimed_ports.add(device_path)

        with self._state_lock:
            self._serial = connection
            self._connected = True
            self._port = device_path

        logger.info(
            "Connected to ESP32 on %s at %d baud.", device_path, self.baud_rate
        )
        return True

    def _read_available(self) -> None:
        """Read and dispatch any pending line(s) from the serial buffer."""
        connection = self._serial
        if connection is None:
            return

        try:
            if connection.in_waiting:
                raw_line = connection.readline()
                message = raw_line.decode("utf-8", errors="replace").strip()
                if message:
                    self._dispatch(message)
        except (serial.SerialException, OSError) as exc:
            logger.warning("Lost connection to %s: %s", self._port, exc)
            self._handle_disconnect()

    def _dispatch(self, message: str) -> None:
        """Forward a received message to the user callback, if set.

        Exceptions raised by the callback are caught and logged so a bug
        in application code can never take down the serial worker thread.

        Args:
            message: The decoded, stripped line received from the device.
        """
        callback = self.callback
        if callback is None:
            return

        try:
            callback(message)
        except Exception:
            logger.exception("Error in serial callback for message: %r", message)

    def _handle_disconnect(self) -> None:
        """React to a detected disconnect by tearing down the connection."""
        logger.warning("ESP32 on %s disconnected.", self._port)
        self._disconnect()

    def _disconnect(self) -> None:
        """Close the serial connection (if any) and reset connection state.

        Idempotent and thread-safe: safe to call from both the worker
        thread (on a read failure) and the calling thread (e.g. from
        :meth:`send` or :meth:`close`) without corrupting state.
        """
        with self._state_lock:
            connection = self._serial
            port = self._port
            self._serial = None
            self._connected = False
            self._port = None

        if connection is not None:
            try:
                connection.close()
            except (serial.SerialException, OSError):
                logger.exception("Error closing serial connection.")

        if port is not None:
            with self._claimed_ports_lock:
                self._claimed_ports.discard(port)