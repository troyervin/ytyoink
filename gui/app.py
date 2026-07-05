"""Main YTYoink GUI window — layout, event bindings, and threading orchestration."""

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from config import AppConfig
from downloader import CancelledError, DownloadPipeline, VideoInfo
from itunes import ItunesMatch, search_itunes
from metadata import clean_title, parse_filename
from paths import app_dir, asset_dir
from url_utils import normalize_youtube_url

from gui.styles import (
    BG_BUTTON, BG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER, BG_BUTTON_CANCEL,
    BG_BUTTON_CANCEL_HOVER, BG_BUTTON_HOVER, BG_INPUT, BG_MAIN, BG_SECTION,
    BORDER_COLOR, COVER_PREVIEW_SIZE, FG_ACCENT, FG_BUTTON_ACCENT, FG_DIM,
    FG_ERROR, FG_LABEL, FG_SUCCESS, FG_TEXT, FG_WARN, FONT_BUTTON,
    FONT_BUTTON_SM, FONT_HEADING, FONT_INPUT, FONT_LABEL, FONT_LABEL_BOLD,
    FONT_SMALL, FONT_SUBHEADING, PAD_CARD, PAD_SECTION, PAD_X, PAD_Y,
    THUMBNAIL_SIZE, WINDOW_HEIGHT, WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH,
    WINDOW_WIDTH, apply_hover, configure_ttk_theme, make_card,
)
from gui.widgets import (
    CheckboxEntry, CollapsibleStatus, CoverTile, ImagePreview, RoundButton,
    RoundField,
)

try:
    from PIL import Image, ImageGrab, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import windnd
    HAS_WINDND = True
except ImportError:
    HAS_WINDND = False


