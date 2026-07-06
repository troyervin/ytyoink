"""Visual constants and helpers for the YTYoink GUI."""

import os
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

# ── Fonts — bundled Poppins (loaded process-private), Segoe fallbacks


def _load_bundled_fonts():
    """Register TTFs under <assets>/fonts for this process only (FR_PRIVATE).

    No system install, no admin rights — the font simply becomes available
    to this app's GDI calls, in dev and in the frozen exe alike.
    """
    try:
        import ctypes
        from paths import asset_dir
        fdir = os.path.join(asset_dir(), "fonts")
        if not os.path.isdir(fdir):
            return
        for fn in os.listdir(fdir):
            if fn.lower().endswith((".ttf", ".otf")):
                ctypes.windll.gdi32.AddFontResourceExW(
                    os.path.join(fdir, fn), 0x10, 0)  # 0x10 = FR_PRIVATE
    except Exception:
        pass


def _pick_ui_families():
    """Probe available fonts once.

    UI text: Poppins → Segoe UI Variable → Segoe UI.
    Title:   Bebas Neue (bundled) → Segoe UI Semibold → Cascadia.
    """
    ui, semibold, heading = "Segoe UI", "Segoe UI Semibold", "Cascadia Code"
    try:
        import tkinter as _tk
        import tkinter.font as _tkfont
        probe = _tk.Tk()
        try:
            probe.withdraw()
            families = set(_tkfont.families(probe))
        finally:
            probe.destroy()
        if "Poppins" in families:
            ui = "Poppins"
            semibold = ("Poppins SemiBold"
                        if "Poppins SemiBold" in families else "Poppins")
        elif "Segoe UI Variable Text" in families:
            ui = "Segoe UI Variable Text"
            semibold = ("Segoe UI Variable Text Semibold"
                        if "Segoe UI Variable Text Semibold" in families else ui)
        if "Bebas Neue" in families:
            heading = "Bebas Neue"
        elif "Segoe UI Semibold" in families:
            heading = "Segoe UI Semibold"
    except Exception:
        pass
    return ui, semibold, heading


_load_bundled_fonts()
_UI, _UI_SEMIBOLD, _HEADING_FAMILY = _pick_ui_families()

# Bebas Neue is tall/condensed — a bit larger for title presence
FONT_HEADING = (_HEADING_FAMILY, 23)
FONT_SUBHEADING = (_UI, 10)
FONT_LABEL = (_UI, 10)
FONT_LABEL_BOLD = (_UI_SEMIBOLD, 10)
FONT_INPUT = (_UI, 10)
FONT_SMALL = (_UI, 9)
FONT_MONO = ("Cascadia Code", 9)
FONT_BUTTON = (_UI_SEMIBOLD, 10)
FONT_BUTTON_SM = (_UI, 9)

# ── Dimensions
PAD_X = 12
PAD_Y = 6
PAD_SECTION = 10
PAD_CARD = 12            # Internal padding for card frames
ENTRY_WIDTH = 50
THUMBNAIL_SIZE = (64, 64)
COVER_PREVIEW_SIZE = (92, 92)
WINDOW_WIDTH = 660
WINDOW_HEIGHT = 640
WINDOW_MIN_WIDTH = 620
WINDOW_MIN_HEIGHT = 560


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

    Returns (outer_frame, inner_frame). Pack the outer_frame, put content in
    inner_frame. When a title is given, `outer.title_row` is the frame holding
    the title label — extra controls can pack into it (side="right").
    """
    outer = tk.Frame(parent, bg=BG_MAIN)
    outer.title_row = None

    if title:
        title_row = tk.Frame(outer, bg=BG_MAIN)
        title_row.pack(fill="x", padx=2, pady=(0, 3))
        tk.Label(
            title_row, text=title, font=FONT_LABEL_BOLD,
            bg=BG_MAIN, fg=FG_LABEL, anchor="w",
        ).pack(side="left")
        outer.title_row = title_row

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
