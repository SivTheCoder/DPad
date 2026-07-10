"""
config.py

Configuration management for the desktop macro application.

Provides :class:`ConfigManager`, responsible for:

- Locating and creating the config file next to the running executable
  (or next to this source file during development).
- Loading, validating, and repairing configuration data - a malformed or
  corrupted file is quarantined and replaced with sane defaults rather
  than crashing the application.
- Upgrading configs written by older versions of the application
  (schema *migrations*).
- Supporting multiple named configuration *profiles*, with one profile
  active at a time, as a foundation for a future macro-profile feature.

Module-level ``load_config()`` / ``save_config()`` functions are kept for
backward compatibility with existing callers (``gui.py``, ``main.py``)
and simply delegate to a shared, module-level :data:`default_manager`
instance, so no other module needs to change.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Filename of the config file, stored next to the executable/script.
CONFIG_FILENAME = "config.json"

#: Bumped whenever the on-disk schema changes; drives migrations in
#: :meth:`ConfigManager._migrate`.
CURRENT_VERSION = 2

#: Name of the profile used when none is specified, and on first run.
DEFAULT_PROFILE_NAME = "default"

#: Default values for a single profile's settings. Also used to fill in
#: any missing/invalid keys when validating a loaded profile, so
#: partially corrupted data can be repaired field-by-field instead of
#: being discarded entirely.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "click_position": {"x": 500, "y": 500},
    "scroll_amount": 400,
}


class ConfigManager:
    """Loads, validates, migrates, and persists application configuration.

    The on-disk document has the shape::

        {
            "version": 2,
            "active_profile": "default",
            "profiles": {
                "default": {
                    "click_position": {"x": 500, "y": 500},
                    "scroll_amount": 400
                },
                "streaming": { ... }
            }
        }

    Callers normally only interact with a single profile's *settings*
    dict (e.g. ``config["scroll_amount"]``), which is exactly what
    :meth:`load` returns. This keeps the manager fully compatible with
    existing code that reads and mutates that dict directly.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        """Initialize the manager.

        Args:
            config_path: Optional explicit path to the config file.
                Defaults to a location next to the running executable.
        """
        self.config_path = config_path or self._default_config_path()
        self._data: Dict[str, Any] = {}
        self._active_profile: str = DEFAULT_PROFILE_NAME

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _default_config_path() -> Path:
        """Determine where the config file should live.

        Uses the directory containing the running executable when frozen
        (e.g. packaged with PyInstaller), or the directory containing
        this source file during development, so the config always sits
        next to the application rather than in whatever the current
        working directory happens to be.

        Returns:
            The default path to the config file.
        """
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parent

        return base_dir / CONFIG_FILENAME

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Load configuration from disk, creating/repairing it as needed.

        Args:
            profile: Name of the profile to activate and return. Defaults
                to whichever profile was last active (or
                :data:`DEFAULT_PROFILE_NAME` on first run).

        Returns:
            The settings dict for the active profile. This is the same
            object held internally, so in-place mutations (as existing
            code performs, e.g. ``config["scroll_amount"] += 100``) are
            automatically reflected the next time :meth:`save` is called.
        """
        if not self.config_path.exists():
            logger.info(
                "No config file found at %s; creating with defaults.",
                self.config_path,
            )
            self._data = self._build_default_document()
        else:
            self._data = self._read_from_disk()

        self._data = self._migrate(self._data)

        self._active_profile = profile or self._data.get(
            "active_profile", DEFAULT_PROFILE_NAME
        )
        self._ensure_profile_exists(self._active_profile)
        self._data["active_profile"] = self._active_profile

        settings = self._validate_settings(self._data["profiles"][self._active_profile])
        self._data["profiles"][self._active_profile] = settings

        self._write_to_disk(self._data)
        return settings

    def save(self, settings: Dict[str, Any], profile: Optional[str] = None) -> None:
        """Persist a settings dict back to disk.

        Args:
            settings: The settings dict to save (typically the same
                object previously returned by :meth:`load`).
            profile: Which profile to save into. Defaults to the
                currently active profile.
        """
        target_profile = profile or self._active_profile
        validated = self._validate_settings(settings)

        self._ensure_profile_exists(target_profile)
        self._data["profiles"][target_profile] = validated
        self._data["active_profile"] = self._active_profile
        self._data["version"] = CURRENT_VERSION

        self._write_to_disk(self._data)
        logger.info("Saved config for profile '%s'.", target_profile)

    def list_profiles(self) -> list[str]:
        """Return the names of all known profiles, sorted alphabetically."""
        return sorted(self._data.get("profiles", {}))

    def switch_profile(self, name: str) -> Dict[str, Any]:
        """Make ``name`` the active profile, creating it from defaults if new.

        Args:
            name: Name of the profile to activate.

        Returns:
            The (possibly newly created) settings dict for that profile.
        """
        self._ensure_profile_exists(name)
        self._active_profile = name
        self._data["active_profile"] = name
        self._write_to_disk(self._data)
        return self._data["profiles"][name]

    def create_profile(self, name: str, base_on: Optional[str] = None) -> Dict[str, Any]:
        """Create a new profile.

        Args:
            name: Name for the new profile. Must not already exist.
            base_on: Optional existing profile name to copy settings
                from. If omitted, the new profile starts from defaults.

        Returns:
            The newly created settings dict.

        Raises:
            ValueError: If a profile with this name already exists.
        """
        profiles = self._data.setdefault("profiles", {})
        if name in profiles:
            raise ValueError(f"Profile '{name}' already exists.")

        source = profiles.get(base_on) if base_on else None
        profiles[name] = copy.deepcopy(source if source is not None else DEFAULT_SETTINGS)

        self._write_to_disk(self._data)
        logger.info("Created profile '%s'%s.", name, f" (from '{base_on}')" if base_on else "")
        return profiles[name]

    def delete_profile(self, name: str) -> None:
        """Delete a profile.

        Args:
            name: Name of the profile to delete.

        Raises:
            ValueError: If the profile doesn't exist, or it is the only
                remaining profile (at least one must always exist).
        """
        profiles = self._data.get("profiles", {})
        if name not in profiles:
            raise ValueError(f"Profile '{name}' does not exist.")
        if len(profiles) <= 1:
            raise ValueError("Cannot delete the only remaining profile.")

        del profiles[name]
        if self._active_profile == name:
            self._active_profile = next(iter(profiles))
            self._data["active_profile"] = self._active_profile

        self._write_to_disk(self._data)
        logger.info("Deleted profile '%s'.", name)

    @property
    def active_profile(self) -> str:
        """Name of the currently active profile."""
        return self._active_profile

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _read_from_disk(self) -> Dict[str, Any]:
        """Read and parse the config file, recovering from corruption.

        If the file cannot be parsed as JSON, or doesn't contain a JSON
        object, it is quarantined (renamed with a timestamped suffix) so
        no data is silently destroyed, and a fresh default document is
        returned instead of crashing the application.

        Returns:
            A raw config document (not yet migrated or validated).
        """
        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.error("Config file is corrupted or unreadable: %s", exc)
            self._quarantine_corrupted_file()
            return self._build_default_document()

        if not isinstance(data, dict):
            logger.error("Config file did not contain a JSON object; resetting.")
            self._quarantine_corrupted_file()
            return self._build_default_document()

        return data

    def _write_to_disk(self, data: Dict[str, Any]) -> None:
        """Write the full config document to disk atomically.

        Writes to a temporary file first and then replaces the real
        config file, so a crash or power loss mid-write can never leave
        behind a half-written, corrupted file.

        Args:
            data: The complete document (all profiles) to persist.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.config_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=4)
            tmp_path.replace(self.config_path)
        except OSError:
            logger.exception("Failed to write config file to %s.", self.config_path)

    def _quarantine_corrupted_file(self) -> None:
        """Rename an unreadable config file so it isn't silently overwritten."""
        try:
            backup_path = self.config_path.with_name(
                f"{self.config_path.name}.corrupted.{int(time.time())}"
            )
            self.config_path.replace(backup_path)
            logger.warning("Backed up corrupted config to %s.", backup_path)
        except OSError:
            logger.exception("Failed to back up corrupted config file.")

    # ------------------------------------------------------------------
    # Defaults / validation / migration
    # ------------------------------------------------------------------

    @staticmethod
    def _build_default_document() -> Dict[str, Any]:
        """Build a brand-new, fully valid config document.

        Returns:
            A document containing a single default profile.
        """
        return {
            "version": CURRENT_VERSION,
            "active_profile": DEFAULT_PROFILE_NAME,
            "profiles": {
                DEFAULT_PROFILE_NAME: copy.deepcopy(DEFAULT_SETTINGS),
            },
        }

    def _ensure_profile_exists(self, name: str) -> None:
        """Create a profile with default settings if it doesn't exist yet.

        Args:
            name: Profile name to ensure exists in ``self._data``.
        """
        profiles = self._data.setdefault("profiles", {})
        if name not in profiles:
            logger.info("Profile '%s' not found; creating with defaults.", name)
            profiles[name] = copy.deepcopy(DEFAULT_SETTINGS)

    @staticmethod
    def _validate_settings(settings: Any) -> Dict[str, Any]:
        """Validate a settings dict, repairing individual invalid fields.

        Rather than discarding an entire profile because one field is
        malformed, each field is checked independently and replaced with
        its default value if missing or invalid. This maximizes how much
        of a user's configuration survives corruption or a bad manual
        edit of the JSON file.

        Args:
            settings: The (possibly malformed) settings value to validate.

        Returns:
            A settings dict guaranteed to contain every default key with
            a value of the expected type.
        """
        if not isinstance(settings, dict):
            logger.warning("Settings block was not an object; using defaults.")
            return copy.deepcopy(DEFAULT_SETTINGS)

        validated = copy.deepcopy(DEFAULT_SETTINGS)

        click_position = settings.get("click_position")
        if (
            isinstance(click_position, dict)
            and isinstance(click_position.get("x"), (int, float))
            and isinstance(click_position.get("y"), (int, float))
        ):
            validated["click_position"] = {
                "x": int(click_position["x"]),
                "y": int(click_position["y"]),
            }
        else:
            logger.warning("Invalid or missing 'click_position'; using default.")

        scroll_amount = settings.get("scroll_amount")
        if isinstance(scroll_amount, (int, float)) and scroll_amount > 0:
            validated["scroll_amount"] = int(scroll_amount)
        else:
            logger.warning("Invalid or missing 'scroll_amount'; using default.")

        return validated

    def _migrate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Upgrade an on-disk document to the current schema version.

        Args:
            data: The raw document as read from disk (already guaranteed
                to be a ``dict``).

        Returns:
            A document conforming to :data:`CURRENT_VERSION`'s schema.
        """
        version = data.get("version")

        if version is None and "profiles" not in data:
            # Pre-versioning, flat schema:
            # {"click_position": {...}, "scroll_amount": ...}
            logger.info("Migrating legacy flat config to versioned profile format.")
            data = {
                "version": 1,
                "active_profile": DEFAULT_PROFILE_NAME,
                "profiles": {
                    DEFAULT_PROFILE_NAME: {
                        "click_position": data.get(
                            "click_position", DEFAULT_SETTINGS["click_position"]
                        ),
                        "scroll_amount": data.get(
                            "scroll_amount", DEFAULT_SETTINGS["scroll_amount"]
                        ),
                    }
                },
            }
            version = 1

        # Future schema changes get their own migration step here, e.g.:
        #
        # if version == 1:
        #     data = self._migrate_v1_to_v2(data)
        #     version = 2

        data["version"] = CURRENT_VERSION
        data.setdefault("active_profile", DEFAULT_PROFILE_NAME)
        data.setdefault(
            "profiles", {DEFAULT_PROFILE_NAME: copy.deepcopy(DEFAULT_SETTINGS)}
        )
        return data


# ----------------------------------------------------------------------
# Backward-compatible module-level API
# ----------------------------------------------------------------------

#: Shared manager instance backing the module-level functions below.
#: New code that needs profile support should use ``ConfigManager``
#: directly (or this instance); the plain functions remain solely for
#: compatibility with existing callers (``gui.py``, ``main.py``).
default_manager = ConfigManager()


def load_config() -> Dict[str, Any]:
    """Load the active profile's settings (legacy API).

    Returns:
        The settings dict for the currently active profile.
    """
    return default_manager.load()


def save_config(config: Dict[str, Any]) -> None:
    """Save settings into the active profile (legacy API).

    Args:
        config: The settings dict to persist.
    """
    default_manager.save(config)