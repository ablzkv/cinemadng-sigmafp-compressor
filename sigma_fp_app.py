# -*- coding: utf-8 -*-
"""Dark, professional Windows front end for the Sigma fp CinemaDNG MVP.

UX redesign of the original form-style GUI. All backend behavior is preserved:
same CLI arguments, same safety guards, same subprocess handling, same queue
semantics and fail-closed rules. Only presentation + threading changed:
conversion progress follows the converter output.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_widget_scaling(1.0)

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "python.exe"
CONVERTER = ROOT / "cdng_lossless_mvp.py"

# ---------------------------------------------------------------------------
# Palette (dark professional video-utility aesthetic)
# ---------------------------------------------------------------------------
# All design tokens live in gui_theme.py (Phase 1 foundation). Import names
# are kept identical so no other code changes are needed.
from gui_theme import (
    APP_BG as BG,
    CARD_BG as PANEL,
    CARD_BORDER as PANEL_BORDER,
    TEXT,
    MUTED,
    ACCENT,
    ACCENT_PRESSED as ACCENT_DARK,
    SUCCESS as GREEN,
    WARNING as AMBER,
    ERROR as RED,
    INPUT_BG as ENTRY_BG,
)
import gui_theme

_PRESET_DETAILS = {
    "Lossless": ("LOSSLESS", "12-bit  •  RAW-exact",
                 "Variable ratio; integrity is absolute", "ARCHIVAL / RAW-EXACT"),
    "High Quality": ("HIGH QUALITY", "12-bit  •  quality 90",
                      "Target approximately 3:1–4:1", "LOSSY / EDITING"),
    "Compact": ("COMPACT", "12-bit  •  quality 82",
                "Target approximately 4:1–6:1", "LOSSY / EDITING"),
}


def combined_progress_percent(processed_frames: float, eligible_frames: int) -> float:
    """Show progress for the frames being converted."""
    return 100.0 * min(1.0, max(0.0, processed_frames / eligible_frames)) if eligible_frames else 0.0


class _PresetCancelled(Exception):
    """Raised when a user cancels a mode-specific confirmation."""
    pass


sys.path.insert(0, str(ROOT))


class ToolTip:
    """Short hover tooltip for technical controls."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tip, text=self.text, background=gui_theme.TOOLTIP_BG, foreground=TEXT,
                         font=gui_theme.TOOLTIP, wraplength=320, justify="left",
                         borderwidth=1, relief="solid", padx=8, pady=6)
        label.pack()

    def _hide(self, _event=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class ProgressBar(ctk.CTkProgressBar):
    """CTkProgressBar with the ttk-style configure()/[] API the pump uses.

    Accepts maximum/value/mode kwargs; indeterminate start()/stop() pass
    through. Tracks value internally for the completion check.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._maximum = 100
        self._value = 0
        self._pmode = "determinate"
        self.set(0)

    def start(self, *args, **kwargs):
        super().start()

    def stop(self, *args, **kwargs):
        super().stop()

    def configure(self, *args, **kwargs):
        require = kwargs.pop("require_redraw", None)  # ignore ctk-only passthroughs
        if "maximum" in kwargs:
            self._maximum = max(1, kwargs.pop("maximum"))
        if "mode" in kwargs:
            self._pmode = kwargs.pop("mode")
        if "value" in kwargs:
            self._value = kwargs.pop("value")
            if self._pmode == "determinate":
                self.set(min(1.0, max(0.0, self._value / self._maximum)))
        if args or kwargs:
            super().configure(*args, **kwargs)

    config = configure

    def __getitem__(self, key):
        if key == "maximum":
            return self._maximum
        if key == "value":
            return self._value
        return super().cget(key)

    def cget(self, key):
        if key == "maximum":
            return self._maximum
        if key == "value":
            return self._value
        return super().cget(key)


def _section_row(parent) -> int:
    """Auto-incrementing grid row for stacked section cards."""
    parent._sec_row = getattr(parent, "_sec_row", 0) + 1
    return parent._sec_row


class SigmaFpApp(tk.Tk):
    def __init__(self):
        super().__init__()
        gui_theme.enable_dark_titlebar(self)
        self.title("Sigma fp CinemaDNG Compressor")
        self.geometry("1536x950")
        self.minsize(1080, 720)
        self.events: queue.Queue[str] = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.started = 0.0
        self.frames_done = 0
        self.frames_total = 0
        self._fps_samples: list = []
        self.current_file = tk.StringVar(value="")
        self.current_footage = tk.StringVar(value="")
        self.current_frame = tk.StringVar(value="")
        self._progress_root: str | None = None
        self._progress_footages: dict[str, dict] = {}
        self._progress_eligible_frames = 0
        self._progress_processed_frames = 0.0
        self._progress_active_footage: str | None = None
        self._progress_incomplete = False
        # Default run state: High Quality, sampled verification, small tiles.
        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.preset = tk.StringVar(value="High Quality")
        self.workers = tk.StringVar(value="auto")
        self.verify_mode = tk.StringVar(value="sampled")
        self.tile_layout = tk.StringVar(value="small")
        self.strict_guardrails = tk.BooleanVar(value=False)
        self.allow_low_space = tk.BooleanVar(value=False)
        self.resume = tk.BooleanVar(value=False)
        self.keep_output_artifacts = tk.BooleanVar(value=False)
        self.include_subfolders = tk.BooleanVar(value=True)
        self.allow_existing = tk.BooleanVar(value=False)
        self.overwrite_text = tk.StringVar()
        self.continue_on_error = tk.BooleanVar(value=True)
        self.batch: list[dict] = []
        self.batch_active = False
        self._batch_running: dict | None = None
        self._batch_scope: set[int] | None = None
        self._stop_requested = False
        self.last_destination: Path | None = None
        self._active_session_id = ""
        self._last_session_artifact_dir: Path | None = None
        self._resume_existing_output = False
        self._resume_destination: Path | None = None
        self.status = tk.StringVar(value="Ready")
        self.metrics = tk.StringVar(value="—")
        self.footer = tk.StringVar(value="Ready")
        self._recommend_dialog = None
        self._last_recommend: dict | None = None
        self._preset_cards: dict[str, ttk.Frame] = {}
        self._config_widgets: list = []
        self._build_theme()
        self._build()
        self._refresh_preset_cards()
        self.after(100, self._drain_events)

    # ------------------------------------------------------------------
    # Theme + layout
    # ------------------------------------------------------------------
    def _build_theme(self):
        th = gui_theme
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.configure(background=th.APP_BG)
        style.configure(".", background=th.APP_BG, foreground=th.TEXT, font=th.BODY)
        style.configure("TFrame", background=th.APP_BG)
        style.configure("Panel.TFrame", background=th.CARD_BG)
        style.configure("Card.TFrame", background=th.CARD_BG, borderwidth=1, relief="solid",
                        bordercolor=th.CARD_BORDER)
        style.configure("TLabel", background=th.APP_BG, foreground=th.TEXT)
        style.configure("Panel.TLabel", background=th.CARD_BG, foreground=th.TEXT)
        style.configure("Muted.TLabel", background=th.APP_BG, foreground=th.MUTED, font=th.CAPTION)
        style.configure("MutedPanel.TLabel", background=th.CARD_BG, foreground=th.MUTED, font=th.CAPTION)
        style.configure("Header.TLabel", background=th.APP_BG, foreground=th.TEXT, font=th.TITLE)
        style.configure("Subheader.TLabel", background=th.APP_BG, foreground=th.MUTED, font=th.TAGLINE)
        style.configure("Section.TLabel", background=th.CARD_BG, foreground=th.MUTED, font=th.SECTION)
        style.configure("PrimarySection.TLabel", background=th.CARD_BG, foreground=th.TEXT, font=th.SECTION)
        style.configure("CardTitle.TLabel", background=th.CARD_BG, foreground=th.TEXT, font=th.CARD_TITLE)
        style.configure("TButton", background=th.INPUT_BORDER, foreground=th.TEXT, borderwidth=1,
                        focusthickness=0, padding=(12, 8))
        style.configure("Compact.TButton", background=th.INPUT_BG, foreground=th.TEXT,
                        borderwidth=1, focusthickness=0, padding=(8, 4), font=th.BODY)
        style.map("Compact.TButton", background=[("active", th.INPUT_BORDER), ("disabled", th.DISABLED_BG)],
                  foreground=[("disabled", th.DISABLED_TEXT)])
        style.map("TButton", background=[("active", th.ACCENT_HOVER), ("disabled", th.DISABLED_BG)],
                  foreground=[("disabled", th.DISABLED_TEXT)])
        style.configure("Accent.TButton", background=th.ACCENT, foreground=th.TEXT_ON_ACCENT, font=th.PRIMARY_BUTTON,
                        padding=(16, 10))
        style.map("Accent.TButton", background=[("active", th.ACCENT_HOVER), ("pressed", th.ACCENT_PRESSED),
                                                ("disabled", th.DISABLED_BG)],
                  foreground=[("disabled", th.DISABLED_TEXT)])
        style.configure("TEntry", fieldbackground=th.INPUT_BG, foreground=th.TEXT, borderwidth=1,
                        insertcolor=th.TEXT)
        style.configure("TCombobox", fieldbackground=th.INPUT_BG, background=th.INPUT_BORDER,
                        foreground=th.TEXT, arrowcolor=th.MUTED)
        style.map("TCombobox", fieldbackground=[("readonly", th.INPUT_BG)],
                  foreground=[("readonly", th.TEXT)])
        try:
            self.option_add("*TCombobox*Listbox.background", th.INPUT_BG)
            self.option_add("*TCombobox*Listbox.foreground", th.TEXT)
            self.option_add("*TCombobox*Listbox.selectBackground", th.ACCENT)
            self.option_add("*TCombobox*Listbox.selectForeground", th.TEXT_ON_ACCENT)
            self.option_add("*TCombobox*Listbox.font", f"{{{th.FONT_FAMILY}}} {th.BODY[1]}")
        except Exception:
            pass
        style.configure("TCheckbutton", background=th.CARD_BG, foreground=th.TEXT)
        style.configure("TProgressbar", background=th.ACCENT, troughcolor=th.APP_BG, borderwidth=0)
        style.configure("Treeview", background=th.INPUT_BG, fieldbackground=th.INPUT_BG,
                        foreground=th.TEXT, borderwidth=0, font=th.BODY, rowheight=24)
        style.configure("Treeview.Heading", background=th.INPUT_BORDER, foreground=th.TEXT,
                        font=(th.FONT_FAMILY, th.BODY[1], "bold"), relief="flat", padding=(6, 4))
        style.configure("Percent.TLabel", background=th.CARD_BG, foreground=th.TEXT, font=th.PERCENT)
        style.map("Treeview", background=[("selected", th.TREEVIEW_SELECTED)])
        style.configure("Card.TRadiobutton", background=th.CARD_BG, foreground=th.TEXT)
        for element in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(element, background=th.CARD_BG, troughcolor=th.APP_BG,
                            bordercolor=th.APP_BG, arrowcolor=th.MUTED)
            style.map(element, background=[("active", th.INPUT_BORDER)])

    def _build(self):
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        self._anchors: dict[str, object] = {}
        self._build_header(shell)
        body = ttk.Frame(shell)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas)
        self.content.columnconfigure(0, weight=72, uniform="dashboard")
        self.content.columnconfigure(1, weight=28, uniform="dashboard")
        self._content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._fit_content)
        try:
            self.canvas.bind_all("<MouseWheel>", self._mousewheel)
        except Exception:
            pass

        self.left_column = ttk.Frame(self.content)
        self.left_column.grid(row=0, column=0, sticky="nsew")
        self.right_column = ttk.Frame(self.content)
        self.right_column.grid(row=0, column=1, sticky="nsew")
        for column in (self.left_column, self.right_column):
            column.columnconfigure(0, weight=1)

        self._build_folders(self.left_column)
        self._build_presets(self.left_column)
        self._build_actions(self.left_column)
        self._build_progress(self.left_column)
        self._build_queue(self.left_column)
        self._build_processing_options(self.right_column)
        self._build_analysis_recommendation(self.right_column)
        self._build_reports(self.right_column)
        self._relayout_dashboard()

        footer = ttk.Frame(self, style="Panel.TFrame", padding=(12, 2), height=30)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        ttk.Label(footer, textvariable=self.footer, style="MutedPanel.TLabel").pack(side="left", padx=12)
        ttk.Label(footer, text="Sigma fp CinemaDNG Compressor", style="MutedPanel.TLabel").pack(side="right", padx=12)
        self._set_footer_default()

    def _set_nav_active(self, target: str):
        for name, button in self._nav_items.items():
            active = name == target
            try:
                button.configure(
                    background=gui_theme.PRESET_FILL if active else gui_theme.SIDEBAR_BG,
                    foreground=gui_theme.TEXT if active else gui_theme.MUTED,
                    highlightbackground=gui_theme.ACCENT if active else gui_theme.CARD_BORDER)
            except Exception:
                pass

    def _fit_content(self, event):
        max_w = gui_theme.CONTENT_MAX_WIDTH + 2 * gui_theme.SIDE_PADDING
        width = min(event.width, max_w)
        x = max(0, (event.width - width) // 2)
        self.canvas.itemconfigure(self._content_window, width=width)
        try:
            self.canvas.coords(self._content_window, x, 0)
        except Exception:
            pass
        stacked = event.width < 1400
        self._relayout_dashboard(stacked)
        left_width = event.width if stacked else event.width * 0.72
        self._relayout_folders(left_width < 760)

    def _relayout_dashboard(self, stacked: bool | None = None):
        if stacked is None:
            stacked = self.canvas.winfo_width() < 1400
        self.right_column.grid_forget()
        if stacked:
            self.content.columnconfigure(0, weight=1, minsize=0)
            self.content.columnconfigure(1, weight=0, minsize=0)
            self.right_column.grid(row=1, column=0, sticky="nsew")
        else:
            self.content.columnconfigure(0, weight=72, uniform="dashboard")
            self.content.columnconfigure(1, weight=28, uniform="dashboard")
            self.right_column.grid(row=0, column=1, sticky="nsew")

    def _mousewheel(self, event):
        try:
            delta = int(-event.delta / 120) if event.delta else 0
            self.canvas.yview_scroll(delta, "units")
        except Exception:
            pass

    def _scroll_to(self, name: str):
        widget = self._anchors.get(name)
        self._set_nav_active(name)
        if name == "sec_logs":
            # Logs live in the progress card's Details area: make sure both
            # are visible before scrolling (presentation only, no log content change).
            self._ensure_progress()
            if not self._details_visible:
                self._toggle_details()
        if widget is None:
            return
        try:
            self.update_idletasks()
            y = 0
            node = widget
            while node is not None and node is not self.content:
                y += node.winfo_y()
                node = node.master
            self.canvas.yview_moveto(max(0.0, min(1.0, y / max(1, self.content.winfo_height()))))
        except Exception:
            pass

    def _section(self, parent, title: str, anchor: str | None = None) -> ttk.Frame:
        number, label = "", title
        if len(title) > 2 and title[0].isdigit() and title[1] == " ":
            number, label = title[0], title[2:]
        primary = not number or number in {"1", "2", "3", "4"}
        card = ctk.CTkFrame(parent, fg_color=gui_theme.CARD_BG, border_width=1 if primary else 0,
                            border_color=gui_theme.CARD_BORDER, corner_radius=gui_theme.RADIUS_CARD)
        card.grid(row=_section_row(parent), column=0, sticky="ew", padx=gui_theme.SIDE_PADDING,
                  pady=gui_theme.CARD_GAP // 2)
        card.columnconfigure(0, weight=1)
        inner = ttk.Frame(card, style="Panel.TFrame", padding=gui_theme.CARD_PADDING if primary else 9)
        inner.grid(row=0, column=0, sticky="ew")
        inner.columnconfigure(0, weight=1)
        heading = ttk.Frame(inner, style="Panel.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        if number:
            ctk.CTkLabel(heading, text=number, width=26, height=26, corner_radius=13,
                         fg_color=gui_theme.ACCENT if primary else gui_theme.INPUT_BORDER,
                         text_color=gui_theme.TEXT,
                         font=gui_theme.BADGE).pack(side="left", padx=(0, 10))
        ttk.Label(heading, text=gui_theme.section_label(label),
                  style="PrimarySection.TLabel" if primary else "Section.TLabel").pack(side="left")
        inner.rowconfigure(0, weight=0)
        inner.grid_rowconfigure(1, weight=1)
        if anchor is not None:
            self._anchors[anchor] = card
        return inner

    # ------------------------------------------------------------------
    # Header + folders
    # ------------------------------------------------------------------
    def _build_header(self, parent):
        self.header_canvas = tk.Canvas(parent, height=104, background=gui_theme.SIDEBAR_BG,
                                       highlightthickness=0, bd=0)
        self.header_canvas.grid(row=0, column=0, sticky="ew")
        self._header_image = None
        self._header_source = None
        self._header_resize_job = None
        self._nav_items: dict[str, tk.Button] = {}
        self._header_bg_item = self.header_canvas.create_rectangle(
            0, 0, 1, 104, fill=gui_theme.SIDEBAR_BG, outline="", tags="header_bg")
        self._header_image_item = self.header_canvas.create_image(
            0, 0, image="", anchor="nw", tags="header_image")
        for label, target in (("Main", "sec_main"), ("Queue", "sec_queue"),
                              ("Logs", "sec_logs"), ("Reports", "sec_reports")):
            button = tk.Button(
                self.header_canvas, text=label, command=lambda t=target: self._scroll_to(t),
                font=gui_theme.BODY, background=gui_theme.SIDEBAR_BG, foreground=gui_theme.TEXT,
                activebackground=gui_theme.PRESET_FILL, activeforeground=gui_theme.TEXT,
                relief="flat", bd=0, highlightthickness=1, highlightbackground=gui_theme.CARD_BORDER,
                padx=8, pady=4, cursor="hand2", takefocus=True)
            self._nav_items[target] = button
        self._about_button = tk.Button(
            self.header_canvas, text="About", command=self._about, font=gui_theme.BODY,
            background=gui_theme.SIDEBAR_BG, foreground=gui_theme.MUTED,
            activebackground=gui_theme.PRESET_FILL, activeforeground=gui_theme.TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=gui_theme.CARD_BORDER,
            padx=8, pady=4, cursor="hand2", takefocus=True)
        self._header_title = self.header_canvas.create_text(
            22, 24, anchor="w", text="Sigma fp CinemaDNG Compressor", fill=gui_theme.TEXT,
            font=gui_theme.TITLE, tags="header_text")
        self._header_subtitle = self.header_canvas.create_text(
            22, 47, anchor="w", text="CinemaDNG compression, ready for Resolve.",
            fill=gui_theme.TEXT, font=gui_theme.TAGLINE, tags="header_text")
        self._header_helper = self.header_canvas.create_text(
            22, 66, anchor="w",
            text="Compress Sigma fp footage to 12-bit tiled CinemaDNG files.",
            fill=gui_theme.MUTED, font=gui_theme.CAPTION, tags="header_text")
        self._header_ready_dot = self.header_canvas.create_oval(
            1, 1, 9, 9, fill=gui_theme.SUCCESS, outline="", tags="header_text")
        self._header_ready_text = self.header_canvas.create_text(
            1, 1, anchor="w", text="Ready", fill=gui_theme.TEXT, font=gui_theme.CAPTION,
            tags="header_text")
        self.header_canvas.bind("<Configure>", self._queue_header_render)
        self.status.trace_add("write", lambda *_: self._update_header_status())
        self._set_nav_active("sec_main")
        self.after_idle(self._render_header)

    def _queue_header_render(self, _event=None):
        if self._header_resize_job is None:
            self._header_resize_job = self.after(60, self._render_header)

    def _render_header(self):
        self._header_resize_job = None
        width = max(1, self.header_canvas.winfo_width())
        height = max(1, self.header_canvas.winfo_height())
        self.header_canvas.coords(self._header_bg_item, 0, 0, width, height)
        try:
            if self._header_source is None:
                from PIL import Image
                self._header_source = Image.open(ROOT / "assets" / "mountain_header.png").convert("RGB")
            from PIL import Image, ImageOps, ImageTk
            backdrop = ImageOps.fit(self._header_source, (width, height), method=Image.Resampling.LANCZOS)
            self._header_image = ImageTk.PhotoImage(backdrop)
            self.header_canvas.itemconfigure(self._header_image_item, image=self._header_image)
            self.header_canvas.tag_lower(self._header_image_item, self._header_title)
        except Exception:
            pass
        nav_widths = (66, 70, 62, 78, 62)
        buttons = [self._nav_items[key] for key in ("sec_main", "sec_queue", "sec_logs", "sec_reports")]
        buttons.append(self._about_button)
        gap, right, status_width = 4, 20, 80
        x = width - right - sum(nav_widths) - gap * (len(nav_widths) - 1) - status_width
        for button, button_width in zip(buttons, nav_widths):
            tag = f"nav_{id(button)}"
            items = self.header_canvas.find_withtag(tag)
            if items:
                self.header_canvas.coords(items[0], x, 39)
            else:
                self.header_canvas.create_window(x, 39, window=button, width=button_width,
                                                 height=30, anchor="nw", tags=tag)
            x += button_width + gap
        self._update_header_status()
        self.header_canvas.coords(self._header_ready_dot, width - 82, 51, width - 72, 61)
        self.header_canvas.coords(self._header_ready_text, width - 65, 56)

    def _update_header_status(self):
        ready = self.status.get().strip().lower() == "ready"
        self.header_canvas.itemconfigure(self._header_ready_dot,
                                         fill=gui_theme.SUCCESS if ready else gui_theme.ACCENT)
        self.header_canvas.itemconfigure(self._header_ready_text,
                                         text="Ready" if ready else "Working")

    def _folder_card(self, parent, title: str, hint: str, variable: tk.StringVar,
                     browse, placeholder: str):
        th = gui_theme
        card = ttk.Frame(parent, style="Panel.TFrame", padding=8)
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(card, style="Panel.TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(3, 1))
        row.columnconfigure(1, weight=1)
        try:
            glyph = tk.Label(row, text=th.icon("folder"), font=(th.icon_font_family(self), 14),
                             background=th.CARD_BG, foreground=th.MUTED)
        except Exception:
            glyph = tk.Label(row, text="", background=th.CARD_BG)
        glyph.grid(row=0, column=0, padx=(2, 6))
        entry = ctk.CTkEntry(row, textvariable=variable, height=th.INPUT_HEIGHT,
                             fg_color=th.INPUT_BG, border_color=th.INPUT_BORDER, border_width=1,
                             corner_radius=th.RADIUS_CONTROL, text_color=th.TEXT)
        entry.grid(row=0, column=1, sticky="ew")
        browse_btn = ctk.CTkButton(row, text="Browse…", command=browse, height=th.INPUT_HEIGHT,
                                   corner_radius=th.RADIUS_CONTROL, fg_color=th.INPUT_BORDER,
                                   hover_color=th.ACCENT_HOVER, text_color=th.TEXT)
        browse_btn.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(card, text=hint, style="MutedPanel.TLabel").grid(row=2, column=0, sticky="w")
        self._add_placeholder(row, entry, variable, placeholder)
        self._config_widgets.extend([entry, browse_btn])
        return card, entry

    def _focus_entry(self, entry):
        try:
            entry.focus_set()
        except Exception:
            pass

    def _add_placeholder(self, row, entry, variable: tk.StringVar, text: str):
        """Overlay hint that is never readable as the entry value."""
        hint = tk.Label(row, text=text, background=gui_theme.INPUT_BG,
                        foreground=gui_theme.DIM, font=gui_theme.CAPTION)
        state = {"visible": False}

        def _refresh(_a=None, _b=None, _c=None):
            try:
                disabled = str(entry.cget("state")) == "disabled"
            except Exception:
                disabled = False
            show = not variable.get().strip() and not disabled
            try:
                # CTkEntry focus lives on an internal child: match the subtree.
                focused = str(self.focus_get()).startswith(str(entry))
            except Exception:
                focused = False
            show = show and not focused
            if show and not state["visible"]:
                try:
                    hint.place(in_=entry, relx=0.0, rely=0.5, anchor="w", x=34)
                except Exception:
                    return
                state["visible"] = True
            elif not show and state["visible"]:
                hint.place_forget()
                state["visible"] = False
        hint.bind("<Button-1>", lambda _e: self._focus_entry(entry))

        try:
            variable.trace_add("write", _refresh)
            entry.bind("<FocusIn>", lambda _e: _refresh())
            entry.bind("<FocusOut>", lambda _e: _refresh())
        except Exception:
            pass
        self.after(200, _refresh)

    def _build_folders(self, parent):
        card = self._section(parent, "1 Source & Output", anchor="sec_main")
        self.folder_wrap = ttk.Frame(card, style="Panel.TFrame")
        self.folder_wrap.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.src_card, _src_entry = self._folder_card(
            self.folder_wrap, "Source folder (read-only)",
            "Original footage. Will not be modified.", self.source, self._pick_source,
            "D:\\Sigma_Footage")
        self.dst_card, _dst_entry = self._folder_card(
            self.folder_wrap, "Output folder (new, empty)",
            "Must be a new folder. Existing output is moved to backup (if allowed).",
            self.destination, self._pick_destination,
            "D:\\Sigma_Footage_Compressed")
        self._relayout_folders()
        opts = ttk.Frame(card, style="Panel.TFrame")
        opts.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        # Backend always preserves the tree: static line, same internal value.
        ttk.Label(opts, text="✓  Subfolders included — folder structure is preserved",
                  style="Panel.TLabel").pack(side="left")
        allow = ctk.CTkSwitch(opts, text="Allow existing output", variable=self.allow_existing,
                              onvalue=True, offvalue=False, command=self._toggle_overwrite,
                              fg_color=gui_theme.ACCENT, progress_color=gui_theme.ACCENT)
        allow.pack(side="left", padx=(16, 8))
        ToolTip(allow, "Only enable to reuse a non-empty output folder. It is moved to a dated backup, never deleted.")
        self.overwrite = ctk.CTkEntry(opts, textvariable=self.overwrite_text, state="disabled", width=200,
                                      placeholder_text="Exact text to allow (e.g. OVERWRITE)",
                                      height=gui_theme.INPUT_HEIGHT, fg_color=gui_theme.INPUT_BG,
                                      border_color=gui_theme.INPUT_BORDER, border_width=1,
                                      corner_radius=gui_theme.RADIUS_CONTROL, text_color=gui_theme.TEXT)
        self.overwrite.pack(side="left")
        self._toggle_overwrite()
        self._config_widgets.append(allow)
        space_opts = ttk.Frame(card, style="Panel.TFrame")
        space_opts.grid(row=3, column=0, sticky="ew", pady=(3, 0))
        low_space = ctk.CTkSwitch(
            space_opts, text="Continue if estimated space is short", variable=self.allow_low_space,
            onvalue=True, offvalue=False, fg_color=gui_theme.ACCENT,
            progress_color=gui_theme.ACCENT)
        low_space.pack(side="left")
        ToolTip(low_space, "Overrides only the conservative estimate-based stop. The disk may still fill and leave partial output.")
        self._config_widgets.append(low_space)
        ToolTip(self.overwrite, "Type OVERWRITE exactly to confirm reuse of an existing output folder.")

    def _relayout_folders(self, narrow: bool | None = None):
        # Keep the two path fields readable when the main column gets narrow.
        if narrow is None:
            try:
                cw = self.left_column.winfo_width()
                narrow = (cw < 760) if cw > 1 else (self.winfo_width() < 1400)
            except Exception:
                narrow = False
        for widget in (self.src_card, self.dst_card):
            widget.grid_forget()
        if narrow:
            self.src_card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            self.dst_card.grid(row=1, column=0, sticky="ew")
            self.folder_wrap.columnconfigure(0, weight=1)
            self.folder_wrap.columnconfigure(1, weight=0)
        else:
            self.folder_wrap.columnconfigure(0, weight=1)
            self.folder_wrap.columnconfigure(1, weight=1)
            self.src_card.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.dst_card.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _pick_source(self):
        value = filedialog.askdirectory(title="Choose original CinemaDNG footage")
        if value:
            self.source.set(value)
            self._set_footer_default()

    def _pick_destination(self):
        value = filedialog.askdirectory(title="Choose an output folder")
        if value:
            self.destination.set(value)
            self._set_footer_default()

    def _toggle_overwrite(self):
        self.overwrite.configure(state="normal" if self.allow_existing.get() else "disabled")
        if not self.allow_existing.get():
            self.overwrite_text.set("")
        else:
            # Refresh placeholder visibility for the state change.
            self.overwrite_text.set(self.overwrite_text.get())

    # ------------------------------------------------------------------
    # Presets + options
    # ------------------------------------------------------------------
    _PRESET_CARDS = {
        "Lossless": ("Lossless", "12-bit  •  RAW-exact", "Target: integrity first; ratio is variable",
                     "ARCHIVAL / RAW-EXACT", "neutral"),
        "High Quality": ("High Quality", "12-bit  •  quality 90", "Target: approximately 3:1–4:1",
                          "LOSSY / PROTECT SHADOWS + HIGHLIGHTS", "green"),
        "Compact": ("Compact", "12-bit  •  quality 82", "Target: approximately 4:1–6:1",
                    "LOSSY / EDITING-SUITABLE", "amber"),
    }

    def _pill(self, parent, text: str, kind: str):
        th = gui_theme
        bg = {"green": th.PILL_GREEN_BG, "amber": th.PILL_AMBER_BG}.get(kind, th.PILL_NEUTRAL_BG)
        fg = {"green": th.SUCCESS, "amber": th.WARNING}.get(kind, th.MUTED)
        return tk.Label(parent, text=text, background=bg, foreground=fg, font=th.TAG, padx=8, pady=2)

    def _bind_card_click(self, widget, name: str):
        widget.bind("<Button-1>", lambda _e: self._set_preset(name))
        for child in widget.winfo_children():
            self._bind_card_click(child, name)

    def _build_presets(self, parent):
        th = gui_theme
        card = self._section(parent, "2 Job Setup")

        ttk.Label(card, text="Compress and convert CinemaDNG footage.", style="MutedPanel.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 5))

        names = ("Lossless", "High Quality", "Compact")
        row = ttk.Frame(card, style="Panel.TFrame")
        row.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.presets_row = row
        row.rowconfigure(0, weight=1)
        for index, name in enumerate(names):
            row.columnconfigure(index, weight=1)
            title, spec, ratio, pill, kind = self._PRESET_CARDS[name]
            frame = ctk.CTkFrame(row, fg_color=th.CARD_BG, border_width=1,
                                 border_color=th.CARD_BORDER, corner_radius=th.RADIUS_CARD)
            frame.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0))
            inner = tk.Frame(frame, background=th.CARD_BG)
            inner.pack(fill="both", expand=True, padx=10, pady=8)
            top = tk.Frame(inner, background=th.CARD_BG)
            top.pack(fill="x")
            title_label = tk.Label(top, text=title, background=th.CARD_BG, foreground=th.TEXT,
                                   font=th.CARD_TITLE)
            title_label.pack(side="left")
            check = tk.Label(top, text="○", background=th.CARD_BG, foreground=th.MUTED,
                             font=(th.FONT_FAMILY, 13))
            check.pack(side="right")
            spec_label = tk.Label(inner, text=spec, background=th.CARD_BG, foreground=th.MUTED,
                                  font=th.CAPTION)
            spec_label.pack(anchor="w")
            ratio_label = tk.Label(inner, text=ratio, background=th.CARD_BG, foreground=th.MUTED,
                                   font=th.CAPTION)
            ratio_label.pack(anchor="w")
            pill_label = self._pill(inner, pill, kind)
            pill_label.pack(anchor="w", pady=(4, 0))
            self._preset_cards[name] = frame
            frame.check = check  # type: ignore[attr-defined]
            frame.inner = inner  # type: ignore[attr-defined]
            frame.top = top  # type: ignore[attr-defined]
            frame.recolor = (title_label, spec_label, ratio_label)  # type: ignore[attr-defined]
            self._bind_card_click(frame, name)
            frame.bind("<Enter>", lambda _e, n=name: self._hover_card(n, True))
            frame.bind("<Leave>", lambda _e, n=name: self._hover_card(n, False))
        # Keep the technical selectors with the preset and operation mode.
        ttk.Separator(card, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=(7, 2))
        opts = ttk.Frame(card, style="Panel.TFrame")
        opts.grid(row=5, column=0, sticky="ew", pady=(3, 0))
        for index in range(3):
            opts.columnconfigure(index, weight=1)
        self._combo_cell(opts, 0, "Workers", self.workers, ("auto", "1", "2", "4", "6", "8"),
                         "Number of parallel conversion workers. Auto chooses a safe default.")
        self._combo_cell(opts, 1, "Verify", self.verify_mode, ("sampled", "full", "checksum"),
                         "Choose how thoroughly converted frames are verified.")
        self._combo_cell(opts, 2, "Tiles", self.tile_layout, ("small", "four"),
                         "Smaller tiles improve compatibility and processing behavior.")

    def _build_processing_options(self, parent):
        th = gui_theme
        card = self._section(parent, "5 Processing Options")
        self._switch_row(card, 1, "Strict guardrails",
                         "Fail the run if error thresholds are exceeded.",
                         self.strict_guardrails,
                         "Fail the run if measured error thresholds are exceeded. Otherwise breaches are warnings.")
        self._switch_row(card, 2, "Resume",
                         "Reuse valid frames from an interrupted run.",
                         self.resume,
                         "Continue a previously interrupted conversion by reusing valid existing frames.")
        self._switch_row(card, 3, "Keep reports in output",
                         "Leave diagnostic files beside the converted footage.",
                         self.keep_output_artifacts,
                         "Off by default: reports and the Resolve Comments CSV are saved under Logs/Sessions. Resume keeps its required source manifest in the output folder.")

    def _build_analysis_recommendation(self, parent):
        th = gui_theme
        card = self._section(parent, "6 Analysis / Recommendation")
        rec = ttk.Frame(card, style="Panel.TFrame")
        rec.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.rec_row = rec
        rec_btn = ctk.CTkButton(rec, text="✨ Analyze & Recommend", command=self._recommend,
                                fg_color="transparent", border_width=1, border_color=th.CARD_BORDER,
                                text_color=th.TEXT, hover_color=th.INPUT_BORDER,
                                corner_radius=th.RADIUS_CONTROL, height=th.SECONDARY_BUTTON_HEIGHT)
        rec_btn.pack(side="left")
        ToolTip(rec_btn, "Measure sample frames and recommend a preset. Never changes your preset silently.")
        self.recommend_result = tk.StringVar(value="No analysis yet.")
        tk.Label(rec, textvariable=self.recommend_result, background=th.CARD_BG,
                 foreground=th.MUTED, font=th.CAPTION, wraplength=250,
                 justify="left").pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.recommend_apply = ctk.CTkButton(rec, text="Apply recommendation", command=self._apply_recommendation,
                                             fg_color="transparent", border_width=1,
                                             border_color=th.ACCENT, text_color=th.TEXT,
                                             hover_color=th.PRESET_FILL,
                                             corner_radius=th.RADIUS_CONTROL,
                                             height=th.SECONDARY_BUTTON_HEIGHT)
        self.recommend_apply.pack_forget()
        self._config_widgets.append(self.recommend_apply)
        self._config_widgets.append(rec_btn)

    def _combo_cell(self, parent, column: int, label: str, variable: tk.StringVar, values: tuple, tip: str):
        th = gui_theme
        cell = ttk.Frame(parent, style="Panel.TFrame")
        cell.grid(row=0, column=column, sticky="ew", padx=(0, 8))
        lab = ttk.Label(cell, text=label, style="Panel.TLabel")
        lab.pack(anchor="w")
        combo = ctk.CTkComboBox(cell, variable=variable, values=list(values), state="readonly",
                                fg_color=th.INPUT_BG, border_color=th.INPUT_BORDER, border_width=1,
                                button_color=th.INPUT_BORDER, button_hover_color=th.ACCENT_HOVER,
                                dropdown_fg_color=th.INPUT_BG, dropdown_text_color=th.TEXT,
                                text_color=th.TEXT, corner_radius=th.RADIUS_CONTROL)
        combo.pack(fill="x", pady=(4, 0))
        ToolTip(combo, tip)
        ToolTip(lab, tip)
        self._config_widgets.append(combo)

    def _switch_row(self, parent, row: int, label: str, caption: str, variable: tk.BooleanVar, tip: str):
        th = gui_theme
        cell = ttk.Frame(parent, style="Panel.TFrame")
        cell.grid(row=row, column=0, sticky="ew", pady=2)
        cell.columnconfigure(1, weight=1)
        switch = ctk.CTkSwitch(cell, text="", variable=variable, onvalue=True, offvalue=False,
                               fg_color=th.SWITCH_OFF, progress_color=th.ACCENT,
                               button_color=th.TEXT_ON_ACCENT, button_hover_color=th.TEXT_ON_ACCENT)
        switch.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 8))
        texts = ttk.Frame(cell, style="Panel.TFrame")
        texts.grid(row=0, column=1, sticky="ew")
        texts.columnconfigure(0, weight=1)
        lab = ttk.Label(texts, text=label, style="Panel.TLabel")
        lab.grid(row=0, column=0, sticky="w")
        cap = ttk.Label(texts, text=caption, style="MutedPanel.TLabel", wraplength=250)
        cap.grid(row=1, column=0, sticky="w")
        info = tk.Label(texts, text="ⓘ", background=th.CARD_BG, foreground=th.DIM, font=th.CAPTION)
        info.grid(row=0, column=1, rowspan=2, sticky="e", padx=(6, 0))
        ToolTip(info, tip)
        ToolTip(lab, tip)
        self._config_widgets.append(switch)


    def _set_preset(self, name: str):
        self.preset.set(name)
        self._refresh_preset_cards()

    def _paint_card(self, name: str, hover: bool = False):
        th = gui_theme
        frame = self._preset_cards[name]
        selected = name == self.preset.get()
        try:
            if selected:
                frame.configure(border_width=2, border_color=th.PRESET_BORDER, fg_color=th.PRESET_FILL)
            elif hover:
                frame.configure(border_width=1, border_color=th.ACCENT_HOVER, fg_color=th.CARD_BG)
            else:
                frame.configure(border_width=1, border_color=th.CARD_BORDER, fg_color=th.CARD_BG)
            check = frame.check  # type: ignore[attr-defined]
            check.configure(text="●" if selected else "○",
                            foreground=th.ACCENT if selected else th.MUTED,
                            background=th.PRESET_FILL if selected else th.CARD_BG)
            surface = th.PRESET_FILL if selected else th.CARD_BG
            widgets = (frame.inner, frame.top) + tuple(frame.recolor)  # type: ignore[attr-defined]
            for widget in widgets:
                try:
                    widget.configure(background=surface)
                except Exception:
                    pass
        except Exception:
            pass

    def _hover_card(self, name: str, hover: bool):
        if name != self.preset.get():
            self._paint_card(name, hover=hover)

    def _refresh_preset_cards(self):
        for name in self._preset_cards:
            self._paint_card(name)

    # ------------------------------------------------------------------
    # Actions + progress
    # ------------------------------------------------------------------
    def _ghost_button(self, parent, text: str, command, tip: str):
        th = gui_theme
        button = ctk.CTkButton(parent, text=text, command=command, height=th.SECONDARY_BUTTON_HEIGHT,
                               corner_radius=th.RADIUS_CONTROL, fg_color="transparent",
                               border_width=1, border_color=th.CARD_BORDER, text_color=th.TEXT,
                               hover_color=th.INPUT_BORDER)
        ToolTip(button, tip)
        return button

    def _build_actions(self, parent):
        th = gui_theme
        card = self._section(parent, "3 Actions")
        row = ttk.Frame(card, style="Panel.TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.start = ctk.CTkButton(row, text="▶  Start Compression", command=self._start,
                                   height=th.PRIMARY_BUTTON_HEIGHT, corner_radius=th.RADIUS_CONTROL,
                                   fg_color=th.ACCENT, hover_color=th.ACCENT_HOVER,
                                   text_color=th.TEXT_ON_ACCENT, font=th.PRIMARY_BUTTON)
        self.start.pack(side="left")
        ToolTip(self.start, "Convert the selected footage using the chosen compression preset.")
        for label, callback, tip in (
                ("Dry-run", self._dry_run, "Read-only preflight validation. Writes nothing."),
                ("Open output folder", self._open_output, "Open the last completed output folder."),
                ("Support bundle", self._bundle, "Zip logs, reports and environment for support.")):
            button = self._ghost_button(row, label, callback, tip)
            button.pack(side="left", padx=(5, 0))
            self._config_widgets.append(button)
        self._config_widgets.extend([self.start])

    def _build_progress(self, parent):
        th = gui_theme
        card = self._section(parent, "Progress")
        self.progress_title = tk.StringVar(value="Idle — nothing running.")
        ttk.Label(card, textvariable=self.progress_title, style="Section.TLabel").grid(row=1, column=0, sticky="w")
        self.progress = ProgressBar(card, height=10, corner_radius=5, fg_color=th.APP_BG,
                                    progress_color=th.ACCENT)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.progress_percent = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.progress_percent, style="Percent.TLabel").grid(row=3, column=0, sticky="w")
        info = ttk.Frame(card, style="Panel.TFrame")
        info.grid(row=4, column=0, sticky="ew")
        info.columnconfigure(1, weight=1)
        ttk.Label(info, text="Sequences", style="MutedPanel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.metrics, style="Panel.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info, text="Current sequence", style="MutedPanel.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(info, textvariable=self.current_footage, style="Panel.TLabel").grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info, text="Frames", style="MutedPanel.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(info, textvariable=self.current_frame, style="Panel.TLabel").grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info, text="Current DNG", style="MutedPanel.TLabel").grid(row=3, column=0, sticky="w")
        ttk.Label(info, textvariable=self.current_file, style="Panel.TLabel").grid(row=3, column=1, sticky="w", padx=(8, 0))
        ttk.Label(info, text="Status", style="MutedPanel.TLabel").grid(row=4, column=0, sticky="w")
        ttk.Label(info, textvariable=self.status, style="Panel.TLabel", wraplength=640).grid(row=4, column=1, sticky="w", padx=(8, 0))
        run_row = ttk.Frame(card, style="Panel.TFrame")
        run_row.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.cancel = ctk.CTkButton(run_row, text="■  Stop after current work", command=self._stop,
                                    state="disabled", height=th.SECONDARY_BUTTON_HEIGHT,
                                    corner_radius=th.RADIUS_CONTROL, fg_color=th.INPUT_BORDER,
                                    hover_color=th.ACCENT_HOVER, text_color=th.TEXT)
        self.cancel.pack(side="left")
        ToolTip(self.cancel, "Stop after the current frame. Completed output is kept.")
        self.details_button = ttk.Button(card, text="Details ▸", command=self._toggle_details)
        self.details_button.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.log_frame = ttk.Frame(card, style="Panel.TFrame")
        self.log = tk.Text(self.log_frame, height=12, wrap="word", state="disabled",
                           font=self._mono_font(), background=gui_theme.LOG_BG, foreground=gui_theme.LOG_TEXT,
                           insertbackground=TEXT, borderwidth=1, relief="solid")
        self.log.pack(fill="both", expand=True)
        self._details_visible = False
        self.complete_row = ttk.Frame(card, style="Panel.TFrame")
        ttk.Button(self.complete_row, text="Open output folder",
                   command=self._open_output).pack(side="left")
        ttk.Button(self.complete_row, text="Reports",
                   command=lambda: self._scroll_to("sec_reports")).pack(side="left", padx=(8, 0))
        # IDLE: no progress panel, no empty bar — just the status bar.
        self._progress_outer = card.master
        self._progress_grid = {k: v for k, v in card.master.grid_info().items() if k != "in"}
        self._progress_outer.grid_remove()
        self._progress_visible = False

    def _ensure_progress(self):
        if not self._progress_visible:
            self._progress_outer.grid(**self._progress_grid)
            self._progress_visible = True
            try:
                self.update_idletasks()
            except Exception:
                pass

    def _middle_ellipse(self, text: str, limit: int = 52) -> str:
        if len(text) <= limit:
            return text
        keep = (limit - 1) // 2
        return text[:keep] + "…" + text[-keep:]

    def _mono_font(self):
        try:
            import tkinter.font as tkfont
            families = set(tkfont.families(self))
            for name in gui_theme.LOG_FONT_OPTIONS:
                if name in families:
                    return (name, gui_theme.LOG_SIZE)
        except Exception:
            pass
        return (gui_theme.MONO_FALLBACK, gui_theme.LOG_SIZE)

    _RECOMMEND_NAMES = {"lossless": "Lossless", "high_quality": "High Quality",
                        "compact": "Compact"}

    @classmethod
    def _recommendation_name(cls, key: str, default: str = "High Quality") -> str:
        return cls._RECOMMEND_NAMES.get(str(key or ""), default)

    def _apply_recommendation(self):
        rec = self._last_recommend
        if not rec:
            return
        name = self._recommendation_name(rec.get("recommendation", ""))
        self._set_preset(name)
        self._append(f"Applied recommended preset: {name}")

    def _toggle_details(self):
        self._details_visible = not self._details_visible
        if self._details_visible:
            self.log_frame.grid(row=7, column=0, sticky="nsew", pady=(6, 0))
            self.details_button.configure(text="Details ▾")
        else:
            self.log_frame.grid_forget()
            self.details_button.configure(text="Details ▸")

    def _set_config_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for widget in self._config_widgets:
            try:
                if isinstance(widget, ctk.CTkComboBox):
                    widget.configure(state="readonly" if enabled else "disabled")
                else:
                    widget.configure(state=state)
            except Exception:
                pass
        self.overwrite.configure(state="normal" if (enabled and self.allow_existing.get()) else "disabled")
        if enabled:
            self._toggle_overwrite()
        for var in (self.source, self.destination):
            var.set(var.get())

    # ------------------------------------------------------------------
    # Queue + reports
    # ------------------------------------------------------------------
    def _build_queue(self, parent):
        card = self._section(parent, "4 Batch Queue", anchor="sec_queue")
        bar = ttk.Frame(card, style="Panel.TFrame")
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="+ Enqueue current folders", command=self._enqueue).pack(side="left")
        ttk.Button(bar, text="Remove selected", command=self._dequeue).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(bar, text="Continue on error", variable=self.continue_on_error).pack(side="left", padx=(8, 0))
        self.qstart = ttk.Button(bar, text="▶ Start queue", command=self._start_queue)
        self.qstart.pack(side="right")
        self._config_widgets.append(self.qstart)
        self.queue_tree = ttk.Treeview(card, columns=("#", "folder", "preset", "mode", "status", "progress"),
                                       show="headings", height=8)
        self.queue_tree.tag_configure("alternate", background=gui_theme.TREEVIEW_ALT)
        for column, title, width in (("#", "#", 36), ("folder", "Folder", 270), ("preset", "Preset", 90),
                                    ("mode", "Mode", 90), ("status", "Status", 115), ("progress", "Progress", 260)):
            self.queue_tree.heading(column, text=title)
            self.queue_tree.column(column, width=width, anchor="w")
        self.queue_tree.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        queue_scroll = ttk.Scrollbar(card, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=queue_scroll.set)
        queue_scroll.grid(row=2, column=1, sticky="ns", pady=(6, 0))
        self.queue_empty = ttk.Label(self.queue_tree,
                                     text="No folders in queue — add a folder above or use the current selection.",
                                     style="MutedPanel.TLabel", anchor="center", justify="center")
        self.queue_empty.place(relx=0.5, rely=0.5, anchor="center")

    def _build_reports(self, parent):
        card = self._section(parent, "7 Logs & Reports", anchor="sec_reports")
        ttk.Label(card, text="Conversion reports and session logs.",
                  style="MutedPanel.TLabel").grid(row=1, column=0, sticky="w")
        row = ttk.Frame(card, style="Panel.TFrame")
        row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        reports = (("Checksums", self._open_checksums),
                   ("Verification report", self._open_report),
                   ("Previews", self._open_previews),
                   ("Resolve Comments", self._open_resolve_metadata),
                   ("Log folder", self._open_log_folder))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        for index, (label, callback) in enumerate(reports):
            ttk.Button(row, text=label, command=callback, style="Compact.TButton").grid(
                row=index // 2, column=index % 2, sticky="ew",
                padx=(0 if index % 2 == 0 else 5, 5 if index % 2 == 0 else 0), pady=3)
        self._anchors["sec_logs"] = self.log_frame

    def _set_footer_default(self):
        destination = self.destination.get().strip()
        self.footer.set(f"Output: {destination}" if destination else "Ready")

    def _about(self):
        messagebox.showinfo("About", "Sigma fp CinemaDNG Compressor\nThree compression modes. Ready for Resolve.\n\nBackend: cdng_lossless_mvp / cdng_parallel.")

    # ------------------------------------------------------------------
    # Preserved backend logic (validation, backup, audit, argv, …)
    # ------------------------------------------------------------------
    def _append(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        try:
            if int(self.log.index("end-1c").split(".")[0]) > 5000:
                self.log.delete("1.0", "1000.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self):
        return self._start_run(False)

    def _fail(self, quiet: bool, title: str, message: str) -> bool:
        if quiet:
            self._append(f"{title}: {message}")
            self.status.set(f"{title}. See log.")
        else:
            messagebox.showerror(title, message)
        return False

    def _validate_folders(self, quiet: bool):
        """Fast synchronous checks. Returns (source, destination) or (None, None)."""
        def _fail(title: str, message: str):
            return self._fail(quiet, title, message)
        raw_source, raw_destination = self.source.get().strip(), self.destination.get().strip()
        # Path("") resolves to the current working directory, and the launcher
        # starts the app in its own install folder, so a blank field could target
        # (and with OVERWRITE, relocate) the application itself.
        if not raw_source:
            _fail("Original footage", "Choose an existing source folder.")
            return None, None
        if not raw_destination:
            _fail("Output folder", "Choose an output folder.")
            return None, None
        source, destination = Path(raw_source), Path(raw_destination)
        if not source.is_dir():
            _fail("Original footage", "Choose an existing source folder.")
            return None, None
        try:
            src_res, dst_res = source.resolve(), destination.resolve()
        except OSError:
            src_res, dst_res = source, destination
        if (src_res == dst_res or dst_res.is_relative_to(src_res) or src_res.is_relative_to(dst_res)):
            _fail("Unsafe output", "The output folder cannot be the source folder, sit inside it, or contain the source.")
            return None, None
        if not self.include_subfolders.get() and not quiet:
            messagebox.showinfo("Subfolders", "This app always preserves subfolders. Leave the option enabled.")
        resume_existing = bool(self.resume.get() and destination.exists())
        if destination.exists():
            if not destination.is_dir():
                _fail("Output folder", "The output path is a file. Choose a folder.")
                return None, None
            if any(destination.iterdir()) and not resume_existing:
                if not (self.allow_existing.get() and self.overwrite_text.get().strip() == "OVERWRITE"):
                    _fail("Existing output", "Choose an empty folder, or tick the option and type OVERWRITE exactly.")
                    return None, None
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = destination.with_name(destination.name + ".backup-" + stamp)
                counter = 1
                while backup.exists():
                    counter += 1
                    backup = destination.with_name(destination.name + f".backup-{stamp}-{counter}")
                try:
                    shutil.move(str(destination), str(backup))
                    self._append(f"Existing output moved safely to: {backup}")
                except OSError as error:
                    _fail("Cannot preserve existing output", str(error))
                    return None, None
        if destination.exists() and not resume_existing:
            # The converter owns creating the output folder and refuses a
            # pre-existing one, so an empty placeholder must be removed first.
            try:
                destination.rmdir()
            except OSError as error:
                _fail("Output folder", f"Could not prepare the output folder (is it empty?): {error}")
                return None, None
        self._resume_existing_output = resume_existing
        self._resume_destination = destination.resolve() if resume_existing else None
        return source, destination

    @staticmethod
    def _progress_path_key(path: str | Path) -> str:
        return str(Path(path).resolve()).casefold()

    def _reset_progress_tracking(self, root: str | Path) -> None:
        self._progress_root = str(Path(root).resolve())
        self._progress_footages = {}
        self._progress_eligible_frames = 0
        self._progress_processed_frames = 0.0
        self._progress_active_footage = None
        self._progress_incomplete = False
        self.metrics.set("Processing 0/0 frames")
        self.current_footage.set("")
        self.current_frame.set("")
        self.current_file.set("")

    def _progress_render(self) -> None:
        active = self._progress_footages.get(self._progress_active_footage)
        weighted_done = self._progress_processed_frames
        if active and active.get("eligible"):
            weighted_done += active["weight"] * active.get("fraction", 0.0)
        percent = combined_progress_percent(weighted_done, self._progress_eligible_frames)
        if self._progress_incomplete or self._progress_active_footage is not None:
            percent = min(percent, 99.0)
        self.progress.configure(mode="determinate", maximum=100, value=percent)
        self.progress_percent.set(f"Overall {percent:.0f}%")
        processed = min(self._progress_eligible_frames, max(0, weighted_done))
        self.metrics.set(f"Processing {processed:,.0f}/{self._progress_eligible_frames:,} frames")

    def _progress_prepare_sequence(self, selection: dict) -> None:
        relative = str((self._batch_running or {}).get("_progress_footage", "."))
        try:
            weight = max(1, int(selection.get("dng_count", 1)))
        except (TypeError, ValueError):
            weight = 1
        self._progress_footages = {relative: {
            "weight": weight, "eligible": True, "fraction": 0.0,
            "credited": False, "processing_index": 1, "processing_total": 1,
        }}
        self._progress_eligible_frames = weight
        self._progress_processed_frames = 0.0
        self._progress_active_footage = None
        self._progress_incomplete = False
        self._progress_render()

    def _progress_begin_sequence(self, selection: dict, action: str) -> None:
        job = self._batch_running or {}
        relative = str(job.get("_progress_footage", selection.get("footage", ".")))
        record = self._progress_footages.get(relative)
        if record is None:
            self._progress_prepare_sequence(selection)
            record = self._progress_footages.get(relative)
        if record is None:
            return
        if record.get("credited"):
            self._progress_processed_frames -= record["weight"] * record.get("fraction", 0.0)
            record["credited"] = False
        record["fraction"] = 0.0
        self._progress_active_footage = relative
        index = record.get("processing_index", 1)
        total = record.get("processing_total", 1)
        source_name = Path(self._progress_root or self.source.get()).name
        name = source_name if relative == "." else f"{source_name}/{relative}"
        self.current_footage.set(f"{action} sequence {index}/{total}: {name}")
        self.progress_title.set(f"{action} sequence {index}/{total} — {name}")
        self.current_frame.set(f"Frame 0 / {record['weight']:,}")
        self.current_file.set("")
        self._progress_render()

    def _progress_frame(self, done: int, total: int) -> None:
        record = self._progress_footages.get(self._progress_active_footage)
        if record is not None:
            record["fraction"] = min(1.0, max(0.0, done / total)) if total else 0.0
        self.current_frame.set(f"Frame {done:,} / {total:,}")
        self._progress_render()

    def _progress_finish_sequence(self, success: bool, stopped: bool = False) -> None:
        relative = self._progress_active_footage
        record = self._progress_footages.get(relative)
        if record is None:
            return
        fraction = 1.0 if success else record.get("fraction", 0.0)
        self._progress_processed_frames += record["weight"] * fraction
        record["fraction"] = fraction
        record["credited"] = True
        record["state"] = "done" if success else "stopped" if stopped else "failed"
        self._progress_incomplete |= not success
        source_name = Path(self._progress_root or self.source.get()).name
        name = source_name if relative == "." else f"{source_name}/{relative}"
        action = "Completed" if success else "Stopped" if stopped else "Failed"
        self.current_footage.set(
            f"{action} sequence {record.get('processing_index', 1)}"
            f"/{record.get('processing_total', 1)}: {name}")
        self._progress_active_footage = None
        self._progress_render()

    def _start_run(self, quiet: bool) -> bool:
        if self.process is not None or (self.batch_active and not quiet):
            messagebox.showinfo("Busy", "A conversion or queue is already running.")
            return False
        source, destination = self._validate_folders(quiet)
        if source is None:
            return False
        progress_root = (self._batch_running or {}).get("_progress_root") or source
        if self._progress_root is None or self._progress_path_key(self._progress_root) != self._progress_path_key(progress_root):
            self._reset_progress_tracking(progress_root)
        self._active_session_id = (self._batch_running.setdefault("session_id", self._new_session_id())
                                   if self._batch_running is not None else self._new_session_id())
        self._ensure_progress()
        self.complete_row.grid_forget()
        selection = {"dng_count": 1, "footage": "."}
        self._progress_prepare_sequence(selection)
        self.progress_title.set("Ready to compress.")
        self.status.set("Starting compression…")
        return self._begin_conversion(source, destination, selection)

    def _queue_skip_current(self):
        """Mark the running queue item skipped and advance (queue mode only)."""
        if self._batch_running is not None:
            self._batch_running["status"] = "skipped"
            self._batch_running = None
        self._refresh_queue()
        self._set_config_enabled(True)
        if not self._progress_eligible_frames:
            self.progress_title.set("No sequences to process.")
            self._progress_render()
        else:
            self.progress_title.set("Idle — nothing running.")
        self.after(100, lambda: self._batch_next(None))


    def _begin_conversion(self, source: Path, destination: Path, selection: dict) -> bool:
        self._launch(source, destination, self._preset_args(), selection)
        return True

    def _preset_args(self, bits: int = 12) -> dict:
        """Map the GUI preset to converter flags.

        The three GUI choices map one-to-one to the backend's public modes.
        Every preset writes 12-bit CinemaDNG.  Lossless promotes the source's
        camera-linear values without changing them; the two lossy modes use
        the shared log/VBR RAW path with conservative versus stronger
        quantization.
        """
        name = self.preset.get()
        if name == "Lossless":
            return {"mode": "lossless", "output_bits": "12", "quality": "90"}
        if name == "Compact":
            return {"mode": "compact", "output_bits": "12", "quality": "82"}
        return {"mode": "high_quality", "output_bits": "12", "quality": "90"}

    @staticmethod
    def _new_session_id() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]

    def _session_artifact_dir(self, destination: Path) -> Path:
        session_id = self._active_session_id or self._new_session_id()
        self._active_session_id = session_id
        item = self._batch_running or {}
        label = item.get("display_name") or Path(self.source.get()).name or destination.name
        slug = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(label))
        slug = slug.strip("._") or "footage"
        slug += "-" + uuid.uuid5(uuid.NAMESPACE_URL, str(label)).hex[:8]
        target = ROOT / "logs" / "sessions" / session_id / slug
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = destination.parent / f"{destination.name}.session-{session_id}" / slug
            target.mkdir(parents=True, exist_ok=True)
        self._last_session_artifact_dir = target
        return target

    def _write_session_record(self, target: Path, source: Path, destination: Path) -> None:
        record = {"session_id": self._active_session_id, "source": str(source.resolve()),
                  "destination": str(destination.resolve())}
        try:
            (target / "session.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as error:
            self._append(f"NOTE: could not save session details ({error}).")

    def _restore_resume_artifacts(self, source: Path, destination: Path) -> None:
        if not self._resume_existing_output:
            return
        import os

        source_key = os.path.normcase(str(source.resolve()))
        destination_key = os.path.normcase(str(destination.resolve()))
        records = list((ROOT / "logs" / "sessions").glob("**/session.json"))
        fallback_prefix = f"{destination.name}.session-"
        try:
            for folder in destination.parent.iterdir():
                if folder.is_dir() and folder.name.startswith(fallback_prefix):
                    records.extend(folder.glob("**/session.json"))
        except OSError:
            pass
        records = list(dict.fromkeys(records))
        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0
        records.sort(key=_mtime, reverse=True)
        restored_all = []
        for record_path in records:
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if (os.path.normcase(record.get("source", "")) != source_key
                        or os.path.normcase(record.get("destination", "")) != destination_key):
                    continue
                archived_dir = record_path.parent
                restored = []
                for name in ("source-manifest.json",):
                    archived = archived_dir / name
                    output = destination / name
                    if not output.exists() and archived.is_file():
                        shutil.copy2(archived, output)
                        restored.append(name)
                restored_all.extend(f"{archived_dir / name}" for name in restored)
                if all((destination / name).is_file()
                       for name in ("source-manifest.json",)):
                    break
            except (OSError, ValueError, TypeError) as error:
                self._append(f"NOTE: could not restore resume files from {record_path} ({error}).")
        if restored_all:
            self._append(f"Restored resume files: {', '.join(restored_all)}")

    def _archive_output_artifacts(self, destination: Path) -> None:
        if self.keep_output_artifacts.get() or not destination.is_dir():
            return
        target = self._session_artifact_dir(destination)
        keep_for_resume = {"source-manifest.json"} if self.resume.get() else set()
        names = ("checksums.sha256", "verification-report.json", "source-manifest.json",
                 "resolve-metadata.csv")
        moved = []
        for name in names:
            if name in keep_for_resume:
                continue
            path = destination / name
            if not path.is_file():
                continue
            try:
                shutil.move(str(path), str(target / name))
                moved.append(name)
            except OSError as error:
                self._append(f"NOTE: could not move {name} to the session folder ({error}); it remains in output.")
        if moved:
            self._append(f"Session files for this footage: {target} ({', '.join(moved)})")
        if keep_for_resume:
            self._append("Resume is enabled; source-manifest.json stays in output for safe resume checks.")

    def _artifact_path(self, name: str) -> Path | None:
        if self.last_destination is not None:
            output_path = self.last_destination / name
            if output_path.exists():
                return output_path
        if self._last_session_artifact_dir is not None:
            archived = self._last_session_artifact_dir / name
            if archived.exists():
                return archived
        return None

    def _launch(self, source: Path, destination: Path, preset: dict, selection: dict):
        artifact_dir = self._session_artifact_dir(destination)
        command = [str(RUNTIME), str(CONVERTER), str(source), str(destination),
                   "--mode", preset["mode"], "--output-bits", preset["output_bits"],
                   "--quality", preset["quality"],
                   "--workers", self.workers.get(), "--verify-mode", self.verify_mode.get(),
                   "--tile-layout", self.tile_layout.get()]
        if self.strict_guardrails.get():
            command.append("--strict-guardrails")
        if self.allow_low_space.get():
            command.append("--allow-low-space")
        if self.resume.get():
            command.append("--resume")
        if self._batch_running is not None and self._batch_running.get("direct_only"):
            command.append("--direct-only")
        if self._batch_running is not None:
            root_source = self._batch_running.get("root_companions_source")
            root_destination = self._batch_running.get("root_companions_destination")
            if root_source and root_destination:
                command += ["--root-companions-source", root_source,
                            "--root-companions-destination", root_destination]
        allowed_resume = (self._resume_existing_output and self.resume.get()
                          and self._resume_destination == destination.resolve())
        if destination.exists() and not allowed_resume:
            self._set_config_enabled(True)
            self.progress_title.set("Idle — nothing running.")
            if self.batch_active:
                self._append(f"Output appeared during conversion: {destination}; skipping queue item.")
                self._queue_skip_current()
            else:
                self._fail(False, "Output folder",
                           "The output folder appeared while preparing conversion. Choose an empty folder and retry.")
            return
        self._restore_resume_artifacts(source, destination)
        self._write_session_record(artifact_dir, source, destination)
        self._append("Starting: " + " ".join(command))
        self.last_destination = destination
        self._set_footer_default()
        self.started = time.perf_counter()
        self.frames_done = self.frames_total = 0
        self._fps_samples = []
        self.current_file.set("")
        self._progress_begin_sequence(selection, "Compressing")
        self.complete_row.grid_forget()
        self._stop_requested = False
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace",
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            self.process = None
            self._progress_finish_sequence(False)
            self._set_config_enabled(True)
            self.progress_title.set("Idle — nothing running.")
            self._fail(self.batch_active, "Could not start converter", str(error))
            if self.batch_active:
                self._queue_skip_current()
            return
        self._set_config_enabled(False)
        self.start.configure(state="disabled")
        self.cancel.configure(state="normal")
        self.status.set("Converting…")
        self.footer.set(f"Converting → {destination}")
        threading.Thread(target=self._read_output, daemon=True).start()

    def _dry_run(self):
        import cdng_lossless_mvp as mvp
        raw_source, raw_destination = self.source.get().strip(), self.destination.get().strip()
        if not raw_source or not raw_destination:
            return messagebox.showerror("Dry-run", "Choose source and output folders first.")
        report = mvp.preflight_report(Path(raw_source), Path(raw_destination), preview="none",
                                      allow_low_space=self.allow_low_space.get())
        self._ensure_progress()
        for check in report["checks"]:
            status = "WARN" if check.get("warning") else "OK" if check["ok"] else "FAIL"
            self._append(f"[{status}] {check['name']}: {check['detail']}")
        passed = report["ok"]
        self.status.set("Dry-run passed: ready to convert." if passed else "Dry-run found problems; see log.")
        self.progress_title.set("Dry-run " + ("passed." if passed else "found problems — open Details."))

    def _recommend(self):
        raw_source = self.source.get().strip()
        if not raw_source:
            return messagebox.showerror("Recommend", "Choose a source folder first.")
        self._ensure_progress()
        self.status.set("Measuring sample frames for a preset recommendation…")
        self.progress_title.set("Analyzing footage for a tailored recommendation…")

        def _work():
            try:
                import cdng_lossless_mvp as mvp
                rec = mvp.recommend_preset(Path(raw_source))
                self.events.put(("recommend", json.dumps(rec)))
            except Exception as error:
                self.events.put(("recommend_failed", f"RECOMMEND failed: {type(error).__name__}: {error}"))

        threading.Thread(target=_work, daemon=True).start()

    def _show_recommend_dialog(self, rec: dict):
        if self._recommend_dialog is not None:
            try:
                if self._recommend_dialog.winfo_exists():
                    self._recommend_dialog.lift()
                    return
            except Exception:
                pass
        dialog = tk.Toplevel(self)
        self._recommend_dialog = dialog
        dialog.title("Preset recommendation")
        dialog.configure(background=PANEL)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        name = self._recommendation_name(rec.get("recommendation", ""))
        ttk.Label(dialog, text=f"Recommended: {name}", style="Section.TLabel").pack(anchor="w", padx=16, pady=(14, 2))
        ttk.Label(dialog, text=f"Estimated compression: ~{rec.get('quality_ratio', 0):.1f}:1 (High Quality) / "
                               f"~{rec.get('lossless_ratio', 0):.1f}:1 (Lossless)",
                  style="MutedPanel.TLabel", wraplength=420).pack(anchor="w", padx=16)
        ttk.Label(dialog, text=f"Guardrails: {'breach' if rec.get('breaches') else 'clean'}  •  "
                               f"samples: {', '.join(rec.get('samples', [])[:3])}",
                  style="MutedPanel.TLabel", wraplength=420).pack(anchor="w", padx=16)
        ttk.Label(dialog, text=rec.get("reason", ""), style="MutedPanel.TLabel",
                  wraplength=420).pack(anchor="w", padx=16, pady=(6, 0))
        buttons = ttk.Frame(dialog, style="Panel.TFrame")
        buttons.pack(fill="x", padx=16, pady=14)

        def _close():
            try:
                dialog.grab_release()
            except Exception:
                pass
            self._recommend_dialog = None
            dialog.destroy()

        def _apply():
            self._set_preset(name)
            self._append(f"Applied recommended preset: {name}")
            _close()

        ttk.Button(buttons, text="Apply recommendation", style="Accent.TButton", command=_apply).pack(side="left")
        ttk.Button(buttons, text="Keep mine", command=_close).pack(side="left", padx=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", _close)

    # ------------------------------------------------------------------
    # Reports navigation (one click to every audit artifact)
    # ------------------------------------------------------------------
    def _reveal(self, path: Path, missing: str):
        import os as _os
        if not path.exists():
            return messagebox.showinfo("Reports", missing)
        try:
            if sys.platform == "win32":
                _os.startfile(str(path))  # noqa: S606 - local GUI affordance
            else:
                messagebox.showinfo("Reports", str(path))
        except OSError as error:
            messagebox.showerror("Reports", str(error))

    def _open_report(self):
        path = self._artifact_path("verification-report.json")
        if path is None:
            return messagebox.showinfo("Reports", "No completed output folder yet.")
        self._reveal(path, "No verification report in the last output yet.")

    def _open_resolve_metadata(self):
        path = self._artifact_path("resolve-metadata.csv")
        if path is None:
            return messagebox.showinfo("Reports", "No Resolve Comments CSV was created for the last run.")
        self._reveal(path, "No Resolve Comments CSV was created for the last run.")

    def _open_checksums(self):
        path = self._artifact_path("checksums.sha256")
        if path is None:
            return messagebox.showinfo("Reports", "No completed output folder yet.")
        self._reveal(path, "No checksums manifest in the last output yet.")

    def _open_previews(self):
        target = self.last_destination
        if target is None:
            return messagebox.showinfo("Reports", "No completed output folder yet.")
        self._reveal(target / "previews", "No previews (preview mode was off).")

    def _open_log_folder(self):
        self._reveal(ROOT / "logs", "No logs folder yet.")

    def _open_output(self):
        target = self.last_destination
        if target is None or not target.exists():
            return messagebox.showinfo("Open output", "No completed output folder yet.")
        self._reveal(target, "Output folder is gone.")

    def _bundle(self):
        target = self.last_destination
        if target is None or not target.exists():
            return messagebox.showinfo("Support bundle", "No completed output folder yet.")
        try:
            import cdng_lossless_mvp as mvp
            bundle = mvp.export_support_bundle(target, ROOT)
            self._append(f"Support bundle: {bundle}")
            self.status.set(f"Support bundle written: {bundle.name}")
        except OSError as error:
            messagebox.showerror("Support bundle", str(error))

    # ------------------------------------------------------------------
    # Batch queue (compact table, same fail-closed semantics)
    # ------------------------------------------------------------------
    def _guard_idle(self, what: str):
        if self.batch_active or self.process is not None:
            messagebox.showinfo("Busy", f"Cannot {what} while conversion or queue work is running.")
            return False
        return True

    def _run_settings_snapshot(self) -> dict:
        """Snapshot of every run setting a queue item must preserve."""
        return {
            "preset": self.preset.get(),
            "workers": self.workers.get(),
            "verify_mode": self.verify_mode.get(),
            "tile_layout": self.tile_layout.get(),
            "strict_guardrails": self.strict_guardrails.get(),
            "allow_low_space": self.allow_low_space.get(),
            "resume": self.resume.get(),
            "keep_output_artifacts": self.keep_output_artifacts.get(),
        }

    def _apply_settings_snapshot(self, item: dict) -> None:
        self._set_preset(item.get("preset", self.preset.get()))
        self.workers.set(item.get("workers", self.workers.get()))
        self.verify_mode.set(item.get("verify_mode", self.verify_mode.get()))
        self.tile_layout.set(item.get("tile_layout", self.tile_layout.get()))
        self.strict_guardrails.set(item.get("strict_guardrails", self.strict_guardrails.get()))
        self.allow_low_space.set(item.get("allow_low_space", self.allow_low_space.get()))
        self.resume.set(item.get("resume", self.resume.get()))
        self.keep_output_artifacts.set(item.get("keep_output_artifacts", self.keep_output_artifacts.get()))

    def _enqueue(self):
        if not self._guard_idle("enqueue"):
            return
        raw_source, raw_destination = self.source.get().strip(), self.destination.get().strip()
        if not raw_source or not raw_destination:
            return messagebox.showerror("Enqueue", "Fill in source and output folders first (one pair per queue item).")
        if not Path(raw_source).is_dir():
            return messagebox.showerror("Enqueue", "Source folder does not exist.")
        item = {"source": raw_source, "destination": raw_destination,
                "status": "pending", "progress": "—", **self._run_settings_snapshot()}
        self.batch.append(item)
        self._refresh_queue()
        self._append(f"Queued #{len(self.batch)}: {raw_source} -> {raw_destination}")

    def _dequeue(self):
        if not self._guard_idle("modify the queue"):
            return
        try:
            picked = self.queue_tree.selection()
        except Exception:
            return
        doomed = set()
        for item_id in picked:
            try:
                doomed.add(int(item_id))
            except ValueError:
                pass
        self.batch = [item for index, item in enumerate(self.batch)
                      if index not in doomed or item.get("status") == "running"]
        self._refresh_queue()

    def _refresh_queue(self):
        try:
            existing = list(self.queue_tree.get_children())
        except Exception:
            existing = []
        desired = []
        display_items = [(str(index), item, index + 1)
                         for index, item in enumerate(self.batch)]
        for iid, item, num in display_items:
            desired.append(iid)
            mode = "Compression"
            values = (num, item.get("display_name", Path(item["source"]).name), item.get("preset", self.preset.get()),
                      mode, item["status"], item.get("progress", "—"))
            row_tags = ("alternate",) if num % 2 == 0 else ()
            if iid in existing:
                self.queue_tree.item(iid, values=values, tags=row_tags)
            else:
                self.queue_tree.insert("", "end", iid=iid, values=values, tags=row_tags)
        for iid in existing:
            if iid not in desired:
                self.queue_tree.delete(iid)
        try:
            self.queue_tree.selection_set(tuple(iid for iid in desired if iid in self.queue_tree.selection()))
        except Exception:
            pass
        if not self.batch:
            self.queue_empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.queue_empty.place_forget()

    def _start_queue(self):
        if self.batch_active:
            return messagebox.showinfo("Queue", "The queue is already running.")
        if self.process is not None:
            return messagebox.showinfo("Queue", "A conversion is already running; wait for it to finish.")
        if not any(item["status"] == "pending" for item in self.batch):
            return messagebox.showinfo("Queue", "Nothing pending: enqueue folders first.")
        self._batch_scope = None
        self.batch_active = True
        self._append(f"Queue started ({sum(1 for i in self.batch if i['status'] == 'pending')} pending).")
        self._batch_next(None)

    def _batch_next(self, prev_code: int | None):
        if not self.batch_active:
            return
        if self._stop_requested:
            self._stop_requested = False
            self.batch_active = False
            self._batch_scope = None
            if self._batch_running is not None:
                self._batch_running["status"] = "skipped"
                self._batch_running = None
            self._refresh_queue()
            self.status.set("Queue stopped. Completed output is retained; nothing is deleted.")
            self.progress_title.set("Idle — nothing running.")
            return
        if self._batch_running is not None and prev_code is not None:
            self._batch_running["status"] = "done" if prev_code == 0 else "failed"
            if prev_code != 0 and not self.continue_on_error.get():
                self._append(f"Queue stopped on failure (exit {prev_code}); fix and requeue.")
                self.batch_active = False
                self._batch_running = None
                self._batch_scope = None
                self._refresh_queue()
                return
            self._batch_running = None
        while True:
            active_items = self._active_queue_items()
            nxt = next((item for item in active_items if item["status"] == "pending"), None)
            if nxt is None:
                self.batch_active = False
                self._batch_scope = None
                self._refresh_queue()
                done = sum(1 for i in active_items if i["status"] == "done")
                failed = sum(1 for i in active_items if i["status"] in ("failed", "skipped"))
                self.status.set(f"Queue complete: {done} done, {failed} failed/skipped.")
                self.progress_title.set("Queue complete.")
                return
            self.source.set(nxt["source"])
            self.destination.set(nxt["destination"])
            self._apply_settings_snapshot(nxt)
            nxt["status"] = "running"
            self._batch_running = nxt
            self._refresh_queue()
            if self._start_run(True):
                return
            nxt["status"] = "skipped"
            self._batch_running = None
            self._refresh_queue()

    def _active_queue_items(self) -> list[dict]:
        if self._batch_scope is None:
            return self.batch
        return [item for item in self.batch if id(item) in self._batch_scope]


    def _read_output(self):
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            self.events.put(("output", line.rstrip()))
        code = self.process.wait()
        self.events.put(("finished", code))

    def _stop(self):
        # The converter runs a ProcessPoolExecutor. terminate() kills only the
        # parent on Windows and leaves workers encoding into the output folder,
        # so the whole tree has to go. taskkill /T does that in one call.
        # Stop is sticky: _batch_next checks _stop_requested so a killed run
        # never auto-advances the queue.
        self._stop_requested = True
        if self.process:
            pid = self.process.pid
            stopped = False
            if sys.platform == "win32":
                try:
                    completed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    stopped = completed.returncode == 0
                    if not stopped:
                        try:
                            self.process.terminate()
                            stopped = True
                        except OSError:
                            pass
                except OSError as error:
                    try:
                        self.process.terminate()
                        stopped = True
                    except OSError:
                        pass
                    self._append(f"WARNING: could not kill the worker tree ({error}); stopped the parent only.")
            else:
                try:
                    self.process.terminate()
                    stopped = True
                except OSError:
                    pass
            if stopped:
                self.status.set("Stopped. Completed output is retained; nothing is deleted.")
                if self._batch_running is not None:
                    self._batch_running["status"] = "skipped"
                    self._batch_running = None
                self.batch_active = False
                self._refresh_queue()
            else:
                self.status.set("Stop failed: workers may still be running. Check Task Manager.")
                self._append("WARNING: stop failed; workers may still be encoding into the output folder.")
                return
        self.cancel.configure(state="disabled")

    def _drain_events(self):
        import re
        try:
            for _ in range(100):
                try:
                    self._drain_one(re)
                except queue.Empty:
                    break
                except Exception as error:
                    self._append(f"WARNING: interface event failed ({type(error).__name__}: {error}); pump continues.")
        finally:
            self.after(100, self._drain_events)

    def _drain_one(self, re):
        event = self.events.get_nowait()
        output_line = False
        if isinstance(event, tuple):
            kind = event[0]
            if kind == "recommend":
                line = "RECOMMEND: " + str(event[1])
            elif kind == "recommend_failed":
                line = str(event[1])
            elif kind == "finished":
                line = f"__FINISHED__:{event[1]}"
            elif kind == "output":
                line = str(event[1])
                output_line = True
            else:
                line = str(event[1])
        else:
            line = event
        if not output_line and line.startswith("RECOMMEND: "):
            try:
                rec = json.loads(line.partition(": ")[2])
                self._last_recommend = rec
                name = self._recommendation_name(rec.get("recommendation", ""), "?")
                self.recommend_result.set(f"Recommended: {name} (~{rec.get('quality_ratio', 0):.1f}×)")
                self.recommend_apply.pack(side="right", padx=(8, 0))
                self._append("Recommendation: " + json.dumps(rec, indent=2))
                self.status.set("Preset recommendation ready.")
                self.progress_title.set("Idle — nothing running.")
                self._show_recommend_dialog(rec)
            except Exception as error:
                self._append(f"Recommendation failed to parse: {error}")
            return
        if not output_line and line.startswith("RECOMMEND failed:"):
            self._append(line)
            self.status.set("Preset recommendation failed; see log.")
            self.progress_title.set("Idle — nothing running.")
            return
        if not output_line and line.startswith("__FINISHED__:"):
            code = int(line.split(":", 1)[1])
            if self.last_destination is not None:
                self._archive_output_artifacts(self.last_destination)
            self.process = None
            self._fps_samples = []
            self._set_config_enabled(True)
            self.start.configure(state="normal")
            self.cancel.configure(state="disabled")
            stopped = self._stop_requested
            ok = code == 0 and not stopped
            self._progress_finish_sequence(ok, stopped=stopped)
            self._stop_requested = False
            if stopped:
                self.progress_title.set("Stopped — see Details.")
                self.status.set("Stopped. Completed output is retained; nothing is deleted.")
                self._set_footer_default()
                if self.batch_active:
                    self.batch_active = False
                    if self._batch_running is not None:
                        self._batch_running["status"] = "skipped"
                        self._batch_running = None
                    self._refresh_queue()
                return
            self.progress_title.set("Completed successfully." if ok else "Stopped/failed — see Details.")
            self.status.set("Completed successfully." if ok else f"Stopped/failed (exit {code}). Inspect the log; partial output is retained.")
            self.complete_row.grid(row=8, column=0, sticky="w", pady=(8, 0))
            self._set_footer_default()
            if self.batch_active:
                self.after(200, lambda: self._batch_next(code))
            return
        self._append(line)
        match = re.search(r"^\[(\d+)/(\d+)\]\s*(.*)$", line)
        if match:
            done, total = int(match.group(1)), int(match.group(2))
            rel = match.group(3).strip()
            self.frames_done, self.frames_total = done, total
            now = time.perf_counter()
            elapsed = max(0.001, now - self.started)
            # GUI-only rolling average over ~10 s of progress lines.
            self._fps_samples.append((now, done))
            cutoff = now - 10.0
            while len(self._fps_samples) > 2 and self._fps_samples[0][0] < cutoff:
                self._fps_samples.pop(0)
            first_t, first_done = self._fps_samples[0]
            span = now - first_t
            if len(self._fps_samples) >= 2 and span > 0.05:
                fps = (done - first_done) / span
            else:
                fps = done / elapsed
            remaining = max(0, total - done)
            eta = remaining / fps if fps > 0 else 0.0
            percent = (100.0 * done / total) if total else 0.0

            def _fmt(seconds: float) -> str:
                seconds = max(0, int(seconds))
                return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

            self._progress_frame(done, total)
            if rel:
                self.current_file.set(self._middle_ellipse(Path(rel).name))
            self.status.set(f"{fps:.1f} fps   •   ETA {_fmt(eta)}   •   elapsed {_fmt(elapsed)}")
            for item in self.batch:
                if item.get("status") == "running":
                    item["progress"] = f"{percent:.0f}%"
            self._refresh_queue()


if __name__ == "__main__":
    gui_theme.apply_dpi_awareness()
    SigmaFpApp().mainloop()
