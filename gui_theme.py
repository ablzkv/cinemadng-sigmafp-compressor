"""Single source of truth for all GUI design tokens.

Phase 1 foundation: colors, fonts, sizes, spacing. No other GUI file may
hardcode colors — import from here. Backend logic must never import this.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
APP_BG = "#0B1220"
SIDEBAR_BG = "#0F1729"
CARD_BG = "#131C2F"
CARD_BORDER = "#22304A"
INPUT_BG = "#0C1424"
INPUT_BORDER = "#2A3A57"
FOCUS_RING = "#3B82F6"
PRESET_FILL = "#14284D"
PRESET_BORDER = "#3B82F6"
TEXT = "#E8EDF7"
MUTED = "#93A0BA"
DIM = "#66738F"
ACCENT = "#3B82F6"
ACCENT_HOVER = "#2F6FE0"
ACCENT_PRESSED = "#2559BD"
DISABLED_BG = "#1B2740"
DISABLED_TEXT = "#5B6885"
SUCCESS = "#34D399"
WARNING = "#FBBF24"
ERROR = "#F87171"
PILL_GREEN_BG = "#0F2E2A"
PILL_AMBER_BG = "#33270F"
PILL_RED_BG = "#3A1D24"
PILL_NEUTRAL_BG = "#1C2740"
LOG_BG = "#0C1424"
LOG_TEXT = "#C9D4E3"
TEXT_ON_ACCENT = "#FFFFFF"
TOOLTIP_BG = "#0D1320"
SWITCH_OFF = "#3A4A63"
TREEVIEW_SELECTED = "#274268"
TREEVIEW_ALT = "#101A2B"

# ---------------------------------------------------------------------------
# Fonts (family, size, weight)
# ---------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"
TITLE = (FONT_FAMILY, 24, "bold")
TAGLINE = (FONT_FAMILY, 12, "normal")
SECTION = (FONT_FAMILY, 11, "bold")
CARD_TITLE = (FONT_FAMILY, 12, "bold")
PERCENT = (FONT_FAMILY, 20, "bold")
BODY = (FONT_FAMILY, 10, "normal")
CAPTION = (FONT_FAMILY, 9, "normal")
TOOLTIP = (FONT_FAMILY, 9, "normal")
BADGE = (FONT_FAMILY, 10, "bold")
TAG = (FONT_FAMILY, 8, "bold")
PRIMARY_BUTTON = (FONT_FAMILY, 12, "bold")
LOG_FONT_OPTIONS = ("Cascadia Mono", "Consolas")
MONO_FALLBACK = "TkFixedFont"
LOG_SIZE = 9

# ---------------------------------------------------------------------------
# Spacing (8px grid) and sizes
# ---------------------------------------------------------------------------
CARD_PADDING = 10
CARD_GAP = 8
SIDE_PADDING = 6
CONTENT_MAX_WIDTH = 1840
INPUT_HEIGHT = 34
PRIMARY_BUTTON_HEIGHT = 40
SECONDARY_BUTTON_HEIGHT = 32
RADIUS_CARD = 9
RADIUS_CONTROL = 7

# Backwards-compatible aliases for the pre-polish constant names.
BG = APP_BG
PANEL = CARD_BG
PANEL_BORDER = CARD_BORDER
ENTRY_BG = INPUT_BG
TEXT_ALIAS = TEXT
MUTED_ALIAS = MUTED
ACCENT_ALIAS = ACCENT
GREEN = SUCCESS
AMBER = WARNING
RED = ERROR
CARD_SELECTED_BORDER = PRESET_BORDER


def section_label(text: str) -> str:
    """Section labels render small, uppercase and muted (callers apply font)."""
    return text.upper()


def apply_dpi_awareness() -> None:
    """Per-monitor DPI awareness before the root window exists. Never raises."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def enable_dark_titlebar(window) -> bool:
    """Dark Windows title bar via DwmSetWindowAttribute. Never raises."""
    try:
        import ctypes
        value = ctypes.c_int(1)
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.windll.user32.GetParent(window.winfo_id()), 20,
                ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.windll.user32.GetParent(window.winfo_id()), 19,
                ctypes.byref(value), ctypes.sizeof(value))
        return True
    except Exception:
        return False


# Glyphs from the stable Segoe MDL2 set (shared with Segoe Fluent Icons).
# Every icon use keeps its text label, so a missing glyph degrades gracefully.
_GLYPHS = {
    "play": "\uE768",
    "stop": "\uE71A",
    "folder": "\uE8B7",
    "browse": "\uE8B7",
    "check": "\uE73E",
    "check_circle": "\uE73E",
    "warning": "\uE7BA",
    "error": "\uE783",
    "info": "\uE946",
    "settings": "\uE713",
    "home": "\uE80F",
    "list": "\uE8FD",
    "document": "\uE8A5",
    "chart": "\uE9D9",
    "refresh": "\uE72C",
    "add": "\uE710",
    "remove": "\uE738",
    "trash": "\uE74D",
    "chevron_down": "\uE70D",
    "shield": "\uEA18",
    "repair": "\uE90F",
    "star": "\uE734",
    "bolt": "\uE945",
    "open": "\uE8E5",
    "bundle": "\uE8C7",
    "dry_run": "\uE8A5",
    "recommend": "\uE945",
}


def icon_font_family(widget) -> str:
    """Best available icon font; falls back to plain text rendering."""
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families(widget))
        if "Segoe Fluent Icons" in families:
            return "Segoe Fluent Icons"
        if "Segoe MDL2 Assets" in families:
            return "Segoe MDL2 Assets"
    except Exception:
        pass
    return FONT_FAMILY


def icon(name: str) -> str:
    """Icon glyph by name, or empty string when unknown (text labels remain)."""
    return _GLYPHS.get(name, "")
