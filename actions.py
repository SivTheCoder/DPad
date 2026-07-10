"""
actions.py

Scalable action engine for the desktop macro application.

All macro behavior lives behind a single :class:`ActionManager`, which:

- Implements the built-in actions (mouse clicks, scrolling, mouse
  movement, keyboard shortcuts, typing text, delays, launching
  applications, and opening URLs).
- Maintains a name -> callable registry so new actions (including future
  third-party *plugin actions*) can be registered at runtime with
  :meth:`ActionManager.register_action` and invoked generically via
  :meth:`ActionManager.execute`.
- Can run an ordered list of steps via :meth:`ActionManager.run_sequence`,
  which is the building block future *macro profiles* will be built on.

For backward compatibility, the original module-level API is preserved
exactly: the ``paused`` flag and the ``click`` / ``scroll_up`` /
``scroll_down`` / ``capture_position`` functions still exist and behave
the same way, so existing callers (``gui.py``, ``main.py``) require no
changes. They are now thin wrappers around a shared, module-level
:data:`default_manager` instance.
"""

from __future__ import annotations

import functools
import logging
import subprocess
import time
import webbrowser
from typing import Any, Callable, Dict, List, Optional, Sequence

import pyautogui

logger = logging.getLogger(__name__)

#: Global pause flag. Preserved at module level (rather than only on the
#: ActionManager instance) because existing code toggles it directly via
#: ``actions.paused = not actions.paused``.
paused = False


def _logged(func: Callable) -> Callable:
    """Decorator that logs unhandled exceptions raised by an action.

    Actions are frequently invoked from hotkey callbacks and Qt timer
    slots, where an unhandled exception would otherwise be silently
    swallowed by the underlying library or crash a callback thread. This
    decorator ensures every failure is logged with full context instead.
    """

    @functools.wraps(func)
    def wrapper(self: "ActionManager", *args: Any, **kwargs: Any) -> Any:
        try:
            return func(self, *args, **kwargs)
        except Exception:
            logger.exception("Error while executing action '%s'.", func.__name__)
            return None

    return wrapper


def _respects_pause(func: Callable) -> Callable:
    """Decorator that skips an action while the engine is paused.

    Applied only to actions that actually control the mouse/keyboard
    (i.e. macro "playback"). Utility actions such as
    :meth:`ActionManager.capture_position` intentionally ignore pause,
    since capturing state is not the same as performing automation.
    Implies :func:`_logged` so every guarded action also gets error
    logging for free.
    """

    @functools.wraps(func)
    def wrapper(self: "ActionManager", *args: Any, **kwargs: Any) -> Any:
        if self.paused:
            logger.debug("Skipping '%s' - engine is paused.", func.__name__)
            return None
        return _logged(func)(self, *args, **kwargs)

    return wrapper


