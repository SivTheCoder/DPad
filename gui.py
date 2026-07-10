"""
gui.py

Modern, minimalist desktop UI for the macro controller application, with
a multi-theme picker (dark, light, and five custom color palettes).

The window is composed of a header (title, subtitle, and a "Theme"
dropdown button) and a vertical stack of "section cards" (status, live
mouse position, saved click position, scroll speed, test actions, and a
bottom action bar). Two small reusable widgets -- ``StatusIndicator``
and ``SectionCard`` -- keep the layout code readable and avoid
duplicating styling/markup across sections.

This module only changes presentation. The public surface that other
modules depend on (``MainWindow(config, esp)`` and
``MainWindow.update_position_label()``) is preserved, and all backend
calls (``actions``, ``config.save_config``, ``esp.connected`` /
``esp.port``) are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import pyautogui

import actions
from config import save_config


#: App name shown in the window title and header.
APP_NAME = "DPad"

#: Filename of the app icon. Looked up next to this file (and, when
#: frozen with PyInstaller, next to the packaged executable) so the
#: same code works both from source and from a built .exe.
APP_ICON_FILENAME = "logo.ico"


def _app_base_dir() -> Path:
    """Return the directory to search for bundled assets like the icon.

    When frozen with PyInstaller (``sys.frozen``), assets are looked up
    relative to the executable; otherwise relative to this source file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_app_icon() -> QIcon:
    """Load the application/window icon from ``APP_ICON_FILENAME``.

    Returns:
        The loaded ``QIcon``, or a null (empty) ``QIcon`` if the file
        isn't found -- Qt simply falls back to its default icon in
        that case, so a missing icon file is never fatal.
    """
    icon_path = _app_base_dir() / APP_ICON_FILENAME
    if icon_path.exists():
        return QIcon(str(icon_path))
    return QIcon()


# ============================================================
# THEME DEFINITIONS
# ============================================================
#
# Each theme is a flat dict of role -> CSS color string (roles are listed
# below). Wherever a theme's source palette didn't include an obvious
# candidate for a given role (e.g. no light/dark neutral suitable for
# body text, or a hover/pressed variant of a button color), a value was
# derived with QColor.lighter()/.darker() from one of that theme's own
# colors, so every theme stays visually self-consistent.
#
# Roles:
#   background     window background
#   surface        raised/secondary surface (e.g. inputs, chips)
#   card           section card background
#   card_hover     hover background for secondary buttons/cards
#   border         hairline borders
#   text           primary text
#   text_muted     secondary/muted text
#   accent         primary action color (buttons, slider, links)
#   accent_hover   accent on hover
#   accent_pressed accent on press
#   accent_text    text color placed on top of the accent color
#   success        positive status color
#   warning        cautionary status color
#   danger         destructive status color
#   danger_hover   danger on hover
#   shadow         QColor used for card drop shadows


def _lighter(hex_color: str, percent: int) -> str:
    """Return a lightened variant of a hex color.

    Args:
        hex_color: Source color, e.g. ``"#334455"``.
        percent: Passed straight to ``QColor.lighter`` (150 = 50% lighter).

    Returns:
        The lightened color as a hex string.
    """
    return QColor(hex_color).lighter(percent).name()


def _darker(hex_color: str, percent: int) -> str:
    """Return a darkened variant of a hex color.

    Args:
        hex_color: Source color, e.g. ``"#334455"``.
        percent: Passed straight to ``QColor.darker`` (150 = 33% darker).

    Returns:
        The darkened color as a hex string.
    """
    return QColor(hex_color).darker(percent).name()