class YTYoinkApp(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()

        self.config = config
        self.pipeline = DownloadPipeline(config)

        # State
        self._current_url = ""
        self._video_info: VideoInfo | None = None
        self._itunes_match: ItunesMatch | None = None
        self._yt_meta: dict | None = None
        self._itunes_meta: dict | None = None
        self._yt_thumb_bytes: bytes | None = None
        self._itunes_cover_bytes: bytes | None = None
        self._custom_cover_path: str | None = None
        self._custom_cover_bytes: bytes | None = None
        self._last_download = ""
        self._last_download_path = ""
        self._fetch_thread: threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self._scrollbar_visible = False
        self._logo_photo = None  # prevent GC
        self._deps_ready = False  # set True after dependency check passes

        self._build_window()
        configure_ttk_theme()
        self._build_layout()
        self._set_icon()
        self._resize_job = None
        self.bind("<Configure>", self._on_root_resize)
        self.after(10, self._strip_native_titlebar)
        self.after(50, self._enforce_min_height)

    def _strip_native_titlebar(self):
        """Remove the Windows caption bar. Keeps the resize borders, the
        taskbar entry, and Win+arrow snapping — unlike overrideredirect.
        The app header provides its own title, icon, and window buttons."""
        try:
            import ctypes
            GWL_STYLE = -16
            WS_CAPTION = 0x00C00000
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id())
            get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            style = get_style(hwnd, GWL_STYLE)
            set_style(hwnd, GWL_STYLE, style & ~WS_CAPTION)
            # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
            try:
                # Dark frame + border painted in the app background color so
                # no light line shows at the window edges (Win10 1809+/Win11).
                dwm = ctypes.windll.dwmapi
                dark = ctypes.c_int(1)
                for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (+legacy)
                    if dwm.DwmSetWindowAttribute(
                            hwnd, attr, ctypes.byref(dark), 4) == 0:
                        break
                DWMWA_BORDER_COLOR = 34
                color = ctypes.c_int(0x00251818)  # COLORREF (BGR) of #181825
                dwm.DwmSetWindowAttribute(
                    hwnd, DWMWA_BORDER_COLOR, ctypes.byref(color), 4)
            except Exception:
                pass  # older Windows — keep the default border
        except Exception:
            pass

    def _toggle_maximize(self):
        self.state("normal" if self.state() == "zoomed" else "zoomed")

    def _force_redraw(self):
        """Repaint and re-sync layout after a zoom transition.

        With the caption stripped, leaving the zoomed state desyncs Tk's
        idea of the client area from the real window — a 1px geometry
        nudge forces a full re-layout, then a native repaint cleans up.
        """
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if self.state() == "normal":
                self.geometry(f"{w}x{h + 1}")
                self.after(30, lambda: self.geometry(f"{w}x{h}"))
            # Re-sync the scroll canvas (its embedded frame is what loses
            # its pixels), then force a native repaint of everything else.
            self.after(80, self._sync_canvas)

            def native_redraw():
                try:
                    import ctypes
                    # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW
                    flags = 0x1 | 0x4 | 0x80 | 0x100
                    inner = self.winfo_id()
                    outer = ctypes.windll.user32.GetParent(inner)
                    ctypes.windll.user32.RedrawWindow(outer, None, None, flags)
                    ctypes.windll.user32.RedrawWindow(inner, None, None, flags)
                except Exception:
                    pass

            self.after(140, native_redraw)
            # Second pass after everything has settled — clears any stale
            # pixels the first pass raced against.
            self.after(450, self._sync_canvas)
            self.after(500, native_redraw)
        except Exception:
            pass

    def _read_edition(self) -> str:
        """Read edition name from edition.key beside the exe. Defaults to TREE."""
        key_path = os.path.join(app_dir(), "edition.key")
        if not os.path.isfile(key_path):
            try:
                with open(key_path, "w", encoding="utf-8") as f:
                    f.write("TREE\n")
            except OSError:
                pass
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                word = f.read().strip()
                if word:
                    return word
        except OSError:
            pass
        return "TREE"

    def _make_fire_photo(self, size: int = 36) -> "ImageTk.PhotoImage | None":
        """Render 🔥 with a red-top → yellow-bottom gradient via PIL."""
        try:
            from PIL import Image, ImageDraw, ImageFont
            pad = 6
            canvas = size + pad * 2
            emoji_img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
            draw = ImageDraw.Draw(emoji_img)
            font = None
            for font_path in ("seguiemj.ttf", r"C:\Windows\Fonts\seguiemj.ttf"):
                try:
                    font = ImageFont.truetype(font_path, size)
                    break
                except OSError:
                    continue
            if font is None:
                return None
            draw.text((pad, pad), "🔥", font=font, embedded_color=True)
            # Build red→yellow gradient over the same canvas
            gradient = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
            g_draw = ImageDraw.Draw(gradient)
            for y in range(canvas):
                t = y / max(canvas - 1, 1)
                r, g_val, b = 220, int(t * 220), 0  # red at top → yellow at bottom
                g_draw.line([(0, y), (canvas, y)], fill=(r, g_val, b, 255))
            # Mask the gradient with the emoji's alpha channel
            gradient.putalpha(emoji_img.split()[3])
            bbox = emoji_img.getbbox()
            if bbox:
                gradient = gradient.crop(bbox)
            return ImageTk.PhotoImage(gradient)
        except Exception:
            return None

    def _enforce_min_height(self):
        """Size window to exactly fit compact content (log hidden) on open."""
        self.update_idletasks()
        content_h = self._main_frame.winfo_reqheight() + self._footer.winfo_reqheight()
        w = self.winfo_width() or WINDOW_WIDTH
        self.geometry(f"{w}x{content_h}")
        self.minsize(WINDOW_MIN_WIDTH,
                     min(content_h, self.winfo_screenheight() - 120))

    def _grow_to_fit(self):
        """Grow the window (screen space permitting) so newly revealed
        panels are visible without scrolling. Never shrinks."""
        self.update_idletasks()
        needed = self._main_frame.winfo_reqheight() + self._footer.winfo_reqheight()
        max_h = self.winfo_screenheight() - 120  # leave room for taskbar/title bar
        target = min(needed, max_h)
        if target > self.winfo_height():
            self.geometry(f"{self.winfo_width()}x{target}")
        self._update_log_visibility()

    def _on_root_resize(self, event):
        if event.widget is not self:
            return
        # Keep the maximize/restore glyph in sync with the window state
        if hasattr(self, "_max_btn"):
            glyph = "" if self.state() == "zoomed" else ""
            if self._max_btn.cget("text") != glyph:
                self._max_btn.config(text=glyph)
                # With the caption stripped, zoom transitions leave stale
                # pixels behind - force a full native repaint.
                self.after(50, self._force_redraw)
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._update_log_visibility)

    def _update_log_visibility(self):
        """Show the command-window log only when the window has spare room.

        Spare space is always measured against the *compact* layout (status
        line, no log) so showing/hiding the log can't oscillate. The window
        min-height tracks the compact layout, keeping content unscrollable.
        """
        self._resize_job = None
        sb = self._status_bar
        content_h = self._main_frame.winfo_reqheight()
        footer_req = self._footer.winfo_reqheight()
        if sb.log_visible:
            compact_footer = footer_req - sb.log_min_req() + sb.line_req()
        else:
            compact_footer = footer_req
        spare = self.winfo_height() - content_h - compact_footer
        if sb.log_visible and spare < 60:
            sb.set_log_visible(False)
        elif not sb.log_visible and spare > 140:
            sb.set_log_visible(True)
        self.minsize(WINDOW_MIN_WIDTH,
                     min(content_h + compact_footer,
                         self.winfo_screenheight() - 120))

    def _build_window(self):
        self._edition = self._read_edition()
        self.title(f"YTYoink - {self._edition} Edition")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.configure(bg=BG_MAIN)


    def _set_icon(self):
        base = asset_dir()
        # Try wm_iconphoto first — gives Windows exact bitmaps at multiple sizes
        # which avoids ICO parsing ambiguity and produces crisper taskbar icons.
        if HAS_PIL:
            try:
                icon_photos = []
                for px in (48, 32, 24, 16):
                    ico_path = os.path.join(base, "ytyoink.ico")
                    if os.path.isfile(ico_path):
                        ico = Image.open(ico_path)
                        ico.size = (px, px)
                        icon_photos.append(ImageTk.PhotoImage(ico.copy()))
                if icon_photos:
                    self._icon_photos = icon_photos  # prevent GC
                    self.wm_iconphoto(True, *icon_photos)
                    return
            except Exception:
                pass
        # Fallback to iconbitmap
        ico_path = os.path.join(base, "ytyoink.ico")
        if os.path.isfile(ico_path):
            try:
                self.iconbitmap(ico_path)
            except Exception:
                pass

    def _load_logo(self) -> "ImageTk.PhotoImage | None":
        """Load the logo PNG for the header (displayed at 38x38)."""
        if not HAS_PIL:
            return None
        base = asset_dir()
        # Use the highest-res source we have for best downscale quality
        for candidate in ("logo_48.png", "logo_32.png", "logo_24.png"):
            logo_path = os.path.join(base, candidate)
            if os.path.isfile(logo_path):
                break
        else:
            return None
        try:
            img = Image.open(logo_path).resize((38, 38), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _make_button(self, parent, text, command, bg, fg, hover_bg, hover_fg=None, font=None, padx=16, pady=4, state="normal"):
        """Create a rounded button with hover effect."""
        return RoundButton(
            parent, text, command, bg, fg, hover_bg, hover_fg,
            font=font or FONT_BUTTON, padx=padx, pady=pady, state=state,
        )

    def _build_layout(self):
        # Fixed footer — Download/Cancel, progress, and status log live here so
        # they stay visible when post-fetch content grows the scroll area.
        # Packed before the canvas so it keeps its minimum height when the
        # window shrinks; expand=True routes any spare height into the log.
        self._footer = tk.Frame(self, bg=BG_MAIN)
        self._footer.pack(side="bottom", fill="both", expand=True)
        tk.Frame(self._footer, bg=BORDER_COLOR, height=1).pack(fill="x")

        # Scroll area wrapper: fills the width, but its height tracks the
        # content's natural height (synced via canvas height config) so any
        # leftover window space goes to the footer's log, not empty background.
        scroll_wrap = tk.Frame(self, bg=BG_MAIN)
        scroll_wrap.pack(side="top", fill="x", expand=False)

        self._canvas = tk.Canvas(scroll_wrap, bg=BG_MAIN, highlightthickness=0, bd=0)
        self._scrollbar = ttk.Scrollbar(scroll_wrap, orient="vertical", command=self._canvas.yview, style="Custom.Vertical.TScrollbar")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)

        self._main_frame = tk.Frame(self._canvas, bg=BG_MAIN)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._main_frame, anchor="nw")

        self._main_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        frame = self._main_frame

        # ---- Header: acts as the title bar — branding, edition badge,
        # settings, and custom window controls (native caption is stripped) ----
        header_frame = tk.Frame(frame, bg=BG_MAIN)
        header_frame.pack(fill="x", padx=(PAD_SECTION, 4), pady=(6, 2))

        drag_widgets = [header_frame]

        self._logo_photo = self._load_logo()
        if self._logo_photo:
            logo_lbl = tk.Label(header_frame, image=self._logo_photo, bg=BG_MAIN)
            logo_lbl.pack(side="left", padx=(0, 8))
            drag_widgets.append(logo_lbl)

        title_lbl = tk.Label(
            header_frame, text="YTYoink", font=FONT_HEADING,
            bg=BG_MAIN, fg=FG_ACCENT,
        )
        title_lbl.pack(side="left")
        drag_widgets.append(title_lbl)

        self._fire_photo = self._make_fire_photo(size=20)
        if self._fire_photo:
            fire_lbl = tk.Label(
                header_frame, image=self._fire_photo, bg=BG_MAIN, padx=2)
        else:
            fire_lbl = tk.Label(
                header_frame, text="🔥", font=("Segoe UI Emoji", 17), bg=BG_MAIN)
        fire_lbl.pack(side="left")
        drag_widgets.append(fire_lbl)

        sub_lbl = tk.Label(
            header_frame, text="YouTube Audio Downloader", font=FONT_SMALL,
            bg=BG_MAIN, fg=FG_DIM,
        )
        sub_lbl.pack(side="left", padx=(10, 0))
        drag_widgets.append(sub_lbl)

        # Window controls — minimize / maximize / close (Segoe MDL2 glyphs)
        controls = tk.Frame(header_frame, bg=BG_MAIN)
        controls.pack(side="right")

        def win_btn(glyph, cmd, hover_bg, hover_fg=None):
            b = tk.Label(
                controls, text=glyph, font=("Segoe MDL2 Assets", 10),
                bg=BG_MAIN, fg=FG_LABEL, width=4, pady=5, cursor="hand2",
            )
            b.pack(side="left")
            b.bind("<Enter>", lambda e: b.config(bg=hover_bg, fg=hover_fg or FG_TEXT))
            b.bind("<Leave>", lambda e: b.config(bg=BG_MAIN, fg=FG_LABEL))
            b.bind("<Button-1>", lambda e: cmd())
            return b

        win_btn("", self.iconify, BG_BUTTON)                   # minimize
        self._max_btn = win_btn("", self._toggle_maximize, BG_BUTTON)
        win_btn("", self.destroy, BG_BUTTON_CANCEL, "#181825")  # close

        settings_btn = self._make_button(
            header_frame, "⚙", self._open_settings,
            BG_MAIN, FG_LABEL, BG_BUTTON,
            font=("Segoe UI Symbol", 13), padx=8, pady=0,
        )
        settings_btn.pack(side="right", padx=(0, 6))

        tk.Label(
            header_frame, text=f" {self._edition} EDITION ", font=("Cascadia Code", 6),
            bg=BG_MAIN, fg=FG_DIM,
            highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
            highlightthickness=1, padx=3, pady=0,
        ).pack(side="right", padx=(0, 10))

        # Drag-to-move + double-click-to-maximize on the branding area.
        # Dragging is handed to Windows (WM_NCLBUTTONDOWN + HTCAPTION) so it
        # moves in the native modal loop: smooth, no repaint flicker, and
        # Aero edge-snapping works like a real title bar.
        self._last_header_click = 0
        self._header_press = None

        def on_press(event):
            # Manual double-click detection — the native move loop would
            # otherwise swallow the second click.
            if event.time - self._last_header_click < 400:
                self._last_header_click = 0
                self._header_press = None
                self._toggle_maximize()
                return "break"
            self._last_header_click = event.time
            self._header_press = (event.x_root, event.y_root)

        def on_motion(event):
            # Enter the native move loop only after real movement — starting
            # it on the bare click causes a visible hiccup while Tk's
            # implicit grab and the loop fight over the pointer.
            if self._header_press is None or self.state() == "zoomed":
                return
            dx = abs(event.x_root - self._header_press[0])
            dy = abs(event.y_root - self._header_press[1])
            if dx + dy < 4:
                return
            self._header_press = None
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                ctypes.windll.user32.ReleaseCapture()
                # PostMessage, NOT SendMessage: the native move loop must run
                # from Tk's own message pump. SendMessage blocks inside the
                # ctypes call with the GIL released and Python callbacks from
                # the loop then crash the interpreter (fatal GIL error).
                ctypes.windll.user32.PostMessageW(hwnd, 0xA1, 2, 0)
            except Exception:
                pass

        def on_release(event):
            self._header_press = None

        for w in drag_widgets:
            w.bind("<Button-1>", on_press)
            w.bind("<B1-Motion>", on_motion)
            w.bind("<ButtonRelease-1>", on_release)

        # Thin separator under header
        tk.Frame(frame, bg=BORDER_COLOR, height=1).pack(fill="x", padx=PAD_SECTION, pady=(6, 8))

        # ---- Preference vars — frequently flipped toggles live inline,
        # set-once source/format preferences live in the settings popover ----
        self._format_var = tk.StringVar(value=self.config.format)
        self._pref_meta_var = tk.StringVar(value=self.config.metadata_source)
        self._pref_artwork_var = tk.StringVar(value=self.config.cover_source)
        self._keep_overrides_var = tk.BooleanVar(value=False)
        self._open_after_var = tk.BooleanVar(value=self.config.open_after_download)
        self._turbo_var = tk.BooleanVar(value=self.config.turbo_mode)
        self._settings_win = None

        def title_check(row, text, var, cmd=None):
            cb = tk.Checkbutton(
                row, text=text, variable=var, font=FONT_SMALL,
                bg=BG_MAIN, fg=FG_LABEL,
                activebackground=BG_MAIN, activeforeground=FG_TEXT,
                selectcolor=BG_INPUT, highlightthickness=0, bd=0,
                command=cmd,
            )
            cb.pack(side="right")
            return cb

        # ---- URL Input ----
        url_outer, url_inner = make_card(frame, "URL")
        url_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)
        self._turbo_cb = title_check(url_outer.title_row, "Turbo",
                                     self._turbo_var, self._on_turbo_change)
        self._make_tooltip(
            self._turbo_cb,
            "Turbo: fetching a URL downloads it immediately,\n"
            "skipping the preview step. Uses your preferred\n"
            "metadata and artwork sources from Settings (⚙).",
        )

        url_row = tk.Frame(url_inner, bg=BG_SECTION)
        url_row.pack(fill="x")

        self._url_field = RoundField(url_row, height=32)
        self._url_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._url_entry = self._url_field.entry

        self._fetch_btn = self._make_button(
            url_row, "Fetch Info", self._on_fetch_info,
            BG_BUTTON_ACCENT, FG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER,
            state="disabled",
        )
        self._fetch_btn.pack(side="right")

        self._url_entry.bind("<Return>", lambda e: self._on_fetch_info())
        self._url_entry.bind("<<Paste>>", lambda e: self.after(100, self._on_url_paste))
        self._url_entry.bind("<Control-v>", lambda e: self.after(100, self._on_url_paste))
        self._url_entry.bind("<FocusIn>", self._on_url_focus)

        # ---- Video Info (compact strip, hidden until first fetch) ----
        self._info_outer = tk.Frame(frame, bg=BG_MAIN)
        # NOT packed yet — shown after first fetch

        info_card = tk.Frame(
            self._info_outer, bg=BG_SECTION,
            highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
            highlightthickness=1, padx=8, pady=6,
        )
        info_card.pack(fill="x")

        self._thumb_preview = ImagePreview(info_card, size=THUMBNAIL_SIZE)
        self._thumb_preview.pack(side="left", padx=(0, 10))

        info_text = tk.Frame(info_card, bg=BG_SECTION)
        info_text.pack(side="left", fill="both", expand=True)

        self._info_title = tk.Label(
            info_text, text="", font=FONT_LABEL_BOLD,
            bg=BG_SECTION, fg=FG_TEXT, wraplength=480, justify="left", anchor="nw",
        )
        self._info_title.pack(anchor="w", pady=(2, 2))

        info_sub_row = tk.Frame(info_text, bg=BG_SECTION)
        info_sub_row.pack(anchor="w")

        self._info_uploader = tk.Label(
            info_sub_row, text="", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_LABEL, anchor="w",
        )
        self._info_uploader.pack(side="left")

        self._info_duration = tk.Label(
            info_sub_row, text="", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_DIM, anchor="w",
        )
        self._info_duration.pack(side="left", padx=(8, 0))

        # ---- Metadata Overrides ----
        meta_outer, meta_inner = make_card(frame, "Metadata  (check to override)")
        meta_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)
        self._meta_outer = meta_outer
        self._keep_overrides_cb = title_check(
            meta_outer.title_row, "Keep overrides on fetch",
            self._keep_overrides_var)
        self._make_tooltip(
            self._keep_overrides_cb,
            "Keeps your checked override fields filled in for the\n"
            "next fetch — useful when downloading several tracks\n"
            "that share the same artist, album, or year.",
        )

        # Metadata source toggle (shown only when iTunes data is available)
        self._meta_source_var = tk.StringVar(value=self.config.metadata_source)
        self._meta_source_frame = tk.Frame(meta_inner, bg=BG_SECTION)
        # Not packed yet — shown after fetch when iTunes data exists

        tk.Label(
            self._meta_source_frame, text="Source:", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_LABEL,
        ).pack(side="left", padx=(0, 6))

        tk.Radiobutton(
            self._meta_source_frame, text="iTunes", variable=self._meta_source_var,
            value="itunes", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_meta_source_change,
        ).pack(side="left", padx=(0, 10))

        tk.Radiobutton(
            self._meta_source_frame, text="YouTube", variable=self._meta_source_var,
            value="youtube", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_meta_source_change,
        ).pack(side="left")

        self._meta_title = CheckboxEntry(meta_inner, "Title")
        self._meta_title.pack(fill="x", pady=2)

        self._meta_artist = CheckboxEntry(meta_inner, "Artist")
        self._meta_artist.pack(fill="x", pady=2)

        self._meta_album = CheckboxEntry(meta_inner, "Album")
        self._meta_album.pack(fill="x", pady=2)

        self._meta_year = CheckboxEntry(meta_inner, "Year")
        self._meta_year.pack(fill="x", pady=2)

        self._meta_genre = CheckboxEntry(meta_inner, "Genre")
        self._meta_genre.pack(fill="x", pady=2)

        # Cover art tiles — click a tile to choose the artwork source.
        # Custom tile doubles as the drop/paste/browse target.
        self._cover_preview_frame = tk.Frame(meta_inner, bg=BG_SECTION)
        self._cover_preview_frame.pack(fill="x", pady=(PAD_Y, 0))

        self._cover_choice_var = tk.StringVar(value="itunes")

        self._covers_row = tk.Frame(self._cover_preview_frame, bg=BG_SECTION)
        self._covers_row.pack(pady=(2, 0))

        self._itunes_tile = CoverTile(
            self._covers_row, "iTunes", lambda: self._select_cover("itunes"),
            size=COVER_PREVIEW_SIZE,
        )
        self._yt_tile = CoverTile(
            self._covers_row, "YouTube", lambda: self._select_cover("youtube"),
            size=COVER_PREVIEW_SIZE,
        )
        # iTunes/YouTube tiles packed after fetch in _update_cover_previews

        self._custom_tile = CoverTile(
            self._covers_row, "Custom", self._on_custom_tile_click,
            size=COVER_PREVIEW_SIZE,
        )
        custom_hint = "Click: browse\nCtrl+V: paste\nor drop image" if HAS_WINDND \
            else "Click: browse\nCtrl+V: paste"
        self._custom_tile.set_placeholder(custom_hint)
        self._custom_tile.pack(side="left", padx=6)

        self._none_tile = CoverTile(
            self._covers_row, "None", lambda: self._select_cover("none"),
            size=COVER_PREVIEW_SIZE,
        )
        self._none_tile.set_placeholder("No artwork")
        self._none_tile.pack(side="left", padx=6)

        self._refresh_tile_selection()

        if HAS_WINDND:
            # Whole window accepts image drops → loads as custom artwork
            windnd.hook_dropfiles(self, func=self._on_drop_files)

        # Bind Ctrl+V globally for clipboard paste
        self.bind_all("<Control-v>", self._on_paste_image)

        # ---- Download Folder (inline row) ----
        folder_row = tk.Frame(frame, bg=BG_MAIN)
        folder_row.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)

        tk.Label(
            folder_row, text="Save to", font=FONT_LABEL_BOLD,
            bg=BG_MAIN, fg=FG_LABEL,
        ).pack(side="left", padx=(2, 8))

        self._folder_field = RoundField(folder_row, height=30, state="readonly")
        self._folder_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._folder_entry = self._folder_field.entry

        if self.config.download_folder:
            self._folder_entry.config(state="normal")
            self._folder_entry.insert(0, self.config.download_folder)
            self._folder_entry.config(state="readonly")

        open_btn = self._make_button(
            folder_row, "Open", self._on_open_folder,
            BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER, font=FONT_BUTTON_SM, padx=10,
        )
        open_btn.pack(side="right", padx=(4, 0))

        browse_btn = self._make_button(
            folder_row, "Browse...", self._on_browse_folder,
            BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER, font=FONT_BUTTON_SM, padx=10,
        )
        browse_btn.pack(side="right")

        # ---- Action Buttons (fixed footer) ----
        btn_frame = tk.Frame(self._footer, bg=BG_MAIN)
        btn_frame.pack(fill="x", padx=PAD_SECTION, pady=(10, 8))

        self._download_btn = self._make_button(
            btn_frame, "  Download  ", self._on_download,
            BG_BUTTON_ACCENT, FG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER,
            padx=28, pady=5, state="disabled",
        )
        self._download_btn.pack(side="left", padx=(0, 8))

        self._cancel_btn = self._make_button(
            btn_frame, "Cancel", self._on_cancel,
            BG_BUTTON_CANCEL, FG_BUTTON_ACCENT, BG_BUTTON_CANCEL_HOVER,
            padx=16, pady=5, state="disabled",
        )
        self._cancel_btn.pack(side="left")

        # Open-after toggle — right side, aligned with the action buttons
        tk.Checkbutton(
            btn_frame, text="Open file after download", variable=self._open_after_var,
            font=FONT_SMALL, bg=BG_MAIN, fg=FG_LABEL,
            activebackground=BG_MAIN, activeforeground=FG_TEXT,
            selectcolor=BG_INPUT, highlightthickness=0, bd=0,
            command=self._on_open_after_change,
        ).pack(side="right")

        # ---- Last Download (clickable — reveals file in Explorer) ----
        # Not packed until the first download completes (avoids an empty gap)
        self._last_dl_label = tk.Label(
            self._footer, text="", font=FONT_SMALL,
            bg=BG_MAIN, fg=FG_DIM, anchor="w", cursor="hand2",
        )
        self._last_dl_label.bind("<Button-1>", self._on_last_dl_click)

        # ---- Progress Bar ----
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(
            self._footer, variable=self._progress_var,
            maximum=100, mode="determinate",
            style="Custom.Horizontal.TProgressbar",
        )
        self._progress_bar.pack(fill="x", padx=PAD_SECTION, pady=(0, 4))

        # ---- Status Area — command-window log when roomy, one line when not ----
        self._status_bar = CollapsibleStatus(self._footer, height=3)
        self._status_bar.pack(fill="both", expand=True, padx=PAD_SECTION, pady=(3, 8))

    # ---- Scrolling ----

    def _on_frame_configure(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"),
                               height=self._main_frame.winfo_reqheight())
        self._update_scrollbar_visibility()

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        # Stretch the frame to fill the canvas when content is shorter than
        # the window — this gives pack's expand=True on the status bar room to grow.
        natural_h = self._main_frame.winfo_reqheight()
        self._canvas.itemconfig(
            self._canvas_window,
            height=max(natural_h, event.height),
        )
        self._update_scrollbar_visibility()

    def _sync_canvas(self):
        """Re-run full canvas sizing — call after dynamic content is shown/hidden."""
        self.update_idletasks()
        natural_h = self._main_frame.winfo_reqheight()
        canvas_h = self._canvas.winfo_height()
        canvas_w = self._canvas.winfo_width()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"),
                               height=natural_h)
        if canvas_w > 1:
            self._canvas.itemconfig(self._canvas_window, width=canvas_w)
        self._canvas.itemconfig(self._canvas_window, height=max(natural_h, canvas_h))
        self._update_scrollbar_visibility()
        self._update_log_visibility()

    def _update_scrollbar_visibility(self):
        # Compare the *natural* content height (not the stretched height) so
        # the scrollbar appears when the window is too small, not when it's large.
        natural_h = self._main_frame.winfo_reqheight()
        canvas_h = self._canvas.winfo_height()

        if natural_h > canvas_h:
            if not self._scrollbar_visible:
                self._scrollbar.pack(side="right", fill="y")
                self._scrollbar_visible = True
        else:
            if self._scrollbar_visible:
                self._scrollbar.pack_forget()
                self._scrollbar_visible = False
            self._canvas.yview_moveto(0)

    def _on_mousewheel(self, event):
        if self._scrollbar_visible:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ---- Event handlers ----

    def _on_url_focus(self, event=None):
        self.after(50, self._check_clipboard_for_url)

    def _check_clipboard_for_url(self):
        try:
            clip = self.clipboard_get().strip()
        except Exception:
            return
        if not clip or not self._is_youtube_url(clip):
            return
        current = self._url_entry.get().strip()
        if clip == current:
            return
        self._url_entry.delete(0, tk.END)
        self._url_entry.insert(0, clip)
        self.after(100, self._on_url_paste)

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        try:
            from urllib.parse import urlparse
            import re
            host = (urlparse(url).hostname or "").lower()
            return bool(re.search(r"(^|\.)youtube\.com$", host) or
                        re.match(r"^(www\.)?youtu\.be$", host))
        except Exception:
            return False

    def _on_url_paste(self):
        url = self._url_entry.get().strip()
        if url and url.startswith("http"):
            self._on_fetch_info()

    def _on_fetch_info(self):
        url = self._url_entry.get().strip()
        if not url:
            return
        if not url.startswith("http"):
            self._status_bar.append("Please enter a valid URL starting with http", "warning")
            return
        if not self._deps_ready:
            self._status_bar.append("Please wait — checking/installing dependencies...", "warning")
            return

        normalized = normalize_youtube_url(url)
        if normalized != url:
            self._url_entry.delete(0, tk.END)
            self._url_entry.insert(0, normalized)
            self._status_bar.append("Playlist parameters stripped; downloading single video only.", "dim")
        self._current_url = normalized

        self._set_ui_state("fetching")
        self._status_bar.clear()
        self._status_bar.append("Fetching video info...", "info")
        self._progress_var.set(0)

        self._fetch_thread = threading.Thread(target=self._fetch_worker, args=(normalized,), daemon=True)
        self._fetch_thread.start()
        self.after(2000, self._check_fetch_thread)

    def _check_fetch_thread(self):
        """Watchdog: detect if fetch thread died without triggering UI callbacks."""
        if self._fetch_thread is None:
            return
        if self._fetch_thread.is_alive():
            self.after(2000, self._check_fetch_thread)
            return
        # Thread is dead — check if UI is still stuck in fetching state
        if str(self._fetch_btn.cget("state")) == "disabled" and \
                str(self._download_btn.cget("state")) == "disabled" and \
                str(self._cancel_btn.cget("state")) == "disabled":
            self._status_bar.append("Fetch stopped unexpectedly.", "error")
            self._set_ui_state("idle")

    def _fetch_worker(self, url: str):
        try:
            info = self.pipeline.fetch_video_info(url)

            raw_title = info.raw_json.get("title", "") or ""
            raw_artist = info.raw_json.get("artist", "") or info.uploader or ""
            if raw_artist and raw_title:
                pseudo_fn = f"{raw_artist} - {raw_title}.tmp"
            elif raw_title:
                pseudo_fn = f"{raw_title}.tmp"
            else:
                pseudo_fn = "Unknown.tmp"

            meta = parse_filename(pseudo_fn)
            title_cleaned = clean_title(meta.title_raw, meta.featuring)

            yt_artist = meta.artist
            if not yt_artist or yt_artist in ("NA", "Unknown Artist"):
                yt_artist = info.uploader or ""
            yt_title = title_cleaned
            yt_album = meta.label or ""
            if not yt_album and info.album:
                yt_album = info.album

            yt_year = ""
            if info.release_year:
                yt_year = str(info.release_year)
            elif info.release_date and len(info.release_date) >= 4:
                yt_year = info.release_date[:4]
            elif info.release_timestamp:
                try:
                    from datetime import datetime
                    dt = datetime.utcfromtimestamp(int(info.release_timestamp))
                    yt_year = str(dt.year)
                except Exception:
                    pass

            yt_genre = info.genre or ""

            yt_meta = {
                "title": yt_title, "artist": yt_artist, "album": yt_album,
                "year": yt_year, "genre": yt_genre,
            }

            # iTunes search
            itunes_match = None
            try:
                it1, it2 = None, None
                if yt_artist and yt_title:
                    it1 = search_itunes(yt_artist, yt_title)
                    it2 = search_itunes(yt_title, yt_artist)
                elif yt_title:
                    it1 = search_itunes("", yt_title)
                elif yt_artist:
                    it1 = search_itunes(yt_artist, "")

                itunes_match = it1
                if it2 and (not it1 or it2.score > (it1.score + 2)):
                    itunes_match = it2
            except Exception:
                pass

            # Build iTunes metadata (fallback to YouTube for missing fields)
            itunes_meta = None
            if itunes_match:
                itunes_meta = {
                    "title": itunes_match.song or yt_title,
                    "artist": itunes_match.artist or yt_artist,
                    "album": itunes_match.album or yt_album,
                    "year": itunes_match.year or yt_year,
                    "genre": itunes_match.genre or yt_genre,
                }

            yt_thumb = None
            if info.thumbnail_url:
                try:
                    yt_thumb = self.pipeline.download_cover_bytes(info.thumbnail_url)
                except Exception:
                    pass

            itunes_cover = None
            if itunes_match and itunes_match.artwork_url:
                try:
                    itunes_cover = self.pipeline.download_cover_bytes(itunes_match.artwork_url)
                except Exception:
                    pass

            self.after(0, self._on_fetch_complete, info, itunes_match,
                       yt_meta, itunes_meta, yt_thumb, itunes_cover)

        except FileNotFoundError:
            self.after(0, self._on_fetch_error,
                       "yt-dlp not found. Please wait for auto-install to finish, or install it manually.")
        except Exception as e:
            self.after(0, self._on_fetch_error, str(e))

    def _on_fetch_complete(
        self, info, itunes_match,
        yt_meta, itunes_meta, yt_thumb, itunes_cover,
    ):
        self._video_info = info
        self._itunes_match = itunes_match
        self._yt_meta = yt_meta
        self._itunes_meta = itunes_meta
        self._yt_thumb_bytes = yt_thumb
        self._itunes_cover_bytes = itunes_cover

        self._info_title.config(text=info.title)
        self._info_uploader.config(text=info.uploader)
        mins, secs = divmod(info.duration, 60)
        self._info_duration.config(text=f"·  {mins}:{secs:02d}")

        if yt_thumb:
            self._thumb_preview.set_image(yt_thumb)
        else:
            self._thumb_preview.set_placeholder("No\nthumb")

        # Show video info strip (first time) — inserted before the metadata card
        self._info_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y, before=self._meta_outer)

        # Show/hide metadata source toggle
        if itunes_meta:
            self._meta_source_frame.pack(fill="x", pady=(0, 6), before=self._meta_title)
            # Default to saved preference (iTunes preferred when available)
            self._meta_source_var.set(self.config.metadata_source)
        else:
            self._meta_source_var.set("youtube")
            self._meta_source_frame.pack_forget()

        # Reset non-overridden fields so they pick up fresh auto-detected values.
        # Fields the user has explicitly checked are always preserved.
        if not self._keep_overrides_var.get():
            for w in (self._meta_title, self._meta_artist, self._meta_album,
                      self._meta_year, self._meta_genre):
                if not w.is_overridden():
                    w.reset()

        self._apply_meta_source()
        self._update_cover_previews()

        self._status_bar.append("Video info fetched successfully.", "success")
        self._set_ui_state("ready")
        self.after(0, self._sync_canvas)
        self.after(0, self._grow_to_fit)

        if self._turbo_var.get():
            self.after(50, self._on_download)

    def _on_fetch_error(self, error_msg):
        self._status_bar.append(f"Error: {error_msg}", "error")
        self._set_ui_state("idle")

    def _on_turbo_change(self):
        self.config.turbo_mode = self._turbo_var.get()
        self.config.save()

    def _make_tooltip(self, widget, text: str):
        """Show a themed tooltip under the widget after a short hover delay."""
        state = {"win": None, "job": None}

        def show():
            state["job"] = None
            if state["win"] is not None:
                return
            tw = tk.Toplevel(self)
            tw.wm_overrideredirect(True)
            tk.Label(
                tw, text=text, font=FONT_SMALL,
                bg=BG_SECTION, fg=FG_TEXT, justify="left",
                highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
                highlightthickness=1, padx=8, pady=5,
            ).pack()
            tw.update_idletasks()
            # Right-align to the widget so it never spills past the window
            x = widget.winfo_rootx() + widget.winfo_width() - tw.winfo_reqwidth()
            x = max(x, self.winfo_rootx() + 8)
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tw.wm_geometry(f"+{x}+{y}")
            state["win"] = tw

        def on_enter(event):
            if state["job"] is None and state["win"] is None:
                state["job"] = self.after(450, show)

        def on_leave(event):
            if state["job"]:
                self.after_cancel(state["job"])
                state["job"] = None
            if state["win"] is not None:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)

    def _open_settings(self):
        """Open (or focus) the settings window with the set-once preferences."""
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_set()
            return

        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=BG_SECTION)
        win.resizable(False, False)
        win.transient(self)
        win.geometry(f"+{self.winfo_rootx() + self.winfo_width() - 300}+{self.winfo_rooty() + 50}")
        self._settings_win = win

        body = tk.Frame(win, bg=BG_SECTION, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        def section(text, first=False):
            tk.Label(
                body, text=text, font=FONT_LABEL_BOLD,
                bg=BG_SECTION, fg=FG_LABEL,
            ).pack(anchor="w", pady=((0 if first else 10), 2))

        def radio_row(options, var, cmd):
            row = tk.Frame(body, bg=BG_SECTION)
            row.pack(anchor="w")
            for text, value in options:
                tk.Radiobutton(
                    row, text=text, variable=var, value=value,
                    font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
                    selectcolor=BG_INPUT, activebackground=BG_SECTION,
                    activeforeground=FG_TEXT, highlightthickness=0, bd=0,
                    command=cmd,
                ).pack(side="left", padx=(0, 10))

        section("Output format", first=True)
        radio_row([("M4A (AAC 256kbps)", "m4a"), ("MP3 (VBR Q0)", "mp3")],
                  self._format_var, self._on_format_change)

        section("Preferred metadata")
        radio_row([("iTunes", "itunes"), ("YouTube", "youtube")],
                  self._pref_meta_var, self._on_pref_meta_change)

        section("Preferred artwork")
        radio_row([("iTunes", "itunes"), ("YouTube", "youtube")],
                  self._pref_artwork_var, self._on_pref_artwork_change)

        tk.Label(
            body, text="Turbo skips the preview and auto-downloads on fetch\n"
                       "using the preferences above.",
            font=FONT_SMALL, bg=BG_SECTION, fg=FG_DIM, justify="left",
        ).pack(anchor="w", pady=(10, 0))

    def _on_format_change(self):
        self.config.format = self._format_var.get()
        self.config.save()

    # ---- Preferred Source card handlers ----

    def _on_pref_meta_change(self):
        """User changed metadata preference in the Preferred Source card.

        Only saves to config for future fetches — does NOT change the
        currently active metadata source toggle.
        """
        self.config.metadata_source = self._pref_meta_var.get()
        self.config.save()

    def _on_pref_artwork_change(self):
        """User changed artwork preference in the Preferred Source card.

        Only saves to config for future fetches — does NOT change the
        currently active artwork selection.
        """
        self.config.cover_source = self._pref_artwork_var.get()
        self.config.save()

    # ---- On-the-fly toggle handlers ----

    def _on_meta_source_change(self):
        """User switched metadata source on-the-fly in the metadata card.

        Ephemeral per-fetch override — does NOT update the saved preference.
        """
        self._apply_meta_source()

    def _select_cover(self, value: str):
        """Set the artwork choice and highlight the matching tile.

        Ephemeral per-fetch override — does NOT update the saved preference.
        """
        self._cover_choice_var.set(value)
        self._refresh_tile_selection()

    def _refresh_tile_selection(self):
        val = self._cover_choice_var.get()
        for value, tile in (("itunes", self._itunes_tile),
                            ("youtube", self._yt_tile),
                            ("custom", self._custom_tile),
                            ("none", self._none_tile)):
            tile.set_selected(value == val)

    def _on_custom_tile_click(self):
        """Custom tile: select it if an image is loaded, otherwise browse."""
        if self._custom_cover_bytes:
            self._select_cover("custom")
        else:
            self._on_browse_custom_art()

    def _load_custom_image(self, data: bytes, path: str | None):
        """Shared loader for custom artwork from browse, paste, or drop."""
        import tempfile

        if path is None:
            # Clipboard paste — save to temp file for the downloader pipeline
            path = os.path.join(tempfile.gettempdir(), "ytyoink_custom_cover.png")
            with open(path, "wb") as f:
                f.write(data)

        self._custom_cover_path = path
        self._custom_cover_bytes = data
        self._custom_tile.set_image(data)
        self._select_cover("custom")
        self.after(0, self._sync_canvas)

    def _on_browse_custom_art(self):
        """Open a file picker for custom cover art."""
        path = filedialog.askopenfilename(
            title="Select Cover Art",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            self._load_custom_image(data, path)
        except Exception as e:
            self._status_bar.append(f"Could not load image: {e}", "warning")

    def _on_paste_image(self, event=None):
        """Handle Ctrl+V — load a clipboard image as custom artwork.

        Never interferes with text pastes: grabclipboard() returns None
        for text, so pasting a URL into an entry works normally.
        """
        if not HAS_PIL:
            return

        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            return

        if clip is None:
            return

        import io

        if isinstance(clip, Image.Image):
            buf = io.BytesIO()
            clip.save(buf, format="PNG")
            self._load_custom_image(buf.getvalue(), None)
        elif isinstance(clip, list):
            # List of file paths from clipboard
            img_exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
            for fpath in clip:
                if isinstance(fpath, str) and os.path.isfile(fpath) and \
                        fpath.lower().endswith(img_exts):
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self._load_custom_image(data, fpath)
                    break

    def _on_drop_files(self, files):
        """Handle drag-and-drop files from Windows Explorer via windnd."""
        img_exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
        for fpath in files:
            if isinstance(fpath, bytes):
                fpath = fpath.decode("utf-8", errors="replace")
            if os.path.isfile(fpath) and fpath.lower().endswith(img_exts):
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self._load_custom_image(data, fpath)
                except Exception:
                    pass
                break

    def _apply_meta_source(self):
        """Populate metadata fields from the currently selected source."""
        source = self._meta_source_var.get()
        if source == "itunes" and self._itunes_meta:
            meta = self._itunes_meta
        elif self._yt_meta:
            meta = self._yt_meta
        else:
            return

        self._meta_title.set_auto_value(meta["title"])
        self._meta_artist.set_auto_value(meta["artist"])
        self._meta_album.set_auto_value(meta["album"])
        self._meta_year.set_auto_value(meta["year"])
        self._meta_genre.set_auto_value(meta["genre"])

    def _update_cover_previews(self):
        """Populate cover tiles after fetch. Order: iTunes, YouTube, Custom, None."""
        # Reset pack order each time so re-fetching doesn't scramble positions
        self._itunes_tile.pack_forget()
        self._yt_tile.pack_forget()
        self._custom_tile.pack_forget()
        self._none_tile.pack_forget()

        if self._itunes_cover_bytes:
            self._itunes_tile.set_image(self._itunes_cover_bytes)
            self._itunes_tile.pack(side="left", padx=6)
        if self._yt_thumb_bytes:
            self._yt_tile.set_image(self._yt_thumb_bytes)
            self._yt_tile.pack(side="left", padx=6)
        self._custom_tile.pack(side="left", padx=6)
        self._none_tile.pack(side="left", padx=6)

        # Custom art persists across fetches, but only stays *selected* if it
        # was the active choice — otherwise the saved preference applies.
        if self._custom_cover_bytes and self._cover_choice_var.get() == "custom":
            pass
        elif self._itunes_cover_bytes:
            pref = self.config.cover_source
            if pref not in ("itunes", "youtube", "none"):
                pref = "itunes"
            if pref == "youtube" and not self._yt_thumb_bytes:
                pref = "itunes"
            self._cover_choice_var.set(pref)
        elif self._yt_thumb_bytes:
            if self._cover_choice_var.get() == "itunes":
                self._cover_choice_var.set("youtube")
        elif self._cover_choice_var.get() in ("itunes", "youtube"):
            self._cover_choice_var.set("none")
        self._refresh_tile_selection()

    def _on_browse_folder(self):
        folder = filedialog.askdirectory(
            title="Select Download Folder",
            initialdir=self.config.download_folder or os.path.expanduser("~"),
        )
        if folder:
            self.config.download_folder = folder
            self.config.save()
            self._folder_entry.config(state="normal")
            self._folder_entry.delete(0, tk.END)
            self._folder_entry.insert(0, folder)
            self._folder_entry.config(state="readonly")

    def _on_open_folder(self):
        folder = self.config.download_folder
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            self._status_bar.append("Download folder not set or does not exist.", "warning")

    def _on_last_dl_click(self, event=None):
        if self._last_download_path and os.path.isfile(self._last_download_path):
            subprocess.run(["explorer", "/select,", self._last_download_path])

    def _on_open_after_change(self):
        self.config.open_after_download = self._open_after_var.get()
        self.config.save()

    def _on_download(self):
        if not self._current_url:
            self._status_bar.append("No URL to download.", "warning")
            return

        if not self.config.download_folder or not os.path.isdir(self.config.download_folder):
            self._status_bar.append("Please set a valid download folder.", "warning")
            return

        self._set_ui_state("downloading")
        self._status_bar.clear()
        self._progress_var.set(0)

        # Always pass all field values so the pipeline uses exactly what's shown
        overrides = {}
        for key, widget in [("title", self._meta_title), ("artist", self._meta_artist),
                            ("album", self._meta_album), ("year", self._meta_year),
                            ("genre", self._meta_genre)]:
            overrides[key] = widget.get_value()

        cover_choice = self._cover_choice_var.get()
        custom_cover = self._custom_cover_path if cover_choice == "custom" else None

        self.pipeline.status_callback = lambda msg: self.after(0, self._on_status, msg)
        self.pipeline.progress_callback = lambda pct, msg: self.after(0, self._on_progress, pct, msg)

        self._download_thread = threading.Thread(
            target=self._download_worker,
            args=(self._current_url, overrides, cover_choice, custom_cover),
            daemon=True,
        )
        self._download_thread.start()
        self.after(2000, self._check_download_thread)

    def _check_download_thread(self):
        """Watchdog: detect if download thread died without triggering UI callbacks."""
        if self._download_thread is None:
            return
        if self._download_thread.is_alive():
            self.after(2000, self._check_download_thread)
            return
        # Thread is dead — check if UI is still stuck in downloading state
        if str(self._download_btn.cget("state")) == "disabled" and \
                str(self._cancel_btn.cget("state")) == "normal":
            self._status_bar.append("Download thread stopped unexpectedly.", "error")
            self._progress_var.set(0)
            self._set_ui_state("ready")

    def _download_worker(self, url, overrides, cover_choice, custom_cover_path):
        try:
            result = self.pipeline.download(
                url=url, video_info=self._video_info,
                metadata_overrides=overrides, cover_choice=cover_choice,
                itunes_match=self._itunes_match,
                custom_cover_path=custom_cover_path,
            )
            self.after(0, self._on_download_complete, result)
        except CancelledError:
            self.after(0, self._on_download_cancelled)
        except Exception as e:
            self.after(0, self._on_download_error, str(e))

    def _on_download_complete(self, result):
        self._last_download = result.filename
        self._last_download_path = result.output_path
        if not self._last_dl_label.winfo_ismapped():
            self._last_dl_label.pack(
                fill="x", padx=PAD_SECTION + 2, pady=(0, 2),
                before=self._progress_bar,
            )
            # Footer grew a row — grow the window to match so the content
            # area isn't squeezed into showing a scrollbar.
            self.after(0, self._grow_to_fit)
        self._last_dl_label.config(
            text=f"Last download: {result.filename}",
            fg=FG_ACCENT,
        )
        self._last_dl_label.bind("<Enter>", lambda e: self._last_dl_label.config(fg=FG_TEXT))
        self._last_dl_label.bind("<Leave>", lambda e: self._last_dl_label.config(fg=FG_ACCENT))
        self._status_bar.append(f"Saved: {result.filename}", "success")
        self._progress_var.set(100)
        self._set_ui_state("ready")

        if self._open_after_var.get() and os.path.isfile(result.output_path):
            try:
                os.startfile(result.output_path)
            except Exception:
                pass

    def _on_download_cancelled(self):
        self._status_bar.append("Download cancelled.", "warning")
        self._progress_var.set(0)
        self._set_ui_state("ready")

    def _on_download_error(self, error_msg):
        self._status_bar.append(f"Error: {error_msg}", "error")
        self._progress_var.set(0)
        self._set_ui_state("ready")

    def _on_cancel(self):
        self.pipeline.cancel()
        self._cancel_btn.config(state="disabled")
        self._status_bar.append("Cancelling...", "warning")

    def _on_status(self, msg):
        self._status_bar.append(msg, "dim")

    def _on_progress(self, pct, msg):
        self._progress_var.set(pct)

    # ---- UI state ----

    def _set_ui_state(self, state):
        states = {
            "idle":        ("normal",   "normal",   "disabled", "disabled"),
            "fetching":    ("disabled", "disabled", "disabled", "disabled"),
            "ready":       ("normal",   "normal",   "normal",   "disabled"),
            "downloading": ("disabled", "disabled", "disabled", "normal"),
        }
        fetch, url, dl, cancel = states.get(state, states["idle"])
        self._fetch_btn.config(state=fetch, cursor="hand2" if fetch == "normal" else "")
        self._url_entry.config(state=url)
        self._download_btn.config(state=dl, cursor="hand2" if dl == "normal" else "")
        self._cancel_btn.config(state=cancel, cursor="hand2" if cancel == "normal" else "")

    # ---- Startup tasks ----

    def run_startup_tasks(self):
        def worker():
            # Clean up stale temp folders from previous runs (respects locks)
            from downloader import cleanup_stale_temp
            removed = cleanup_stale_temp()
            if removed:
                self.after(0, self._status_bar.append,
                           f"Cleaned up {removed} stale temp folder{'s' if removed > 1 else ''}.", "dim")

            from dependencies import ensure_dependency, update_ytdlp, update_self
            from version import APP_VERSION, GITHUB_REPO

            def cb(msg):
                if not msg:
                    return
                if msg.endswith(" ready."):
                    return  # suppress "ffmpeg ready." / "yt-dlp ready." noise
                if msg.endswith("%"):
                    self.after(0, self._status_bar.update_last, msg, "dim")
                else:
                    self.after(0, self._status_bar.append, msg, "dim")

            # Check for app update first — if restarting, skip everything else
            self.after(0, self._status_bar.append, "Checking for updates...", "dim")
            if update_self(GITHUB_REPO, APP_VERSION, status_callback=cb):
                self.after(0, self._apply_self_update)
                return

            if not ensure_dependency("ffmpeg", "FFmpeg.FFmpeg", status_callback=cb):
                self.after(0, self._status_bar.append, "ffmpeg is required but could not be installed.", "error")
            if not ensure_dependency("yt-dlp", "yt-dlp.yt-dlp", status_callback=cb):
                self.after(0, self._status_bar.append, "yt-dlp is required but could not be installed.", "error")

            update_ytdlp(status_callback=cb)
            self._deps_ready = True
            self.after(0, self._fetch_btn.config, state="normal", cursor="hand2")
            self.after(0, self._status_bar.append, "Ready.", "success")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_self_update(self):
        self._status_bar.append(
            "Update ready — YTYoink will close and restart itself...", "success")
        self.after(1500, self.destroy)

    def prompt_download_folder(self):
        if not self.config.download_folder or not os.path.isdir(self.config.download_folder):
            default = os.path.join(os.path.expanduser("~"), "Downloads")
            folder = filedialog.askdirectory(
                title="Select Download Folder (first-time setup)",
                initialdir=default,
            )
            self.config.download_folder = folder or default
            if not folder:
                os.makedirs(default, exist_ok=True)
            self.config.save()

            self._folder_entry.config(state="normal")
            self._folder_entry.delete(0, tk.END)
            self._folder_entry.insert(0, self.config.download_folder)
            self._folder_entry.config(state="readonly")
