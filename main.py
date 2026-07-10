"""
main.py

Application entry point for the Teleprompter Desktop macro application.

This module wires together the GUI, the ESP32 serial handler, and the
global keyboard hotkeys used to trigger predefined macro actions (click,
scroll up, scroll down, capture cursor position, pause/resume, and quit).

``main.py`` is intentionally kept as a thin composition / bootstrap layer:
business logic for individual actions lives in ``actions``, hardware
communication lives in ``serial_handler``, and the UI lives in ``gui``.
This separation keeps the entry point easy to extend with future features
such as macro *profiles* (swappable sets of hotkeys/actions), a *plugin*
system for third-party actions, and system tray integration.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List

import keyboard
from PySide6.QtWidgets import QApplication

import actions
from config import load_config
from gui import MainWindow
from serial_handler import ESP32Handler

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure application-wide logging.

    Installs a single stream handler with a consistent format on the root
    logger. Safe to call more than once - subsequent calls only adjust the
    log level instead of adding duplicate handlers.

    Args:
        level: Minimum log level to emit. Defaults to ``logging.INFO``.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


@dataclass
class HotkeyBinding:
    """A single global hotkey -> callback binding.

    Attributes:
        combination: Key combination string understood by the ``keyboard``
            library (e.g. ``"ctrl+shift+p"``).
        callback: Zero-argument callable invoked when the hotkey fires.
        description: Human-readable description. Not currently used at
            runtime, but kept so a future hotkey-configuration screen (or
            macro profile switcher) can list/label bindings without
            changes to this class.
    """

    combination: str
    callback: Callable[[], None]
    description: str = ""


class HotkeyManager:
    """Registers, tracks, and cleanly tears down global keyboard hotkeys.

    Centralizing hotkey registration here (rather than calling
    ``keyboard.add_hotkey`` from many places) makes it straightforward to
    later support macro *profiles*: an entirely different set of bindings
    can be swapped in by calling :meth:`unregister_all` followed by
    :meth:`register_many` with a new binding list.
    """

    def __init__(self) -> None:
        self._bindings: Dict[str, HotkeyBinding] = {}

    def register(self, binding: HotkeyBinding) -> None:
        """Register a single hotkey binding.

        Args:
            binding: The hotkey binding to register.

        Raises:
            RuntimeError: If the ``keyboard`` library fails to register
                the hotkey (e.g. invalid combination string).
        """
        try:
            keyboard.add_hotkey(binding.combination, binding.callback)
        except Exception as exc:  # `keyboard` raises plain Exception subclasses
            raise RuntimeError(
                f"Failed to register hotkey '{binding.combination}': {exc}"
            ) from exc

        self._bindings[binding.combination] = binding
        logger.debug(
            "Registered hotkey '%s' (%s)",
            binding.combination,
            binding.description or "no description",
        )

    def register_many(self, bindings: List[HotkeyBinding]) -> None:
        """Register multiple hotkey bindings at once.

        Args:
            bindings: The list of bindings to register, in order.
        """
        for binding in bindings:
            self.register(binding)

    def unregister_all(self) -> None:
        """Unregister every currently tracked hotkey.

        Safe to call during shutdown even if some hotkeys are already gone;
        failures are logged rather than raised so teardown always
        completes.
        """
        for combination in list(self._bindings):
            try:
                keyboard.remove_hotkey(combination)
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "Could not unregister hotkey '%s': %s", combination, exc
                )
        self._bindings.clear()
        logger.info("All hotkeys unregistered.")


class SerialCommandRouter:
    """Routes commands received over the serial link to the right action.

    This decouples ``serial_handler`` from the concrete action
    implementations and gives future plugin actions a single place to
    register additional serial command handlers via
    :meth:`register_handler`.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._handlers: Dict[str, Callable[[], None]] = {
            "ACTION_CLICK": lambda: actions.click(self._config),
            "ACTION_SCROLL_UP": lambda: actions.scroll_up(self._config),
            "ACTION_SCROLL_DOWN": lambda: actions.scroll_down(self._config),
        }

    def register_handler(self, command: str, handler: Callable[[], None]) -> None:
        """Register or override the handler for a serial command string.

        Intended extension point for future plugin actions that want to
        react to additional commands sent by the ESP32.

        Args:
            command: The command string sent by the device (e.g.
                ``"ACTION_CLICK"``).
            handler: Zero-argument callable to invoke for that command.
        """
        self._handlers[command] = handler

    def dispatch(self, command: str) -> None:
        """Dispatch an incoming serial command to its registered handler.

        Unknown commands are logged and ignored rather than raising, so a
        malformed or unexpected message from the ESP32 can never crash the
        application. Exceptions raised by handlers are caught and logged
        for the same reason.

        Args:
            command: The raw command string received from the device.
        """
        handler = self._handlers.get(command)
        if handler is None:
            logger.warning("Received unknown serial command: %r", command)
            return

        try:
            handler()
        except Exception:
            logger.exception("Error while handling serial command %r", command)