THEMES: dict[str, dict] = {
    "dark": {
        "background": "#121317",
        "surface": "#191B21",
        "card": "#1E2027",
        "card_hover": "#22242C",
        "border": "#2B2D36",
        "text": "#EDEEF2",
        "text_muted": "#8B90A0",
        "accent": "#6C63FF",
        "accent_hover": "#7D74FF",
        "accent_pressed": "#5B53E0",
        "accent_text": "#FFFFFF",
        "success": "#3DDC84",
        "warning": "#FFC24B",
        "danger": "#FF5C5C",
        "danger_hover": "#FF7373",
        "shadow": QColor(0, 0, 0, 130),
    },
    "light": {
        "background": "#F5F6F8",
        "surface": "#FFFFFF",
        "card": "#FFFFFF",
        "card_hover": "#F0F1F5",
        "border": "#E3E5EA",
        "text": "#1B1D24",
        "text_muted": "#6B7080",
        "accent": "#5A52E0",
        "accent_hover": "#6C63FF",
        "accent_pressed": "#4A43C4",
        "accent_text": "#FFFFFF",
        "success": "#1FAA63",
        "warning": "#B9791B",
        "danger": "#E4483F",
        "danger_hover": "#F05B52",
        "shadow": QColor(15, 15, 20, 35),
    },
    # Black / slate-blue / orange / off-white.
    "ember_noir": {
        "background": "#000000",
        "surface": "#233D4D",
        "card": "#233D4D",
        "card_hover": _lighter("#233D4D", 118),
        "border": _lighter("#233D4D", 135),
        "text": "#EAECF0",
        "text_muted": "#9FB0BC",
        "accent": "#FE7F2D",
        "accent_hover": _lighter("#FE7F2D", 112),
        "accent_pressed": _darker("#FE7F2D", 115),
        "accent_text": "#12130E",
        "success": "#3DDC84",
        "warning": "#FFC24B",
        "danger": "#FF5C5C",
        "danger_hover": "#FF7373",
        "shadow": QColor(0, 0, 0, 160),
    },
    # Near-black navy / teal-blue / muted teal / off-white.
    "deep_tide": {
        "background": "#061E29",
        "surface": "#1D546D",
        "card": "#1D546D",
        "card_hover": _lighter("#1D546D", 116),
        "border": _lighter("#1D546D", 130),
        "text": "#F3F4F4",
        "text_muted": "#9DB8C2",
        "accent": "#5F9598",
        "accent_hover": _lighter("#5F9598", 114),
        "accent_pressed": _darker("#5F9598", 115),
        "accent_text": "#06181F",
        "success": "#3DDC84",
        "warning": "#FFC24B",
        "danger": "#FF5C5C",
        "danger_hover": "#FF7373",
        "shadow": QColor(0, 0, 0, 150),
    },
    # Navy / royal blue / periwinkle / electric yellow.
    "cobalt_volt": {
        "background": "#000957",
        "surface": "#344CB7",
        "card": "#344CB7",
        "card_hover": _lighter("#344CB7", 116),
        "border": _lighter("#344CB7", 128),
        "text": "#F4F6FB",
        "text_muted": "#577BC1",
        "accent": "#FFEB00",
        "accent_hover": _lighter("#FFEB00", 108),
        "accent_pressed": _darker("#FFEB00", 112),
        "accent_text": "#000957",
        "success": "#3DDC84",
        "warning": "#FFC24B",
        "danger": "#FF6161",
        "danger_hover": "#FF7A7A",
        "shadow": QColor(0, 0, 0, 165),
    },
    # Dark maroon / deep red / crimson / bright yellow.
    "crimson_bloom": {
        "background": "#4A102A",
        "surface": "#85193C",
        "card": "#85193C",
        "card_hover": _lighter("#85193C", 116),
        "border": _lighter("#85193C", 128),
        "text": "#F7ECEA",
        "text_muted": "#DDA9B7",
        "accent": "#FCF259",
        "accent_hover": _lighter("#FCF259", 106),
        "accent_pressed": _darker("#FCF259", 112),
        "accent_text": "#4A102A",
        "success": "#3DDC84",
        "warning": "#FFC24B",
        "danger": "#C5172E",
        "danger_hover": _lighter("#C5172E", 118),
        "shadow": QColor(0, 0, 0, 165),
    },
    # Cream / tan / warm brown / dark brown (the one light custom theme).
    "cafe_latte": {
        "background": "#FFF8F0",
        "surface": "#FFFFFF",
        "card": "#FFFFFF",
        "card_hover": "#FBF3EA",
        "border": "#EAD9C4",
        "text": "#4B2E2B",
        "text_muted": "#8C5A3C",
        "accent": "#C08552",
        "accent_hover": _lighter("#C08552", 110),
        "accent_pressed": _darker("#C08552", 112),
        "accent_text": "#FFF8F0",
        "success": "#1FAA63",
        "warning": "#B9791B",
        "danger": "#E4483F",
        "danger_hover": "#F05B52",
        "shadow": QColor(60, 35, 20, 40),
    },
}

#: Display order and labels for the theme picker menu.
THEME_ORDER: list[tuple[str, str]] = [
    ("dark", "Dark"),
    ("light", "Light"),
    ("ember_noir", "Ember Noir"),
    ("deep_tide", "Deep Tide"),
    ("cobalt_volt", "Cobalt Volt"),
    ("crimson_bloom", "Crimson Bloom"),
    ("cafe_latte", "Cafe Latte"),
]

