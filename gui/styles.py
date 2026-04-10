"""Visual constants and helpers for the YTYoink GUI."""

import tkinter as tk
from tkinter import ttk

# ── Colors ── Catppuccin Mocha inspired, slightly softened
BG_MAIN = "#181825"       # Base — deepest background
BG_SECTION = "#1e1e2e"    # Surface 0 — card/section background
BG_INPUT = "#313244"       # Surface 2 — input fields
BG_HOVER = "#45475a"       # Overlay 0 — hover state for inputs
BG_BUTTON = "#45475a"      # Button default
BG_BUTTON_HOVER = "#585b70"  # Button hover
BG_BUTTON_ACCENT = "#89b4fa"  # Primary accent button
BG_BUTTON_ACCENT_HOVER = "#b4d0fb"  # Lighter on hover
BG_BUTTON_CANCEL = "#f38ba8"
BG_BUTTON_CANCEL_HOVER = "#f5a3b8"
FG_TEXT = "#cdd6f4"         # Main text
FG_LABEL = "#a6adc8"        # Secondary text
FG_ACCENT = "#89b4fa"       # Links, highlights
FG_SUCCESS = "#a6e3a1"
FG_ERROR = "#f38ba8"
FG_WARN = "#fab387"
FG_DIM = "#6c7086"
FG_BUTTON_ACCENT = "#181825"  # Dark text on accent buttons
BORDER_COLOR = "#313244"     # Subtle borders

# ── Fonts
FONT_HEADING = ("Cascadia Code", 17, "bold")
FONT_SUBHEADING = ("Segoe UI", 10)
FONT_LABEL = ("Segoe UI", 10)
FONT_LABEL_BOLD = ("Segoe UI Semibold", 10)
FONT_INPUT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO = ("Cascadia Code", 9)
FONT_BUTTON = ("Segoe UI Semibold", 10)
FONT_BUTTON_SM = ("Segoe UI", 9)

# ── Dimensions
PAD_X = 12
PAD_Y = 6
PAD_SECTION = 10
PAD_CARD = 12            # Internal padding for card frames
ENTRY_WIDTH = 50
THUMBNAIL_SIZE = (140, 140)
COVER_PREVIEW_SIZE = (130, 130)
WINDOW_WIDTH = 660
WINDOW_HEIGHT = 860
WINDOW_MIN_WIDTH = 620
WINDOW_MIN_HEIGHT = 820


def apply_hover(widget, bg_normal, bg_hover, fg_normal=None, fg_hover=None):
    """Bind mouse enter/leave to change background (and optionally foreground)."""
    def on_enter(e):
        if str(widget.cget("state")) != "disabled":
            widget.config(bg=bg_hover)
            if fg_hover:
                widget.config(fg=fg_hover)

    def on_leave(e):
        if str(widget.cget("state")) != "disabled":
            widget.config(bg=bg_normal)
            if fg_normal:
                widget.config(fg=fg_normal)

    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


def make_card(parent, title: str = "", **kwargs):
    """Create a styled card frame (replaces LabelFrame for a cleaner look).

    Returns (outer_frame, inner_frame). Pack the outer_frame, put content in inner_frame.
    """
    outer = tk.Frame(parent, bg=BG_MAIN)

    if title:
        tk.Label(
            outer, text=title, font=FONT_LABEL_BOLD,
            bg=BG_MAIN, fg=FG_LABEL, anchor="w",
        ).pack(fill="x", padx=2, pady=(0, 3))

    inner = tk.Frame(
        outer, bg=BG_SECTION,
        highlightbackground=BORDER_COLOR,
        highlightcolor=BORDER_COLOR,
        highlightthickness=1,
        padx=PAD_CARD, pady=PAD_CARD,
    )
    inner.pack(fill="both", expand=True)

    return outer, inner


def configure_ttk_theme():
    """Apply custom ttk theme for combobox, progressbar, scrollbar."""
    style = ttk.Style()
    style.theme_use("default")

    # Progressbar
    style.configure(
        "Custom.Horizontal.TProgressbar",
        troughcolor=BG_INPUT,
        background=FG_ACCENT,
        thickness=5,
        borderwidth=0,
    )

    # Scrollbar
    style.configure(
        "Custom.Vertical.TScrollbar",
        background=BG_BUTTON,
        troughcolor=BG_MAIN,
        borderwidth=0,
        arrowcolor=FG_DIM,
        relief="flat",
    )
    style.map(
        "Custom.Vertical.TScrollbar",
        background=[("active", BG_BUTTON_HOVER)],
    )

    # Combobox
    style.configure(
        "Custom.TCombobox",
        fieldbackground=BG_INPUT,
        background=BG_BUTTON,
        foreground=FG_TEXT,
        arrowcolor=FG_LABEL,
        borderwidth=0,
        relief="flat",
    )
    style.map(
        "Custom.TCombobox",
        fieldbackground=[("readonly", BG_INPUT)],
        foreground=[("readonly", FG_TEXT)],
        selectbackground=[("readonly", BG_INPUT)],
        selectforeground=[("readonly", FG_TEXT)],
    )