class TeleprompterApplication:
    """Top-level application object.

    Owns the Qt application, the main window, the ESP32 serial handler,
    the hotkey manager, and the serial command router, and is responsible
    for orderly startup and shutdown. This is also the natural place to
    later add system tray integration and macro profile switching, since
    it already holds references to every long-lived component.
    """

    def __init__(self) -> None:
        self.config = load_config()
        self.esp = ESP32Handler()
        self.command_router = SerialCommandRouter(self.config)
        self.hotkeys = HotkeyManager()

        self.app = QApplication(sys.argv)
        self.window = MainWindow(self.config, self.esp)

        self._wire_serial()
        self._register_hotkeys()
        self.app.aboutToQuit.connect(self.shutdown)

    def _wire_serial(self) -> None:
        """Connect the ESP32 handler's callback to the command router."""
        self.esp.callback = self.command_router.dispatch

    def _register_hotkeys(self) -> None:
        """Register all global hotkeys used by the application."""
        bindings = [
            HotkeyBinding("f8", lambda: actions.click(self.config), "Click"),
            HotkeyBinding(
                "f9", lambda: actions.scroll_up(self.config), "Scroll up"
            ),
            HotkeyBinding(
                "f10", lambda: actions.scroll_down(self.config), "Scroll down"
            ),
            HotkeyBinding(
                "ctrl+shift+c",
                self._capture_position,
                "Capture cursor position",
            ),
            HotkeyBinding(
                "ctrl+shift+p", self._toggle_pause, "Pause/resume macros"
            ),
            HotkeyBinding("ctrl+shift+q", self._quit, "Quit application"),
        ]
        self.hotkeys.register_many(bindings)

    def _capture_position(self) -> None:
        """Capture the current cursor position and refresh the GUI label."""
        try:
            actions.capture_position(self.config)
            self.window.update_position_label()
        except Exception:
            logger.exception("Failed to capture cursor position.")

    def _toggle_pause(self) -> None:
        """Toggle the global paused state consumed by ``actions``."""
        actions.paused = not actions.paused
        logger.info("Paused = %s", actions.paused)

    def _quit(self) -> None:
        """Hotkey handler that requests a clean Qt application shutdown."""
        logger.info("Quit hotkey pressed, shutting down.")
        self.app.quit()

    def shutdown(self) -> None:
        """Perform cleanup when the application is about to exit.

        Connected to ``QApplication.aboutToQuit`` so this runs regardless
        of whether the app is closed via the quit hotkey, the main window,
        or any future system tray "Exit" action.
        """
        logger.info("Shutting down...")
        self.hotkeys.unregister_all()

        close = getattr(self.esp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.exception("Error closing ESP32 serial connection.")

    def run(self) -> int:
        """Show the main window and start the Qt event loop.

        Returns:
            The process exit code produced by the Qt event loop.
        """
        self.window.show()
        return self.app.exec()


def main() -> int:
    """Application entry point.

    Returns:
        Process exit code, suitable for passing to ``sys.exit``.
    """
    configure_logging()
    app = TeleprompterApplication()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())