#: Theme applied on first launch.
DEFAULT_THEME = "ember_noir"


def _swatch_icon(color_hex: str, size: int = 14) -> QIcon:
    """Render a small filled circle icon used to preview a theme's accent color.

    Args:
        color_hex: The color to fill the swatch with.
        size: Diameter of the swatch in pixels.

    Returns:
        A ``QIcon`` suitable for use on a ``QAction``.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color_hex))
    painter.drawEllipse(0, 0, size - 1, size - 1)
    painter.end()

    return QIcon(pixmap)


def build_stylesheet(p: dict) -> str:
    """Build the application-wide QSS stylesheet for a given palette.

    Args:
        p: A theme dict from ``THEMES``.

    Returns:
        A QSS string applied to the main window and inherited by children.
    """
    return f"""
        QWidget#MainWindow {{
            background-color: {p['background']};
        }}

        QScrollArea {{
            border: none;
            background: transparent;
        }}

        QWidget#ScrollContent {{
            background: transparent;
        }}

        QLabel {{
            color: {p['text']};
            background: transparent;
        }}

        QLabel#AppTitle {{
            font-size: 21px;
            font-weight: 700;
            letter-spacing: -0.3px;
        }}

        QLabel#AppSubtitle {{
            font-size: 12px;
            color: {p['text_muted']};
        }}

        QPushButton#ThemeToggle {{
            background-color: {p['surface']};
            border: 1px solid {p['border']};
            border-radius: 10px;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 600;
            color: {p['text']};
        }}

        QPushButton#ThemeToggle:hover {{
            background-color: {p['card_hover']};
        }}

        QPushButton#ThemeToggle::menu-indicator {{
            width: 0px;
        }}

        QMenu {{
            background-color: {p['card']};
            color: {p['text']};
            border: 1px solid {p['border']};
            border-radius: 10px;
            padding: 6px;
        }}

        QMenu::item {{
            padding: 7px 12px;
            border-radius: 6px;
        }}

        QMenu::item:selected {{
            background-color: {p['card_hover']};
        }}

        QMenu::icon {{
            padding-left: 4px;
        }}

        QFrame#Card {{
            background-color: {p['card']};
            border: 1px solid {p['border']};
            border-radius: 16px;
        }}

        QLabel#CardTitle {{
            font-size: 11px;
            font-weight: 700;
            color: {p['text_muted']};
            letter-spacing: 1.2px;
        }}

        QLabel#StatusText {{
            font-size: 13px;
            font-weight: 500;
        }}

        QLabel#BigCoords {{
            font-size: 26px;
            font-weight: 600;
            letter-spacing: -0.5px;
        }}

        QLabel#PositionLabel {{
            font-size: 14px;
            color: {p['text_muted']};
        }}

        QLabel#SliderValue {{
            font-size: 13px;
            font-weight: 700;
            color: {p['accent']};
        }}

        QPushButton {{
            background-color: {p['accent']};
            color: {p['accent_text']};
            border: none;
            border-radius: 10px;
            padding: 11px 16px;
            font-size: 13px;
            font-weight: 600;
        }}

        QPushButton:hover {{
            background-color: {p['accent_hover']};
        }}

        QPushButton:pressed {{
            background-color: {p['accent_pressed']};
        }}

        QPushButton#SecondaryButton {{
            background-color: {p['surface']};
            color: {p['text']};
            border: 1px solid {p['border']};
        }}

        QPushButton#SecondaryButton:hover {{
            background-color: {p['card_hover']};
        }}

        QPushButton#DangerButton {{
            background-color: transparent;
            color: {p['danger']};
            border: 1px solid {p['danger']};
        }}

        QPushButton#DangerButton:hover {{
            background-color: {p['danger']};
            color: white;
        }}

        QSlider::groove:horizontal {{
            height: 6px;
            background: {p['surface']};
            border: 1px solid {p['border']};
            border-radius: 3px;
        }}

        QSlider::sub-page:horizontal {{
            background: {p['accent']};
            border-radius: 3px;
        }}

        QSlider::handle:horizontal {{
            background: white;
            border: 1px solid {p['border']};
            width: 16px;
            height: 16px;
            margin: -6px 0;
            border-radius: 8px;
        }}

        QMessageBox {{
            background-color: {p['card']};
        }}
    """


class StatusIndicator(QWidget):
    """A small "dot + label" widget used to show a live status.

    Used for the running/paused state and the ESP32 connection state.
    Call :meth:`set_state` whenever the underlying status changes.
    """

    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        """Initialize the indicator.

        Args:
            text: Initial label text.
            color: Initial dot color as a CSS color string.
            parent: Optional parent widget.
        """
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self._dot = QLabel()
        self._dot.setFixedSize(9, 9)

        self._label = QLabel()
        self._label.setObjectName("StatusText")

        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch(1)

        self.set_state(text, color)

    def set_state(self, text: str, color: str) -> None:
        """Update the indicator's text and dot color.

        Args:
            text: New label text.
            color: New dot color as a CSS color string.
        """
        self._label.setText(text)
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 4px;"
        )


class SectionCard(QFrame):
    """A titled, rounded, drop-shadowed container used to group controls.

    Widgets belonging to a section should be added to :attr:`body`
    (the card's inner ``QVBoxLayout``) rather than to the card directly.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        """Initialize the card.

        Args:
            title: Section title shown at the top of the card.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(18, 16, 18, 16)
        self._body.setSpacing(10)

        if title:
            title_label = QLabel(title.upper())
            title_label.setObjectName("CardTitle")
            self._body.addWidget(title_label)

        self._shadow = self._apply_shadow()

    @property
    def body(self) -> QVBoxLayout:
        """The layout new content should be added to."""
        return self._body

    def _apply_shadow(self) -> QGraphicsDropShadowEffect:
        """Give the card a soft drop shadow for a modern, elevated look."""
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)
        return shadow

    def set_shadow_color(self, color: QColor) -> None:
        """Update the shadow color/opacity to suit the active theme.

        Args:
            color: The new shadow color (including alpha).
        """
        self._shadow.setColor(color)