class ActionManager:
    """Executes and manages all macro actions.

    An ``ActionManager`` owns a registry mapping action names to bound
    methods. Built-in actions are registered automatically on
    construction; additional actions (e.g. from a future plugin system)
    can be added at runtime with :meth:`register_action`.
    """

    #: Default interval (seconds) between keystrokes for `type_text`.
    DEFAULT_TYPE_INTERVAL = 0.02

    def __init__(self) -> None:
        self._actions: Dict[str, Callable[..., Any]] = {}
        self._register_builtin_actions()

    # ------------------------------------------------------------------
    # Pause state
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        """Whether macro playback is currently paused.

        Backed by the module-level ``paused`` flag so external code that
        reads/writes ``actions.paused`` directly stays in sync with any
        ``ActionManager`` instance.
        """
        return paused

    @paused.setter
    def paused(self, value: bool) -> None:
        global paused
        paused = bool(value)
        logger.info("Paused = %s", paused)

    # ------------------------------------------------------------------
    # Registry / generic dispatch
    # ------------------------------------------------------------------

    def _register_builtin_actions(self) -> None:
        """Populate the action registry with all built-in actions."""
        self._actions.update(
            {
                "click": self.click,
                "right_click": self.right_click,
                "double_click": self.double_click,
                "scroll_up": self.scroll_up,
                "scroll_down": self.scroll_down,
                "scroll": self.scroll,
                "move_mouse": self.move_mouse,
                "press_hotkey": self.press_hotkey,
                "type_text": self.type_text,
                "delay": self.delay,
                "launch_application": self.launch_application,
                "open_url": self.open_url,
                "capture_position": self.capture_position,
            }
        )

    def register_action(self, name: str, handler: Callable[..., Any]) -> None:
        """Register (or override) an action under a given name.

        This is the extension point future plugin actions should use:
        a plugin simply calls ``manager.register_action("my_action", fn)``
        and the action immediately becomes available to
        :meth:`execute` and :meth:`run_sequence`.

        Args:
            name: Unique action name (used to reference it in sequences).
            handler: Callable implementing the action.
        """
        if name in self._actions:
            logger.warning("Overwriting existing action registration: '%s'", name)
        self._actions[name] = handler
        logger.debug("Registered action '%s'.", name)

    def available_actions(self) -> List[str]:
        """Return the names of all currently registered actions."""
        return sorted(self._actions)

    def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a registered action by name.

        Args:
            name: The registered action name.
            *args: Positional arguments forwarded to the action.
            **kwargs: Keyword arguments forwarded to the action.

        Returns:
            Whatever the underlying action returns (``None`` for most
            actions).

        Raises:
            KeyError: If no action is registered under ``name``.
        """
        handler = self._actions.get(name)
        if handler is None:
            raise KeyError(f"No action registered under name '{name}'.")

        logger.debug("Executing action '%s' args=%s kwargs=%s", name, args, kwargs)
        return handler(*args, **kwargs)

    def run_sequence(self, steps: Sequence[Dict[str, Any]]) -> None:
        """Run an ordered sequence of actions.

        This is the primitive future *macro profiles* are built on: a
        profile is simply a list of steps like::

            [
                {"action": "click", "kwargs": {"config": config}},
                {"action": "delay", "args": [0.5]},
                {"action": "type_text", "kwargs": {"text": "hello"}},
            ]

        A failure in one step is logged and does not stop the remaining
        steps from running.

        Args:
            steps: An ordered list of ``{"action": name, "args": [...],
                "kwargs": {...}}`` dictionaries. ``args``/``kwargs`` are
                optional.
        """
        for index, step in enumerate(steps):
            name = step.get("action")
            args = step.get("args", ())
            kwargs = step.get("kwargs", {})

            if not name:
                logger.warning("Skipping sequence step %d: missing 'action'.", index)
                continue

            try:
                self.execute(name, *args, **kwargs)
            except KeyError:
                logger.error(
                    "Skipping sequence step %d: unknown action '%s'.", index, name
                )
            except Exception:
                logger.exception(
                    "Sequence step %d ('%s') raised an unexpected error.", index, name
                )

    # ------------------------------------------------------------------
    # Mouse actions
    # ------------------------------------------------------------------

    @_respects_pause
    def click(
        self, config: dict, x: Optional[int] = None, y: Optional[int] = None
    ) -> None:
        """Perform a left mouse click.

        Args:
            config: Application config; used for the click position when
                ``x``/``y`` are not given.
            x: Optional explicit x-coordinate, overriding the saved position.
            y: Optional explicit y-coordinate, overriding the saved position.
        """
        target_x, target_y = self._resolve_position(config, x, y)
        pyautogui.click(target_x, target_y)
        logger.info("Clicked at (%d, %d).", target_x, target_y)

    @_respects_pause
    def right_click(
        self, config: dict, x: Optional[int] = None, y: Optional[int] = None
    ) -> None:
        """Perform a right mouse click.

        Args:
            config: Application config; used for the click position when
                ``x``/``y`` are not given.
            x: Optional explicit x-coordinate, overriding the saved position.
            y: Optional explicit y-coordinate, overriding the saved position.
        """
        target_x, target_y = self._resolve_position(config, x, y)
        pyautogui.click(target_x, target_y, button="right")
        logger.info("Right-clicked at (%d, %d).", target_x, target_y)

    @_respects_pause
    def double_click(
        self, config: dict, x: Optional[int] = None, y: Optional[int] = None
    ) -> None:
        """Perform a double left mouse click.

        Args:
            config: Application config; used for the click position when
                ``x``/``y`` are not given.
            x: Optional explicit x-coordinate, overriding the saved position.
            y: Optional explicit y-coordinate, overriding the saved position.
        """
        target_x, target_y = self._resolve_position(config, x, y)
        pyautogui.doubleClick(target_x, target_y)
        logger.info("Double-clicked at (%d, %d).", target_x, target_y)

    @_respects_pause
    def scroll_up(self, config: dict) -> None:
        """Scroll up by ``config['scroll_amount']``."""
        self.scroll(config["scroll_amount"])

    @_respects_pause
    def scroll_down(self, config: dict) -> None:
        """Scroll down by ``config['scroll_amount']``."""
        self.scroll(-config["scroll_amount"])

    @_respects_pause
    def scroll(self, amount: int) -> None:
        """Scroll by an arbitrary signed amount.

        Args:
            amount: Positive scrolls up, negative scrolls down (matches
                ``pyautogui.scroll`` semantics).
        """
        pyautogui.scroll(amount)
        logger.info("Scrolled by %d.", amount)

    @_respects_pause
    def move_mouse(self, x: int, y: int, duration: float = 0.0) -> None:
        """Move the mouse cursor to an absolute position.

        Args:
            x: Target x-coordinate.
            y: Target y-coordinate.
            duration: Seconds the movement should take (``0`` = instant).
        """
        pyautogui.moveTo(x, y, duration=duration)
        logger.info("Moved mouse to (%d, %d) over %.2fs.", x, y, duration)

    def capture_position(self, config: dict) -> tuple[int, int]:
        """Capture the current cursor position into ``config``.

        Intentionally does not respect the paused flag: capturing the
        cursor position is a setup step, not macro playback.

        Args:
            config: Application config to update in place.

        Returns:
            The captured ``(x, y)`` coordinates.
        """
        x, y = pyautogui.position()

        config["click_position"]["x"] = x
        config["click_position"]["y"] = y

        logger.info("Captured click position: (%d, %d).", x, y)
        return x, y

    @staticmethod
    def _resolve_position(
        config: dict, x: Optional[int], y: Optional[int]
    ) -> tuple[int, int]:
        """Resolve the coordinates a mouse action should target.

        Args:
            config: Application config holding the saved click position.
            x: Explicit override x-coordinate, if any.
            y: Explicit override y-coordinate, if any.

        Returns:
            The ``(x, y)`` tuple to act on.
        """
        if x is not None and y is not None:
            return x, y
        return config["click_position"]["x"], config["click_position"]["y"]

    # ------------------------------------------------------------------
    # Keyboard actions
    # ------------------------------------------------------------------

    @_respects_pause
    def press_hotkey(self, *keys: str) -> None:
        """Press a keyboard shortcut (a chord of keys held simultaneously).

        Args:
            *keys: Key names in the format ``pyautogui.hotkey`` expects,
                e.g. ``press_hotkey("ctrl", "c")``.
        """
        if not keys:
            logger.warning("press_hotkey called with no keys; ignoring.")
            return
        pyautogui.hotkey(*keys)
        logger.info("Pressed hotkey: %s", "+".join(keys))

    @_respects_pause
    def type_text(self, text: str, interval: float = DEFAULT_TYPE_INTERVAL) -> None:
        """Type a string of text as though from the keyboard.

        Args:
            text: The text to type.
            interval: Delay in seconds between each keystroke.
        """
        pyautogui.write(text, interval=interval)
        logger.info("Typed text of length %d.", len(text))

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    @_logged
    def delay(self, seconds: float) -> None:
        """Pause execution for a fixed duration.

        Not gated by the paused flag, since this is a passive wait rather
        than an interaction with the mouse/keyboard.

        Args:
            seconds: How long to sleep, in seconds.
        """
        time.sleep(max(0.0, seconds))
        logger.debug("Delayed for %.3fs.", seconds)

    # ------------------------------------------------------------------
    # System integration
    # ------------------------------------------------------------------

    @_respects_pause
    def launch_application(
        self, path: str, args: Optional[List[str]] = None
    ) -> None:
        """Launch an external application.

        Args:
            path: Path (or command name on ``PATH``) of the executable.
            args: Optional list of command-line arguments.

        Raises:
            Any exception from :class:`subprocess.Popen` is caught by the
            error-logging decorator and logged rather than propagated.
        """
        command = [path, *(args or [])]
        subprocess.Popen(command)
        logger.info("Launched application: %s", " ".join(command))

    @_respects_pause
    def open_url(self, url: str) -> None:
        """Open a URL in the user's default web browser.

        Args:
            url: The URL to open. A ``https://`` scheme is assumed if
                none is present.
        """
        normalized_url = url if "://" in url else f"https://{url}"
        webbrowser.open(normalized_url)
        logger.info("Opened URL: %s", normalized_url)


# ----------------------------------------------------------------------
# Backward-compatible module-level API
# ----------------------------------------------------------------------

#: Shared ActionManager instance used by the module-level convenience
#: functions below. New code is encouraged to use ``ActionManager``
#: directly (or this instance) to access the full action set; the plain
#: functions remain solely for compatibility with existing callers.
default_manager = ActionManager()


def click(config: dict) -> None:
    """Perform a left mouse click at the saved position (legacy API)."""
    default_manager.click(config)


def scroll_up(config: dict) -> None:
    """Scroll up by the configured amount (legacy API)."""
    default_manager.scroll_up(config)


def scroll_down(config: dict) -> None:
    """Scroll down by the configured amount (legacy API)."""
    default_manager.scroll_down(config)


def capture_position(config: dict) -> tuple[int, int]:
    """Capture the current cursor position into config (legacy API)."""
    return default_manager.capture_position(config)