class MainWindow(QWidget):
    """Main application window: a clean, minimalist macro control panel."""

    #: Bounds and step size for the scroll-speed slider.
    SCROLL_MIN = 100
    SCROLL_MAX = 2000
    SCROLL_STEP = 100

    def __init__(self, config: dict, esp) -> None:
        """Build the window.

        Args:
            config: Mutable application configuration dictionary.
            esp: The ESP32 serial handler, exposing ``connected`` and
                ``port`` attributes used to render connection status.
        """
        super().__init__()

        self.config = config
        self.esp = esp

        # Theme state lives only in the GUI layer; it does not touch
        # ``config`` so the backend/config schema is left untouched.
        self._theme_key = DEFAULT_THEME
        self._cards: list[SectionCard] = []
        self._theme_actions: dict[str, QAction] = {}

        self.setObjectName("MainWindow")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(480, 480)
        self.setFont(QFont("Segoe UI", 10))

        self._build_ui()
        self._apply_theme()
        self._start_timers()

        # Start as a square window; the scroll area inside still lets the
        # user grow it (including taller/non-square) to see everything
        # comfortably without clipping any content.
        self.resize(600, 600)

    # ====================================================
    # UI CONSTRUCTION
    # ====================================================

    def _build_ui(self) -> None:
        """Assemble the header, scroll area, and all section cards."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        content.setObjectName("ScrollContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(14)

        content_layout.addLayout(self._build_header())
        content_layout.addWidget(self._register(self._build_status_card()))
        content_layout.addWidget(self._register(self._build_mouse_card()))
        content_layout.addWidget(self._register(self._build_position_card()))
        content_layout.addWidget(self._register(self._build_scroll_card()))
        content_layout.addWidget(self._register(self._build_test_actions_card()))
        content_layout.addLayout(self._build_bottom_bar())
        content_layout.addStretch(1)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(content)
        outer.addWidget(scroll_area)

    def _register(self, card: SectionCard) -> SectionCard:
        """Track a card so its shadow color can be updated on theme changes.

        Args:
            card: The section card to track.

        Returns:
            The same card, for convenient chaining inside ``addWidget``.
        """
        self._cards.append(card)
        return card

    def _build_header(self) -> QHBoxLayout:
        """Build the app title/subtitle header with a theme picker button."""
        header = QHBoxLayout()
        header.setSpacing(12)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)

        self.logo_label = QLabel()
        icon = load_app_icon()
        if not icon.isNull():
            self.logo_label.setPixmap(icon.pixmap(24, 24))
            title_row.addWidget(self.logo_label)

        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)

        subtitle = QLabel("DadPad - The Ultimate Desktop Automation Controller made with love ❤️")
        subtitle.setObjectName("AppSubtitle")

        title_block.addLayout(title_row)
        title_block.addWidget(subtitle)

        self.theme_button = QPushButton("Theme")
        self.theme_button.setObjectName("ThemeToggle")
        self.theme_button.setCursor(Qt.PointingHandCursor)
        self.theme_button.setToolTip("Choose a color theme")
        self.theme_button.setMenu(self._build_theme_menu())

        header.addLayout(title_block)
        header.addStretch(1)
        header.addWidget(self.theme_button, 0, Qt.AlignTop)
        return header

    def _build_theme_menu(self) -> QMenu:
        """Build the dropdown menu listing every available theme.

        Each entry shows a small color swatch (that theme's accent
        color) and is checkable, with only the active theme checked.
        """
        menu = QMenu(self)

        group = QActionGroup(menu)
        group.setExclusive(True)

        for key, label in THEME_ORDER:
            action = QAction(_swatch_icon(THEMES[key]["accent"]), label, menu)
            action.setCheckable(True)
            action.setChecked(key == self._theme_key)
            action.triggered.connect(lambda checked=False, k=key: self._set_theme(k))
            group.addAction(action)
            menu.addAction(action)
            self._theme_actions[key] = action

        return menu

    def _build_status_card(self) -> SectionCard:
        """Build the card showing running/paused and ESP32 connection state."""
        card = SectionCard("Status")

        self.status_indicator = StatusIndicator("Running", THEMES[DEFAULT_THEME]["success"])
        self.esp_indicator = StatusIndicator(
            "ESP32 Disconnected", THEMES[DEFAULT_THEME]["danger"]
        )

        card.body.addWidget(self.status_indicator)
        card.body.addWidget(self.esp_indicator)
        return card

    def _build_mouse_card(self) -> SectionCard:
        """Build the card showing the live, continuously updating cursor position."""
        card = SectionCard("Live Mouse Position")

        self.live_coords_label = QLabel("(0, 0)")
        self.live_coords_label.setObjectName("BigCoords")

        card.body.addWidget(self.live_coords_label)
        return card

    def _build_position_card(self) -> SectionCard:
        """Build the card showing/capturing the saved click position."""
        card = SectionCard("Saved Click Position")

        self.position_label = QLabel()
        self.position_label.setObjectName("PositionLabel")

        capture_button = QPushButton("Capture Mouse Position  (Ctrl+Shift+C)")
        capture_button.setCursor(Qt.PointingHandCursor)
        capture_button.clicked.connect(self.capture)

        card.body.addWidget(self.position_label)
        card.body.addWidget(capture_button)

        self.update_position_label()
        return card

    def _build_scroll_card(self) -> SectionCard:
        """Build the card containing the scroll-speed slider."""
        card = SectionCard("Scroll Speed")

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Scroll amount"))
        header_row.addStretch(1)

        self.scroll_value_label = QLabel()
        self.scroll_value_label.setObjectName("SliderValue")
        header_row.addWidget(self.scroll_value_label)

        self.scroll_slider = QSlider(Qt.Horizontal)
        self.scroll_slider.setCursor(Qt.PointingHandCursor)
        self.scroll_slider.setRange(self.SCROLL_MIN, self.SCROLL_MAX)
        self.scroll_slider.setSingleStep(self.SCROLL_STEP)
        self.scroll_slider.setPageStep(self.SCROLL_STEP)
        self.scroll_slider.setTickInterval(self.SCROLL_STEP)
        self.scroll_slider.setValue(self.config["scroll_amount"])
        self.scroll_slider.valueChanged.connect(self._on_scroll_slider_changed)

        card.body.addLayout(header_row)
        card.body.addWidget(self.scroll_slider)

        self.update_scroll_label()
        return card

    def _build_test_actions_card(self) -> SectionCard:
        """Build the card with buttons that fire actions immediately, for testing."""
        card = SectionCard("Test Actions")

        row = QHBoxLayout()
        row.setSpacing(8)

        click_button = QPushButton("Click (F8)")
        click_button.setObjectName("SecondaryButton")
        click_button.setCursor(Qt.PointingHandCursor)
        click_button.clicked.connect(lambda: actions.click(self.config))

        up_button = QPushButton("Scroll Up (F9)")
        up_button.setObjectName("SecondaryButton")
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda: actions.scroll_up(self.config))

        down_button = QPushButton("Scroll Down (F10)")
        down_button.setObjectName("SecondaryButton")
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda: actions.scroll_down(self.config))

        for button in (click_button, up_button, down_button):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row.addWidget(button)

        card.body.addLayout(row)
        return card

    def _build_bottom_bar(self) -> QHBoxLayout:
        """Build the bottom row of pause/resume, save, and exit buttons."""
        row = QHBoxLayout()
        row.setSpacing(8)

        pause_button = QPushButton("Pause / Resume")
        pause_button.setObjectName("SecondaryButton")
        pause_button.setCursor(Qt.PointingHandCursor)
        pause_button.clicked.connect(self.toggle_pause)

        save_button = QPushButton("Save")
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self.save)

        exit_button = QPushButton("Exit")
        exit_button.setObjectName("DangerButton")
        exit_button.setCursor(Qt.PointingHandCursor)
        exit_button.clicked.connect(self.close)

        for button in (pause_button, save_button, exit_button):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row.addWidget(button)

        return row

    # ====================================================
    # TIMERS
    # ====================================================

    def _start_timers(self) -> None:
        """Start the timers that keep the mouse position and status live."""
        self.mouse_timer = QTimer(self)
        self.mouse_timer.timeout.connect(self.update_mouse)
        self.mouse_timer.start(50)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)

    # ====================================================
    # THEME
    # ====================================================

    def _active_palette(self) -> dict:
        """Return the currently active theme dict."""
        return THEMES[self._theme_key]

    def _set_theme(self, key: str) -> None:
        """Switch to a different theme by key and re-render.

        Args:
            key: One of the keys in ``THEMES`` (see ``THEME_ORDER``).
        """
        if key not in THEMES or key == self._theme_key:
            return
        self._theme_key = key
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Apply the active palette's stylesheet and shadow colors."""
        palette = self._active_palette()
        self.setStyleSheet(build_stylesheet(palette))

        for card in self._cards:
            card.set_shadow_color(palette["shadow"])

        for key, action in self._theme_actions.items():
            action.setChecked(key == self._theme_key)

        # Re-run status refreshes so indicator dot colors match the new theme.
        self._refresh_run_status()
        self.update_status()

    # ====================================================
    # ACTIONS / EVENT HANDLERS
    # ====================================================

    def capture(self) -> None:
        """Capture the current cursor position and refresh the label."""
        x, y = actions.capture_position(self.config)
        self.update_position_label()

    def toggle_pause(self) -> None:
        """Toggle the global paused flag and refresh the status indicator."""
        actions.paused = not actions.paused
        self._refresh_run_status()

    def _on_scroll_slider_changed(self, value: int) -> None:
        """Handle slider movement by writing the new value into config.

        Args:
            value: The slider's current value.
        """
        self.config["scroll_amount"] = value
        self.update_scroll_label()

    # ====================================================
    # LABEL / INDICATOR REFRESH
    # ====================================================

    def update_position_label(self) -> None:
        """Refresh the saved click position label from ``self.config``.

        Kept as a public method with this exact name for compatibility
        with callers outside this module (e.g. the capture hotkey handler
        in ``main.py``).
        """
        x = self.config["click_position"]["x"]
        y = self.config["click_position"]["y"]
        self.position_label.setText(f"X: {x}    Y: {y}")

    def update_scroll_label(self) -> None:
        """Refresh the scroll-speed value label from ``self.config``."""
        self.scroll_value_label.setText(str(self.config["scroll_amount"]))

    def update_mouse(self) -> None:
        """Poll and display the current, live cursor position."""
        x, y = pyautogui.position()
        self.live_coords_label.setText(f"({x}, {y})")

    def update_status(self) -> None:
        """Refresh the ESP32 connection and running/paused indicators."""
        palette = self._active_palette()
        if self.esp.connected:
            self.esp_indicator.set_state(
                f"ESP32 Connected ({self.esp.port})", palette["success"]
            )
        else:
            self.esp_indicator.set_state("ESP32 Disconnected", palette["danger"])

        self._refresh_run_status()

    def _refresh_run_status(self) -> None:
        """Update the running/paused status indicator from ``actions.paused``."""
        palette = self._active_palette()
        if actions.paused:
            self.status_indicator.set_state("Paused", palette["warning"])
        else:
            self.status_indicator.set_state("Running", palette["success"])

    # ====================================================
    # SAVE
    # ====================================================

    def save(self) -> None:
        """Persist the current configuration to disk and confirm to the user."""
        save_config(self.config)
        QMessageBox.information(
            self,
            "Saved",
            "Configuration saved successfully.",
        )