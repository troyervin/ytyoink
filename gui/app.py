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
    RoundField, monitor_bounds,
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
        self._queue: list[str] = []
        self._queue_total = 0
        self._queue_running = False
        self._queue_review = False    # review-each mode: wait for the user
        self._queue_source = None     # per-batch metadata/artwork override
        self._queue_album_hint = None  # (artist, album) picked via Wrong match?
        self._ui_state = "idle"

        self._build_window()
        configure_ttk_theme()
        self._build_layout()
        self._set_icon()
        self._resize_job = None
        self.bind("<Configure>", self._on_root_resize)
        self.after(10, self._strip_native_titlebar)
        self.after(50, self._enforce_min_height)

    def destroy(self):
        # Tidy: remove the session's clipboard-paste cover temp file, if any
        # (browsed cover files are the user's own and are never touched)
        try:
            import tempfile
            paste_png = os.path.join(tempfile.gettempdir(),
                                     "ytyoink_custom_cover.png")
            if os.path.isfile(paste_png):
                os.remove(paste_png)
        except Exception:
            pass
        super().destroy()

    def _strip_native_titlebar(self, window=None):
        """Remove the Windows caption bar (of the main window or a popup).
        Keeps the resize borders, the taskbar entry, and Win+arrow snapping,
        unlike overrideredirect. Our own header provides title and buttons."""
        try:
            import ctypes
            GWL_STYLE = -16
            WS_CAPTION = 0x00C00000
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent((window or self).winfo_id())
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

    def _clamp_zoomed_to_workarea(self):
        """Caption-stripped windows maximize to the FULL screen, covering
        the taskbar — resize the zoomed window to the monitor work area."""
        if self.state() != "zoomed":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id())
            monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest monitor
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return
            rw = mi.rcWork
            # SWP_NOZORDER | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, 0, rw.left, rw.top,
                                rw.right - rw.left, rw.bottom - rw.top,
                                0x0004 | 0x0010)
        except Exception:
            pass

    def _force_redraw(self):
        """Repaint and re-sync layout after a zoom transition.

        With the caption stripped, leaving the zoomed state desyncs Tk's
        idea of the client area from the real window — a 1px geometry
        nudge forces a full re-layout, then a native repaint cleans up.
        """
        try:
            def nudge():
                # Read the size fresh — during the restore animation the
                # height is transient, and pinning it via geometry() would
                # freeze the window at a wrong (too tall) size.
                if self.state() != "normal":
                    return
                w, h = self.winfo_width(), self.winfo_height()
                self.geometry(f"{w}x{h + 1}")
                self.after(30, lambda: self.geometry(f"{w}x{h}"))

            self.after(250, nudge)
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
                # Keep the maximized window off the taskbar area
                self.after(20, self._clamp_zoomed_to_workarea)
                # With the caption stripped, zoom transitions leave stale
                # pixels behind - force a full native repaint.
                self.after(50, self._force_redraw)
                # Final settle pass: earlier evaluations can race the
                # transition animation and read a transient height.
                self.after(700, self._update_log_visibility)
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._update_log_visibility)

    # Adaptive space budget: spare height goes to the log first (up to the
    # reserve), then into growing the artwork tiles, then back to the log.
    LOG_RESERVE = 160
    TILE_MAX = 300

    def _update_log_visibility(self):
        """Distribute spare window height: log, then artwork tile growth.

        Spare space is always measured against the *compact baseline*
        (status line, no log, base-size tiles) so nothing can oscillate.
        The window min-height tracks that baseline, keeping the content
        unscrollable while still allowing shrink-back.
        """
        self._resize_job = None
        sb = self._status_bar
        content_h = self._main_frame.winfo_reqheight()
        footer_req = self._footer.winfo_reqheight()
        if sb.log_visible:
            compact_footer = footer_req - sb.log_min_req() + sb.line_req()
        else:
            compact_footer = footer_req
        # Baseline content: as if tiles were at their base size
        base_content = content_h - (self._tile_size - self._tile_base)
        spare = self.winfo_height() - base_content - compact_footer

        if sb.log_visible and spare < 60:
            sb.set_log_visible(False)
        elif not sb.log_visible and spare > 140:
            sb.set_log_visible(True)

        # Grow tiles with whatever remains after the log's reserved slice
        if sb.log_visible:
            tile_extra = max(0, spare - self.LOG_RESERVE)
        else:
            tile_extra = 0
        target = min(self._tile_base + tile_extra, self.TILE_MAX)
        if abs(target - self._tile_size) >= 8 or \
                (target == self._tile_base and self._tile_size != target):
            self._tile_size = target
            for tile in (self._itunes_tile, self._yt_tile,
                         self._custom_tile, self._none_tile):
                tile.set_display_size(target)

        self.minsize(WINDOW_MIN_WIDTH,
                     min(base_content + compact_footer,
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
            if self._header_press is None:
                return
            dx = abs(event.x_root - self._header_press[0])
            dy = abs(event.y_root - self._header_press[1])
            if dx + dy < 4:
                return
            self._header_press = None
            if self.state() == "zoomed":
                # Like a real title bar: dragging a maximized window
                # restores it under the cursor, then the drag continues.
                xfrac = event.x_root / max(self.winfo_width(), 1)
                self.state("normal")
                self.update_idletasks()
                new_x = int(event.x_root - xfrac * self.winfo_width())
                new_y = max(event.y_root - 24, 0)
                self.geometry(f"+{new_x}+{new_y}")
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
        self._ask_playlist_var = tk.BooleanVar(value=self.config.ask_playlist)
        self._ignore_mixes_var = tk.BooleanVar(value=self.config.ignore_mixes)
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
            "metadata and artwork sources from Settings (⚙).\n"
            "Careful: with a playlist link, Turbo downloads\n"
            "the whole playlist.",
        )

        url_row = tk.Frame(url_inner, bg=BG_SECTION)
        url_row.pack(fill="x")

        self._url_field = RoundField(url_row, height=32)
        self._url_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._url_entry = self._url_field.entry

        # Search sits to the RIGHT of Fetch Info: fetch belongs with the
        # URL box beside it, search is its own entry point
        self._search_btn = self._make_button(
            url_row, "SearchYT", self._open_search,
            BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER, state="disabled",
        )
        self._search_btn.pack(side="right")

        self._fetch_btn = self._make_button(
            url_row, "Fetch Info", self._on_fetch_info,
            BG_BUTTON_ACCENT, FG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER,
            state="disabled",
        )
        self._fetch_btn.pack(side="right", padx=(0, 6))

        self._url_entry.bind("<Return>", lambda e: self._on_fetch_info())
        # Note: Ctrl+V also fires <<Paste>>, so binding <Control-v> too
        # would run the auto-fetch twice for one paste.
        self._url_entry.bind("<<Paste>>", lambda e: self.after(100, self._on_url_paste))
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

        title_row = tk.Frame(info_text, bg=BG_SECTION)
        title_row.pack(anchor="w", fill="x", pady=(2, 2))

        self._info_title = tk.Label(
            title_row, text="", font=FONT_LABEL_BOLD,
            bg=BG_SECTION, fg=FG_TEXT, wraplength=460, justify="left", anchor="nw",
        )
        self._info_title.pack(side="left")

        # Already-downloaded badge (hidden until a fetch matches history)
        self._dup_photo = self._make_alert_photo(16)
        self._dup_badge = tk.Label(title_row, image=self._dup_photo,
                                   bg=BG_SECTION)
        self._dup_tip_text = ""
        self._make_tooltip(self._dup_badge, lambda: self._dup_tip_text)

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
            "next fetch. Useful when downloading several tracks\n"
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

        wrong_match = tk.Label(
            self._meta_source_frame, text="Wrong match?", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_ACCENT, cursor="hand2",
        )
        wrong_match.pack(side="right")
        wrong_match.bind("<Button-1>", lambda e: self._open_match_picker())

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
        self._tile_base = COVER_PREVIEW_SIZE[0]
        self._tile_size = self._tile_base

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

        self._history_btn = self._make_button(
            btn_frame, "History", self._open_history,
            BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER, pady=5,
        )
        self._history_btn.pack(side="left", padx=(8, 0))

        # Skip / Cancel batch — only shown while reviewing a queue
        self._skip_btn = self._make_button(
            btn_frame, "Skip", self._skip_current,
            BG_BUTTON, FG_WARN, BG_BUTTON_HOVER, pady=5,
        )
        self._cancel_batch_btn = self._make_button(
            btn_frame, "Cancel batch", self._cancel_batch,
            BG_BUTTON_CANCEL, "#181825", BG_BUTTON_CANCEL_HOVER, pady=5,
        )

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
        """Convenience: clicking an EMPTY url box auto-fills it from the
        clipboard. Never clobbers existing content or re-grabs the same
        link — refocusing after popups used to restart the fetch and wipe
        user edits (e.g. a hand-picked iTunes match)."""
        if self._queue_running:
            return
        try:
            clip = self.clipboard_get().strip()
        except Exception:
            return
        if not clip or not self._is_youtube_url(clip):
            return
        if self._url_entry.get().strip():
            return
        if clip == getattr(self, "_last_clip_grab", ""):
            return
        self._last_clip_grab = clip
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
            self._status_bar.append("Please wait, checking dependencies...", "warning")
            return
        # Re-entry guard: a paste can fire multiple triggers at once
        if self._ui_state in ("fetching", "downloading"):
            return

        # Playlist links: ask (popup), turbo the whole list, or take just
        # the pasted video — depending on settings. Notices are appended
        # after the log clear below so they stay visible.
        playlist_notice = None
        if self._is_playlist_url(url) and not self._queue_running:
            single = normalize_youtube_url(url)
            has_single = "watch?v=" in single and "list=" not in single
            if self.config.ignore_mixes and self._is_mix_url(url):
                if has_single:
                    playlist_notice = ("YouTube Mix ignored; downloading "
                                       "the video you pasted.")
                    url = single
                else:
                    self._status_bar.append(
                        "This is an auto-generated Mix link with no single "
                        "video in it. Nothing to download.", "warning")
                    return
            elif not self.config.ask_playlist:
                if has_single:
                    playlist_notice = ("Playlist ignored (asking is off in "
                                       "settings); downloading the video "
                                       "you pasted.")
                    url = single
                else:
                    self._status_bar.append(
                        "This playlist link has no single video. Enable "
                        "playlist asking in settings to pick from it.", "warning")
                    return
            elif self._turbo_var.get():
                self._turbo_playlist(url)
                return
            else:
                self._open_playlist_picker(url)
                return

        normalized = normalize_youtube_url(url)
        if normalized != url:
            self._url_entry.delete(0, tk.END)
            self._url_entry.insert(0, normalized)
            self._status_bar.append("Playlist parameters stripped; downloading single video only.", "dim")
        self._current_url = normalized

        # A previous Cancel must not poison this fresh fetch
        self.pipeline.reset_cancel()
        self._show_skip(False)
        self._set_ui_state("fetching")
        if not self._queue_running:
            self._status_bar.clear()
        if playlist_notice:
            self._status_bar.append(playlist_notice, "dim")
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
            if self._queue_running:
                if self._queue:
                    self.after(600, self._next_in_queue)
                else:
                    self._finish_queue(
                        "Queue finished (last video failed).", "warning")

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

                # Batch album memory: if the user picked an album via
                # "Wrong match?", prefer candidates from that album. Songs
                # that aren't on it fall back to the normal best match.
                hint = self._queue_album_hint if self._queue_running else None
                if hint:
                    from itunes import search_itunes_candidates
                    cands = search_itunes_candidates(yt_artist, yt_title)
                    same_album = [c for c in cands
                                  if c.album.lower() == hint[1]]
                    if same_album:
                        same_album.sort(
                            key=lambda c: (hint[0] not in c.artist.lower(),
                                           -c.score))
                        itunes_match = same_album[0]
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
        self._update_dup_badge(self._current_url)
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

        if self._queue_running:
            # Per-batch source override from the playlist popup
            if self._queue_source in ("itunes", "youtube"):
                if self._queue_source == "itunes" and self._itunes_meta:
                    self._meta_source_var.set("itunes")
                elif self._queue_source == "youtube":
                    self._meta_source_var.set("youtube")
                self._apply_meta_source()
                if self._queue_source == "itunes" and self._itunes_cover_bytes:
                    self._select_cover("itunes")
                elif self._queue_source == "youtube" and self._yt_thumb_bytes:
                    self._select_cover("youtube")
            if self._queue_album_hint and itunes_meta and \
                    itunes_meta.get("album", "").lower() == self._queue_album_hint[1]:
                self._status_bar.append(
                    f"Matched to your chosen album: {itunes_meta['album']}",
                    "dim")
            if self._queue_review:
                self._show_skip(True)
                self._status_bar.append(
                    "Review: adjust anything, then hit Download (or Skip).",
                    "info")
            else:
                self.after(50, self._on_download)
        elif self._turbo_var.get():
            self.after(50, self._on_download)

    def _on_fetch_error(self, error_msg):
        self._status_bar.append(f"Error: {error_msg}", "error")
        self._set_ui_state("idle")
        if self._queue_running:
            if self._queue:
                self._status_bar.append("Skipping to the next video in the queue...", "warning")
                self.after(600, self._next_in_queue)
            else:
                self._finish_queue("Queue finished (last video failed).", "warning")

    def _on_turbo_change(self):
        self.config.turbo_mode = self._turbo_var.get()
        self.config.save()

    def _on_ask_playlist_change(self):
        self.config.ask_playlist = self._ask_playlist_var.get()
        self.config.save()

    def _on_ignore_mixes_change(self):
        self.config.ignore_mixes = self._ignore_mixes_var.get()
        self.config.save()

    def report_callback_exception(self, exc, val, tb):
        """Log unexpected UI errors to crash.log instead of losing them."""
        import traceback
        from datetime import datetime
        try:
            with open(os.path.join(app_dir(), "crash.log"), "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] UI error:\n")
                traceback.print_exception(exc, val, tb, file=f)
        except OSError:
            pass
        try:
            self._status_bar.append(
                f"Unexpected error: {val} (details saved to crash.log)", "error")
        except Exception:
            pass

    # ---- Shared popup scaffolding ----

    def _make_popup(self, title: str, body_bg=BG_MAIN):
        """Themed transient window with our own title bar (native caption
        stripped): app-styled title, drag-to-move, custom close button."""
        win = tk.Toplevel(self)
        win.title(title)  # still shown in alt-tab
        win.configure(bg=body_bg)
        win.resizable(False, False)
        win.transient(self)
        win.geometry(f"+{self.winfo_rootx() + 60}+{self.winfo_rooty() + 90}")

        header = tk.Frame(win, bg=body_bg)
        header.pack(fill="x")
        title_lbl = tk.Label(header, text=title, font=FONT_LABEL_BOLD,
                             bg=body_bg, fg=FG_LABEL)
        title_lbl.pack(side="left", padx=14, pady=(8, 2))

        close = tk.Label(header, text=chr(0xE8BB),
                         font=("Segoe MDL2 Assets", 9),
                         bg=body_bg, fg=FG_LABEL, width=4, pady=5,
                         cursor="hand2")
        close.pack(side="right", padx=(0, 4))
        close.bind("<Enter>",
                   lambda e: close.config(bg=BG_BUTTON_CANCEL, fg="#181825"))
        close.bind("<Leave>", lambda e: close.config(bg=body_bg, fg=FG_LABEL))
        close.bind("<Button-1>", lambda e: win.destroy())

        # Drag-to-move via the native move loop (same pattern as the main
        # title bar: threshold first, then hand off via PostMessage)
        press = {"pos": None}

        def on_press(event):
            press["pos"] = (event.x_root, event.y_root)

        def on_motion(event):
            if press["pos"] is None:
                return
            if (abs(event.x_root - press["pos"][0])
                    + abs(event.y_root - press["pos"][1])) < 4:
                return
            press["pos"] = None
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
                ctypes.windll.user32.ReleaseCapture()
                ctypes.windll.user32.PostMessageW(hwnd, 0xA1, 2, 0)
            except Exception:
                pass

        for w in (header, title_lbl):
            w.bind("<Button-1>", on_press)
            w.bind("<B1-Motion>", on_motion)
            w.bind("<ButtonRelease-1>", lambda e: press.update(pos=None))

        # Tk re-applies its own styles when the window maps, so strip the
        # caption right after mapping (plus a late fallback pass).
        win.bind("<Map>",
                 lambda e: win.after(10, lambda: self._strip_native_titlebar(win)),
                 add="+")
        win.after(150, lambda: self._strip_native_titlebar(win))

        body = tk.Frame(win, bg=body_bg, padx=14, pady=12)
        body.pack(fill="both", expand=True)
        return win, body

    def _scroll_area(self, parent, height=320, width=540):
        """Scrollable themed list area; returns (wrapper, inner frame)."""
        wrap = tk.Frame(parent, bg=BG_MAIN)
        canvas = tk.Canvas(wrap, bg=BG_MAIN, highlightthickness=0,
                           height=height, width=width)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview,
                            style="Custom.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=BG_MAIN)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(item, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        # Bind on the popup toplevel (every child's bindtags include it) —
        # a global bind_all would let popups steal each other's wheel and
        # break the main window's handler on out-of-order closes
        wrap.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        return wrap, inner

    def _reveal_file(self, path: str):
        """Show a file in Explorer. Files dropped into 'Automatically Add
        to iTunes' get moved into the library tree — hunt them down there."""
        if not path:
            return
        path = os.path.normpath(path)
        if os.path.isfile(path):
            subprocess.run(["explorer", "/select," + path])
            return
        folder = os.path.dirname(path)
        if "automatically add to itunes" in folder.lower():
            self._locate_in_itunes_library(path)
        elif os.path.isdir(folder):
            os.startfile(folder)

    def _locate_in_itunes_library(self, orig_path: str):
        """iTunes moves auto-added files to Music/<Artist>/<Album>/ and
        usually renames them to just the title — search the library for
        the real location and reveal it."""
        self._status_bar.append(
            "iTunes moved this file into the library. Searching...", "dim")
        fname = os.path.basename(orig_path)
        stem, ext = os.path.splitext(fname)
        title_part = stem.split(" - ", 1)[1].strip() if " - " in stem else stem
        media_root = os.path.dirname(os.path.dirname(orig_path))

        def worker():
            target_full = fname.lower()
            target_title = (title_part + ext).lower()
            found = fallback = None
            scanned = 0
            roots = [os.path.join(media_root, "Music"), media_root]
            for root in roots:
                if not os.path.isdir(root) or found:
                    continue
                for dirpath, _dirs, filenames in os.walk(root):
                    for fn in filenames:
                        scanned += 1
                        low = fn.lower()
                        if low == target_full:
                            found = os.path.join(dirpath, fn)
                            break
                        if fallback is None and (
                                low == target_title
                                or low.endswith(" " + target_title)):
                            # iTunes rename: "Title.ext" or "1-01 Title.ext"
                            fallback = os.path.join(dirpath, fn)
                    if found or scanned > 200000:
                        break

            hit = found or fallback

            def done():
                if hit:
                    self._status_bar.append(
                        f"Found it: {os.path.basename(hit)}", "dim")
                    subprocess.run(["explorer",
                                    "/select," + os.path.normpath(hit)])
                else:
                    self._status_bar.append(
                        "Could not find it in the iTunes library (it may "
                        "have been renamed). Opening the library folder.",
                        "warning")
                    music = os.path.join(media_root, "Music")
                    target = music if os.path.isdir(music) else media_root
                    if os.path.isdir(target):
                        os.startfile(target)

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ---- YouTube search popup ----

    def _open_search(self):
        if getattr(self, "_search_win", None) and self._search_win.winfo_exists():
            self._search_win.lift()
            return
        win, body = self._make_popup("Search YouTube")
        self._search_win = win

        row = tk.Frame(body, bg=BG_MAIN)
        row.pack(fill="x")
        field = RoundField(row, height=32, width=44)
        field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        field.config(width=400)

        # Length filter — applied by YouTube itself on the next Search
        dur_var = tk.StringVar(value="any")
        len_row = tk.Frame(body, bg=BG_MAIN)
        len_row.pack(fill="x", pady=(6, 0))
        tk.Label(len_row, text="Length:", font=FONT_SMALL, bg=BG_MAIN,
                 fg=FG_DIM).pack(side="left", padx=(0, 6))
        for text, value in (("Any", "any"), ("Under 4 min", "short"),
                            ("4-20 min", "medium"), ("Over 20 min", "long")):
            tk.Radiobutton(
                len_row, text=text, variable=dur_var, value=value,
                font=FONT_SMALL, bg=BG_MAIN, fg=FG_TEXT,
                selectcolor=BG_INPUT, activebackground=BG_MAIN,
                activeforeground=FG_TEXT, highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 8))

        # Live filter over the returned results
        filt_row = tk.Frame(body, bg=BG_MAIN)
        filt_row.pack(fill="x", pady=(4, 0))
        tk.Label(filt_row, text="Filter:", font=FONT_SMALL, bg=BG_MAIN,
                 fg=FG_DIM).pack(side="left", padx=(0, 6))
        filt_field = RoundField(filt_row, height=26)
        filt_field.pack(side="left", fill="x", expand=True)

        info = tk.Label(body, text="Type a song or artist and hit Enter.",
                        font=FONT_SMALL, bg=BG_MAIN, fg=FG_DIM, anchor="w")
        info.pack(fill="x", pady=(8, 2))
        results_wrap, results = self._scroll_area(body, height=360)
        results_wrap.pack(fill="both", expand=True)

        def set_row_bg(frame, color):
            frame.config(bg=color)
            for child in frame.winfo_children():
                child.config(bg=color)

        def fold(s):
            import unicodedata
            return "".join(ch for ch in unicodedata.normalize("NFKD", s or "")
                           if not unicodedata.combining(ch)).lower()

        def pick(url):
            if self._ui_state in ("fetching", "downloading"):
                info.config(text="Busy with another download. Try again in a moment.",
                            fg=FG_WARN)
                return
            win.destroy()
            self._url_entry.config(state="normal")
            self._url_entry.delete(0, tk.END)
            self._url_entry.insert(0, url)
            self._on_fetch_info()

        all_rows = []
        thumb_labels = []          # (label, video_id) still needing an image
        thumb_bytes = {}           # video_id -> jpg bytes (survives filtering)
        thumb_photos = {}          # video_id -> PhotoImage (prevents GC)
        loader_gen = [0]           # bumping this retires stale loader threads

        def set_thumb(label, vid):
            data = thumb_bytes.get(vid)
            if not data:
                return False
            try:
                photo = thumb_photos.get(vid)
                if photo is None:
                    import io
                    img = Image.open(io.BytesIO(data)).resize(
                        (64, 36), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    thumb_photos[vid] = photo
                label.config(image=photo, width=64, height=36)
                return True
            except Exception:
                return False

        def load_thumbs():
            # Fetch missing thumbnails in the background with a small pool —
            # rows appear instantly, pictures stream in
            gen = loader_gen[0]
            import queue
            jobs = queue.Queue()
            for item in thumb_labels:
                jobs.put(item)

            def fetch_worker():
                while gen == loader_gen[0] and win.winfo_exists():
                    try:
                        lbl, vid = jobs.get_nowait()
                    except queue.Empty:
                        return
                    if vid not in thumb_bytes:
                        data = self.pipeline.download_cover_bytes(
                            f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
                        if not data:
                            continue
                        thumb_bytes[vid] = data
                    self.after(0, lambda l=lbl, v=vid: (
                        win.winfo_exists() and set_thumb(l, v)))

            for _ in range(6):
                threading.Thread(target=fetch_worker, daemon=True).start()

        # Hover zoom on thumbnails — same feel as the artwork tiles
        zoom = {"win": None, "job": None, "photo": None}

        def hide_zoom(event=None):
            if zoom["job"]:
                try:
                    self.after_cancel(zoom["job"])
                except Exception:
                    pass
                zoom["job"] = None
            if zoom["win"] is not None:
                try:
                    zoom["win"].destroy()
                except Exception:
                    pass
                zoom["win"] = None
                zoom["photo"] = None

        def show_zoom(label, vid):
            zoom["job"] = None
            if zoom["win"] is not None or not win.winfo_exists():
                return
            if not label.winfo_exists():
                return
            data = thumb_bytes.get(vid)
            if not data:
                return
            import io
            from PIL import ImageDraw
            try:
                img = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                return
            w_z = 380
            h_z = max(1, int(w_z * img.height / img.width))
            img = img.resize((w_z, h_z), Image.LANCZOS)
            ss, radius = 4, 14
            mask = Image.new("L", (w_z * ss, h_z * ss), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, w_z * ss - 1, h_z * ss - 1), radius=radius * ss,
                fill=255)
            img.putalpha(mask.resize((w_z, h_z), Image.LANCZOS))
            ring = Image.new("RGBA", (w_z * ss, h_z * ss), (0, 0, 0, 0))
            ImageDraw.Draw(ring).rounded_rectangle(
                (ss, ss, w_z * ss - 1 - ss, h_z * ss - 1 - ss),
                radius=radius * ss, outline=BORDER_COLOR, width=2 * ss)
            img.alpha_composite(ring.resize((w_z, h_z), Image.LANCZOS))
            zoom["photo"] = ImageTk.PhotoImage(img)

            tw = tk.Toplevel(win)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            key = "#010203"
            tw.configure(bg=key)
            try:
                tw.attributes("-transparentcolor", key)
            except tk.TclError:
                pass
            tk.Label(tw, image=zoom["photo"], bg=key, bd=0).pack()

            left, top, right, bottom = monitor_bounds(label)
            lx, ly = label.winfo_rootx(), label.winfo_rooty()
            lh = label.winfo_height()
            x = lx + label.winfo_width() // 2 - w_z // 2
            x = max(left + 8, min(x, right - w_z - 8))
            if bottom - (ly + lh) - 16 >= h_z:
                y = ly + lh + 10
            elif ly - top - 16 >= h_z:
                y = ly - h_z - 10
            else:
                x = min(lx + label.winfo_width() + 12, right - w_z - 8)
                y = max(top + 8, min(ly, bottom - h_z - 8))
            tw.wm_geometry(f"+{x}+{y}")
            zoom["win"] = tw

        def schedule_zoom(label, vid):
            if zoom["win"] is not None:
                return
            if zoom["job"]:
                try:
                    self.after_cancel(zoom["job"])
                except Exception:
                    pass
            zoom["job"] = self.after(350, lambda: show_zoom(label, vid))

        def render(rows):
            loader_gen[0] += 1
            hide_zoom()
            for child in results.winfo_children():
                child.destroy()
            thumb_labels.clear()
            for r in rows:
                rowf = tk.Frame(results, bg=BG_SECTION, padx=8, pady=5,
                                highlightbackground=BORDER_COLOR,
                                highlightthickness=1, cursor="hand2")
                rowf.pack(fill="x", pady=2)
                thumb = tk.Label(rowf, bg=BG_INPUT, width=9, height=2)
                thumb.pack(side="left", padx=(0, 10))
                if not set_thumb(thumb, r["id"]):
                    thumb_labels.append((thumb, r["id"]))
                text = tk.Frame(rowf, bg=BG_SECTION)
                text.pack(side="left", fill="x", expand=True)
                tk.Label(text, text=r["title"][:70], font=FONT_LABEL,
                         bg=BG_SECTION, fg=FG_TEXT, anchor="w").pack(fill="x")
                sub = "  ·  ".join(x for x in (r["uploader"], r["duration"],
                                               r.get("views", "")) if x)
                tk.Label(text, text=sub, font=FONT_SMALL,
                         bg=BG_SECTION, fg=FG_DIM, anchor="w").pack(fill="x")
                for w in (rowf, thumb, text, *text.winfo_children()):
                    w.bind("<Button-1>", lambda e, u=r["url"]: pick(u))
                    w.bind("<Enter>", lambda e, f=rowf: set_row_bg(f, BG_INPUT))
                    w.bind("<Leave>", lambda e, f=rowf: set_row_bg(f, BG_SECTION))
                # thumbnail hover shows the enlarged preview
                thumb.bind("<Enter>",
                           lambda e, l=thumb, v=r["id"]: schedule_zoom(l, v),
                           add="+")
                thumb.bind("<Leave>", hide_zoom, add="+")
                thumb.bind("<Button-1>", hide_zoom, add="+")
                # open the video in the default browser (does not fetch)
                link = tk.Label(text, text="Open in browser", font=FONT_SMALL,
                                bg=BG_SECTION, fg=FG_ACCENT, cursor="hand2",
                                anchor="w")
                link.pack(fill="x")

                def open_browser(event, u=r["url"]):
                    import webbrowser
                    webbrowser.open(u)
                    return "break"

                link.bind("<Button-1>", open_browser)
                link.bind("<Enter>", lambda e, f=rowf, l=link:
                          (set_row_bg(f, BG_INPUT), l.config(fg=FG_TEXT)))
                link.bind("<Leave>", lambda e, f=rowf, l=link:
                          (set_row_bg(f, BG_SECTION), l.config(fg=FG_ACCENT)))
            if thumb_labels:
                load_thumbs()

        def apply_filter(*_):
            q = fold(filt_field.entry.get().strip())
            subset = [r for r in all_rows
                      if not q or q in fold(r["title"] + " " + r["uploader"])]
            render(subset)
            if all_rows:
                if len(subset) != len(all_rows):
                    info.config(text=f"{len(subset)} of {len(all_rows)} "
                                     "match your filter:", fg=FG_DIM)
                else:
                    info.config(text=f"{len(all_rows)} results. "
                                     "Click one to fetch it:", fg=FG_DIM)

        filt_field.entry.bind("<KeyRelease>", apply_filter)

        def populate(rows):
            if not win.winfo_exists():
                return
            go.config(state="normal")
            all_rows[:] = rows
            if not rows:
                info.config(text="No results.", fg=FG_DIM)
                render([])
                return
            apply_filter()

        def fail(msg):
            if not win.winfo_exists():
                return
            go.config(state="normal")
            info.config(text=f"Search failed: {msg}", fg=FG_ERROR)

        def start():
            query = field.entry.get().strip()
            if not query:
                return
            go.config(state="disabled")
            info.config(text="Searching...", fg=FG_DIM)
            for child in results.winfo_children():
                child.destroy()
            duration = None if dur_var.get() == "any" else dur_var.get()

            def worker():
                try:
                    from downloader import search_youtube
                    rows = search_youtube(query, limit=100, duration=duration)
                    self.after(0, populate, rows)
                except Exception as e:
                    self.after(0, fail, str(e))

            threading.Thread(target=worker, daemon=True).start()

        go = self._make_button(row, "Search", start, BG_BUTTON_ACCENT,
                               FG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER)
        go.pack(side="right")
        field.entry.bind("<Return>", lambda e: start())
        field.entry.focus_set()

    # ---- iTunes match picker ----

    def _open_match_picker(self):
        if not self._yt_meta:
            return
        if getattr(self, "_match_win", None) and self._match_win.winfo_exists():
            self._match_win.lift()
            return
        win, body = self._make_popup("Choose iTunes match")
        self._match_win = win

        # Editable query — when the auto-search misses, refine it yourself
        search_row = tk.Frame(body, bg=BG_MAIN)
        search_row.pack(fill="x", pady=(0, 6))
        query_field = RoundField(search_row, height=30)
        query_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        query_field.config(width=330)
        query_field.entry.insert(
            0, f"{self._yt_meta.get('artist', '')} "
               f"{self._yt_meta.get('title', '')}".strip())

        # Live filters — narrow whatever the search returned
        filters_row = tk.Frame(body, bg=BG_MAIN)
        filters_row.pack(fill="x", pady=(0, 4))
        filter_entries = {}
        for name, expand in (("Artist", True), ("Album", True), ("Year", False)):
            cell = tk.Frame(filters_row, bg=BG_MAIN)
            cell.pack(side="left", fill="x", expand=expand,
                      padx=(0, 6) if expand else 0)
            tk.Label(cell, text=name, font=FONT_SMALL, bg=BG_MAIN,
                     fg=FG_DIM, anchor="w").pack(fill="x")
            field = RoundField(cell, height=26)
            if not expand:
                field.config(width=70)
            field.pack(fill="x")
            filter_entries[name.lower()] = field.entry

        info = tk.Label(body, text="Searching iTunes...", font=FONT_SMALL,
                        bg=BG_MAIN, fg=FG_DIM, anchor="w")
        info.pack(fill="x", pady=(0, 4))
        results_wrap, results = self._scroll_area(body, height=330)
        results_wrap.pack(fill="both", expand=True)

        def set_row_bg(frame, color):
            frame.config(bg=color)
            for child in frame.winfo_children():
                child.config(bg=color)

        all_cands = []
        art_labels = []
        art_cache = {}

        def render(cands):
            for child in results.winfo_children():
                child.destroy()
            art_labels.clear()
            for m in cands:
                rowf = tk.Frame(results, bg=BG_SECTION, padx=8, pady=6,
                                highlightbackground=BORDER_COLOR,
                                highlightthickness=1, cursor="hand2")
                rowf.pack(fill="x", pady=2)
                art = tk.Label(rowf, bg=BG_INPUT, width=6, height=3)
                art.pack(side="left", padx=(0, 10))
                art_labels.append((art, m.artwork_url))
                text = tk.Frame(rowf, bg=BG_SECTION)
                text.pack(side="left", fill="x", expand=True)
                tk.Label(text, text=f"{m.artist} - {m.song}"[:70],
                         font=FONT_LABEL, bg=BG_SECTION, fg=FG_TEXT,
                         anchor="w").pack(fill="x")
                sub = "  ·  ".join(x for x in (m.album, m.year, m.genre) if x)
                tk.Label(text, text=sub[:80], font=FONT_SMALL, bg=BG_SECTION,
                         fg=FG_DIM, anchor="w").pack(fill="x")
                widgets = [rowf, art, text, *text.winfo_children()]
                for w in widgets:
                    w.bind("<Button-1>",
                           lambda e, mm=m: (win.destroy(),
                                            self._apply_itunes_choice(mm)))
                    w.bind("<Enter>", lambda e, f=rowf: set_row_bg(f, BG_INPUT))
                    w.bind("<Leave>", lambda e, f=rowf: set_row_bg(f, BG_SECTION))
            threading.Thread(target=load_art, daemon=True).start()

        def fold(s):
            # Diacritic-insensitive compare: Apple styles names like
            # JAŸ-Z and Beyoncé, which plain .lower() can't match
            import unicodedata
            return "".join(ch for ch in unicodedata.normalize("NFKD", s or "")
                           if not unicodedata.combining(ch)).lower()

        def apply_filters(*_):
            fa = fold(filter_entries["artist"].get().strip())
            fb = fold(filter_entries["album"].get().strip())
            fy = filter_entries["year"].get().strip()
            subset = [c for c in all_cands
                      if (not fa or fa in fold(c.artist))
                      and (not fb or fb in fold(c.album))
                      and (not fy or fy in (c.year or ""))]
            render(subset)
            if all_cands:
                if len(subset) != len(all_cands):
                    info.config(text=f"{len(subset)} of {len(all_cands)} "
                                     "match your filters:", fg=FG_DIM)
                else:
                    info.config(text="Click the correct song:", fg=FG_DIM)

        for entry in filter_entries.values():
            entry.bind("<KeyRelease>", apply_filters)

        def populate(cands):
            if not win.winfo_exists():
                return
            go.config(state="normal")
            all_cands[:] = cands
            if not cands:
                info.config(text="No iTunes matches found. Try different "
                                 "search words above.", fg=FG_WARN)
                render([])
                return
            apply_filters()

        def load_art():
            import io
            for label, url in list(art_labels):
                if not url:
                    continue
                data = art_cache.get(url)
                if data is None:
                    data = self.pipeline.download_cover_bytes(
                        url.replace("600x600bb", "100x100bb"))
                    if not data:
                        continue
                    art_cache[url] = data

                def show(lbl=label, raw=data):
                    if not win.winfo_exists():
                        return
                    try:
                        img = Image.open(io.BytesIO(raw)).resize((44, 44),
                                                                 Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                        lbl._photo = photo  # prevent GC
                        lbl.config(image=photo, width=44, height=44)
                    except Exception:
                        pass

                self.after(0, show)

        def run_search(term=None, f_artist="", f_album=""):
            go.config(state="disabled")
            info.config(text="Searching iTunes...", fg=FG_DIM)
            for child in results.winfo_children():
                child.destroy()
            art_labels.clear()

            def worker():
                try:
                    from itunes import (search_itunes_candidates,
                                        search_itunes_album_tracks,
                                        lookup_apple_music)
                    artist = self._yt_meta.get("artist", "")
                    title = self._yt_meta.get("title", "")
                    if term and "music.apple.com" in term:
                        # Pasted Apple Music link: resolve it exactly
                        cands = lookup_apple_music(term, artist, title)
                    elif term:
                        # User-driven search: rank by Apple's relevance for
                        # THEIR words — don't drag results back toward the
                        # original video's artist/title. An Album filter
                        # additionally pulls that album's full track list.
                        cands = []
                        if f_album:
                            cands = search_itunes_album_tracks(f_album,
                                                               f_artist)
                        extra = search_itunes_candidates("", "", limit=10,
                                                         term=term)
                        have = {(c.song.lower(), c.artist.lower(),
                                 c.album.lower()) for c in cands}
                        cands += [c for c in extra
                                  if (c.song.lower(), c.artist.lower(),
                                      c.album.lower()) not in have]
                    else:
                        cands = search_itunes_candidates(artist, title,
                                                         limit=10)
                    self.after(0, populate, cands)
                except Exception as e:
                    self.after(0, lambda: (
                        go.config(state="normal"),
                        info.config(text=f"Search failed: {e}", fg=FG_ERROR)))

            threading.Thread(target=worker, daemon=True).start()

        def search_edited():
            # The filter fields join the search itself, so "blueprint" +
            # Artist "Jay-Z" actually searches for Jay-Z's Blueprint
            query = query_field.entry.get().strip()
            f_artist = filter_entries["artist"].get().strip()
            f_album = filter_entries["album"].get().strip()
            f_year = filter_entries["year"].get().strip()
            if "music.apple.com" in query:
                run_search(query)
                return
            combined = " ".join(p for p in (query, f_artist, f_album, f_year)
                                if p)
            if combined:
                run_search(combined, f_artist, f_album)

        go = self._make_button(search_row, "Search", search_edited,
                               BG_BUTTON_ACCENT, FG_BUTTON_ACCENT,
                               BG_BUTTON_ACCENT_HOVER)
        go.pack(side="right")
        query_field.entry.bind("<Return>", lambda e: search_edited())

        run_search()

    def _apply_itunes_choice(self, match):
        """User picked a different iTunes match; refresh metadata and art."""
        self._status_bar.append(
            f"Using iTunes match: {match.artist} - {match.song}", "info")
        snapshot_url = self._current_url

        def worker():
            cover = None
            if match.artwork_url:
                cover = self.pipeline.download_cover_bytes(match.artwork_url)
            self.after(0, apply, cover)

        def apply(cover):
            if snapshot_url != self._current_url:
                return  # a newer fetch replaced this song meanwhile
            yt = self._yt_meta or {}
            self._itunes_match = match
            self._itunes_meta = {
                "title": match.song or yt.get("title", ""),
                "artist": match.artist or yt.get("artist", ""),
                "album": match.album or yt.get("album", ""),
                "year": match.year or yt.get("year", ""),
                "genre": match.genre or yt.get("genre", ""),
            }
            if cover:
                self._itunes_cover_bytes = cover
            self._meta_source_var.set("itunes")
            self._apply_meta_source()
            self._update_cover_previews()
            # In a batch, remember the picked album — later songs prefer
            # iTunes matches from it when they have one
            if self._queue_running and match.album:
                self._queue_album_hint = (match.artist.lower(),
                                          match.album.lower())
                self._status_bar.append(
                    f"Album remembered for this batch: {match.album}", "dim")

        threading.Thread(target=worker, daemon=True).start()

    def _make_alert_photo(self, size: int = 16):
        """Draw a small amber exclamation badge (anti-aliased via PIL)."""
        if not HAS_PIL:
            return None
        try:
            from PIL import ImageDraw
            ss = 4
            big = size * ss
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((0, 0, big - 1, big - 1), fill="#fab387")
            cx, w = big // 2, max(big // 11, 2)
            draw.rounded_rectangle(
                (cx - w, int(big * 0.20), cx + w, int(big * 0.58)),
                radius=w, fill="#181825")
            draw.ellipse((cx - w, int(big * 0.68), cx + w, int(big * 0.68) + 2 * w),
                         fill="#181825")
            return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))
        except Exception:
            return None

    @staticmethod
    def _video_id(url: str) -> str:
        """Extract the video id from any YouTube URL shape, so the same
        video matches whether it came in bare, in a playlist, as a
        youtu.be link, or as a short."""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url or "")
            v = parse_qs(parsed.query).get("v", [None])[0]
            if v:
                return v
            host = (parsed.hostname or "").lower()
            if "youtu.be" in host:
                return parsed.path.strip("/").split("/")[0]
            if parsed.path.startswith("/shorts/"):
                return parsed.path.split("/")[2]
        except Exception:
            pass
        return url or ""

    def _update_dup_badge(self, url: str):
        """Show the already-downloaded badge when this video is in history."""
        uid = self._video_id(url)
        hits = [e for e in self._load_history()
                if uid and self._video_id(e.get("url", "")) == uid]
        if hits:
            last = hits[0]
            text = f"You've downloaded this one before.\nLast on {last.get('ts', '?')}"
            if last.get("file"):
                text += f"\nSaved as {last['file']}"
            if len(hits) > 1:
                text += f"\nDownloaded {len(hits)} times total."
            self._dup_tip_text = text
            if self._dup_photo:
                self._dup_badge.pack(side="left", padx=(8, 0))
        else:
            self._dup_tip_text = ""
            self._dup_badge.pack_forget()

    # ---- Download history ----

    HISTORY_MAX = 100

    def _history_path(self):
        return os.path.join(app_dir(), "history.json")

    def _load_history(self) -> list:
        import json
        try:
            with open(self._history_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _append_history(self, filename: str, path: str, url: str = ""):
        import json
        from datetime import datetime
        entries = self._load_history()
        entries.insert(0, {"file": filename, "path": path, "url": url,
                           "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
        try:
            with open(self._history_path(), "w", encoding="utf-8") as f:
                json.dump(entries[:self.HISTORY_MAX], f, ensure_ascii=False)
        except OSError:
            pass

    def _open_history(self):
        if getattr(self, "_history_win", None) and self._history_win.winfo_exists():
            self._history_win.lift()
            return
        win, body = self._make_popup("Download History")
        self._history_win = win

        entries = self._load_history()
        if not entries:
            tk.Label(body, text="Nothing downloaded yet.", font=FONT_LABEL,
                     bg=BG_MAIN, fg=FG_DIM).pack(pady=20, padx=40)
            return

        wrap, inner = self._scroll_area(
            body, height=min(360, 34 * len(entries) + 10))
        wrap.pack(fill="both", expand=True)

        for e in entries:
            rowf = tk.Frame(inner, bg=BG_SECTION, padx=10, pady=5)
            rowf.pack(fill="x", pady=1)
            tk.Label(rowf, text=e.get("ts", ""), font=FONT_SMALL,
                     bg=BG_SECTION, fg=FG_DIM).pack(side="right")
            tk.Label(rowf, text=e.get("file", "?")[:64], font=FONT_SMALL,
                     bg=BG_SECTION, fg=FG_TEXT, anchor="w").pack(
                side="left", fill="x", expand=True)

    # ---- Playlist picker + queue ----

    @staticmethod
    def _is_playlist_url(url: str) -> bool:
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            if "list" in parse_qs(parsed.query):
                return True
            return parsed.path.startswith("/playlist")
        except Exception:
            return False

    @staticmethod
    def _is_mix_url(url: str) -> bool:
        """Auto-generated YouTube Mix/radio lists have RD-prefixed ids.
        Real playlists (PL...) and albums (OLAK...) don't match."""
        try:
            from urllib.parse import urlparse, parse_qs
            list_id = parse_qs(urlparse(url).query).get("list", [""])[0]
            return list_id.startswith("RD")
        except Exception:
            return False

    def _fetch_single(self, url: str):
        self._url_entry.config(state="normal")
        self._url_entry.delete(0, tk.END)
        self._url_entry.insert(0, url)
        self._on_fetch_info()

    def _turbo_playlist(self, url: str):
        """Turbo + playlist link + asking enabled: queue the whole playlist."""
        self._set_ui_state("fetching")
        self._status_bar.append("Turbo: reading playlist...", "info")

        def fail(msg):
            self._set_ui_state("idle")
            single = normalize_youtube_url(url)
            if "watch?v=" in single:
                self._status_bar.append(
                    f"Could not read playlist ({msg}). Downloading the video only.",
                    "warning")
                self._fetch_single(single)
            else:
                self._status_bar.append(f"Could not read playlist ({msg}).", "error")

        def go(title, rows):
            self._set_ui_state("idle")
            if not rows:
                fail("no entries")
                return
            self._status_bar.append(
                f"Turbo is ON: downloading the ENTIRE playlist ({len(rows)} videos). "
                "Cancel to stop, or turn Turbo off if you only wanted one.",
                "warning")
            self._start_queue([r["url"] for r in rows])

        def worker():
            try:
                from downloader import list_playlist
                title, rows = list_playlist(url)
                self.after(0, go, title, rows)
            except Exception as e:
                self.after(0, fail, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _open_playlist_picker(self, url: str):
        self._set_ui_state("fetching")
        self._status_bar.append("Reading playlist...", "info")

        def fallback(msg):
            self._set_ui_state("idle")
            single = normalize_youtube_url(url)
            if "watch?v=" in single:
                self._status_bar.append(
                    f"Could not read playlist ({msg}). Fetching the video only.",
                    "warning")
                self._fetch_single(single)
            else:
                self._status_bar.append(
                    f"Could not read playlist ({msg}).", "error")

        def done(title, rows):
            self._set_ui_state("idle")
            if not rows:
                fallback("no entries")
                return
            if len(rows) == 1:
                self._fetch_single(rows[0]["url"])
                return
            self._show_playlist_popup(url, title, rows)

        def worker():
            try:
                from downloader import list_playlist
                title, rows = list_playlist(url)
                self.after(0, done, title, rows)
            except Exception as e:
                self.after(0, fallback, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _show_playlist_popup(self, source_url, playlist_title, rows):
        if getattr(self, "_playlist_win", None) and self._playlist_win.winfo_exists():
            self._playlist_win.lift()
            return
        win, body = self._make_popup("Playlist detected")
        self._playlist_win = win

        # The video actually pasted (watch?v=...&list=...), if any
        primary_url = None
        try:
            from urllib.parse import urlparse, parse_qs
            v = parse_qs(urlparse(source_url).query).get("v", [None])[0]
            if v:
                primary_url = f"https://www.youtube.com/watch?v={v}"
        except Exception:
            pass

        tk.Label(body, text=playlist_title[:70], font=FONT_LABEL_BOLD,
                 bg=BG_MAIN, fg=FG_TEXT, anchor="w").pack(fill="x")
        tk.Label(body, text=f"{len(rows)} videos. Pick what to download:",
                 font=FONT_SMALL, bg=BG_MAIN, fg=FG_DIM,
                 anchor="w").pack(fill="x", pady=(2, 0))

        # YouTube "Mix" radios are generated fresh per viewer and session,
        # so warn that this list can differ from the browser's
        try:
            from urllib.parse import urlparse, parse_qs
            list_id = parse_qs(urlparse(source_url).query).get("list", [""])[0]
        except Exception:
            list_id = ""
        if list_id.startswith("RD"):
            tk.Label(body,
                     text="Note: this is an auto-generated YouTube Mix. YouTube "
                          "builds it fresh\neach time, so it may not exactly match "
                          "the list in your browser.",
                     font=FONT_SMALL, bg=BG_MAIN, fg=FG_WARN, justify="left",
                     anchor="w").pack(fill="x", pady=(2, 0))

        # Make it obvious what "Just this video" will grab
        primary_row = next(
            (r for r in rows if primary_url and r["url"] == primary_url), None)
        target_row = primary_row or rows[0]
        prefix = "Your video: " if primary_row else "First video: "
        tk.Label(body, text=prefix + target_row["title"][:62],
                 font=FONT_SMALL, bg=BG_MAIN, fg=FG_ACCENT,
                 anchor="w").pack(fill="x", pady=(2, 6))

        # Quick filter: narrows the list as you type; selections persist
        filter_row = tk.Frame(body, bg=BG_MAIN)
        filter_row.pack(fill="x", pady=(0, 4))
        filt = RoundField(filter_row, height=30)
        filt.pack(side="left", fill="x", expand=True)
        clear_btn = tk.Label(filter_row, text="✕", font=FONT_SMALL, bg=BG_MAIN,
                             fg=FG_DIM, cursor="hand2", padx=8)
        clear_btn.pack(side="right")
        clear_btn.bind("<Enter>", lambda e: clear_btn.config(fg=FG_TEXT))
        clear_btn.bind("<Leave>", lambda e: clear_btn.config(fg=FG_DIM))

        wrap, inner = self._scroll_area(body, height=min(340, 30 * len(rows) + 10))
        wrap.pack(fill="both", expand=True)
        list_canvas = inner.master

        variables = []
        row_widgets = []

        def update_count(*_):
            n = sum(var.get() for var in variables)
            count_lbl.config(text=f"{n} of {len(rows)} selected")

        # Shift+click selects the whole range since the last clicked box
        anchor = {"i": 0}

        def on_row_click(i):
            anchor["i"] = i
            update_count()

        def on_shift_click(i):
            vis = visible_indices()
            if i not in vis:
                return "break"
            a = anchor["i"] if anchor["i"] in vis else i
            ai, bi = vis.index(a), vis.index(i)
            state = not variables[i].get()
            for j in vis[min(ai, bi):max(ai, bi) + 1]:
                variables[j].set(state)
            anchor["i"] = i
            update_count()
            return "break"

        for i, r in enumerate(rows):
            var = tk.BooleanVar(value=True)
            variables.append(var)
            is_primary = primary_url and r["url"] == primary_url
            label = f"{i + 1:>3}.  {r['title'][:58]}"
            if r["duration"]:
                label += f"  ({r['duration']})"
            if is_primary:
                label += "   (your link)"
            cb = tk.Checkbutton(
                inner, text=label, variable=var, font=FONT_SMALL,
                bg=BG_MAIN, fg=FG_ACCENT if is_primary else FG_TEXT,
                activebackground=BG_MAIN, activeforeground=FG_TEXT,
                selectcolor=BG_INPUT, highlightthickness=0, bd=0, anchor="w",
                command=lambda i=i: on_row_click(i),
            )
            cb.bind("<Shift-Button-1>", lambda e, i=i: on_shift_click(i))
            cb.pack(fill="x", padx=2)
            row_widgets.append((cb, f"{r['title']} {r['uploader']}".lower()))

        def visible_indices():
            query = filt.entry.get().strip().lower()
            return [i for i, (cb, text) in enumerate(row_widgets)
                    if not query or query in text]

        def refilter(*_):
            show = set(visible_indices())
            for i, (cb, text) in enumerate(row_widgets):
                cb.pack_forget()
            for i, (cb, text) in enumerate(row_widgets):
                if i in show:
                    cb.pack(fill="x", padx=2)
            list_canvas.yview_moveto(0)

        filt.entry.bind("<KeyRelease>", refilter)

        def clear_filter(event=None):
            filt.entry.delete(0, tk.END)
            refilter()

        clear_btn.bind("<Button-1>", clear_filter)

        toggles = tk.Frame(body, bg=BG_MAIN)
        toggles.pack(fill="x", pady=(6, 0))

        def set_all(value):
            # All/None act on the rows currently shown by the filter
            for i in visible_indices():
                variables[i].set(value)
            update_count()

        for text, val in (("All", True), ("None", False)):
            lbl = tk.Label(toggles, text=text, font=FONT_SMALL, bg=BG_MAIN,
                           fg=FG_ACCENT, cursor="hand2")
            lbl.pack(side="left", padx=(0, 12))
            lbl.bind("<Button-1>", lambda e, v=val: set_all(v))

        count_lbl = tk.Label(toggles, text="", font=FONT_SMALL,
                             bg=BG_MAIN, fg=FG_LABEL)
        count_lbl.pack(side="right")
        update_count()
        filt.entry.focus_set()

        # Per-batch options: how to run it, and which details to use.
        # The batch mode is remembered; the details override is per-batch.
        mode_var = tk.StringVar(
            value="review" if self.config.playlist_review else "auto")
        src_var = tk.StringVar(value="default")

        def save_mode():
            self.config.playlist_review = (mode_var.get() == "review")
            self.config.save()

        def opt_row(label, var, options, cmd=None):
            rowf = tk.Frame(body, bg=BG_MAIN)
            rowf.pack(fill="x", pady=(6, 0))
            tk.Label(rowf, text=label, font=FONT_SMALL, bg=BG_MAIN,
                     fg=FG_LABEL, width=7, anchor="w").pack(side="left")
            for text, value in options:
                tk.Radiobutton(
                    rowf, text=text, variable=var, value=value,
                    font=FONT_SMALL, bg=BG_MAIN, fg=FG_TEXT,
                    selectcolor=BG_INPUT, activebackground=BG_MAIN,
                    activeforeground=FG_TEXT, highlightthickness=0, bd=0,
                    command=cmd,
                ).pack(side="left", padx=(0, 10))

        opt_row("Batch:", mode_var,
                [("Download automatically", "auto"),
                 ("Review each one", "review")], cmd=save_mode)
        opt_row("Details:", src_var,
                [("My defaults", "default"), ("iTunes", "itunes"),
                 ("YouTube", "youtube")])

        btns = tk.Frame(body, bg=BG_MAIN)
        btns.pack(fill="x", pady=(10, 0))

        def download_selected():
            selected = [r["url"] for r, var in zip(rows, variables) if var.get()]
            review = mode_var.get() == "review"
            source = None if src_var.get() == "default" else src_var.get()
            win.destroy()
            if not selected:
                return
            if len(selected) == 1 and not review and source is None:
                self._fetch_single(selected[0])
            else:
                self._start_queue(selected, review=review, source=source)

        def just_one():
            win.destroy()
            self._fetch_single(primary_url or rows[0]["url"])

        self._make_button(
            btns, "Download selected", download_selected,
            BG_BUTTON_ACCENT, FG_BUTTON_ACCENT, BG_BUTTON_ACCENT_HOVER,
        ).pack(side="left")
        self._make_button(
            btns, "Just this video" if primary_url else "Just the first one",
            just_one, BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER,
        ).pack(side="left", padx=(8, 0))
        self._make_button(
            btns, "Cancel", win.destroy,
            BG_BUTTON, FG_TEXT, BG_BUTTON_HOVER,
        ).pack(side="right")

    def _start_queue(self, urls, review=False, source=None):
        self._queue = list(urls)
        self._queue_total = len(self._queue)
        self._queue_running = True
        self._queue_review = review
        self._queue_source = source
        self._queue_album_hint = None
        mode = "review each one" if review else "download automatically"
        self._status_bar.append(
            f"Queue started: {self._queue_total} videos ({mode}).", "info")
        self._next_in_queue()

    def _next_in_queue(self):
        if not self._queue_running or not self._queue:
            return
        if self._ui_state in ("fetching", "downloading"):
            # Busy (e.g. a manual fetch mid-review) — retry shortly instead
            # of popping the item into a fetch that would be rejected
            self.after(700, self._next_in_queue)
            return
        url = self._queue.pop(0)
        n = self._queue_total - len(self._queue)
        self._status_bar.append(f"Queue {n}/{self._queue_total}:", "info")
        self._fetch_single(url)

    def _finish_queue(self, message, tag):
        self._queue = []
        self._queue_running = False
        self._queue_review = False
        self._queue_source = None
        self._queue_album_hint = None
        self._queue_total = 0
        self._show_skip(False)
        self._status_bar.append(message, tag)

    def _show_skip(self, show: bool):
        if show and not self._skip_btn.winfo_ismapped():
            self._skip_btn.pack(side="left", padx=(8, 0),
                                before=self._history_btn)
            self._cancel_batch_btn.pack(side="left", padx=(8, 0),
                                        before=self._history_btn)
        elif not show and self._skip_btn.winfo_ismapped():
            self._skip_btn.pack_forget()
            self._cancel_batch_btn.pack_forget()

    def _cancel_batch(self):
        if self._queue_running:
            self._finish_queue("Batch cancelled.", "warning")

    def _skip_current(self):
        """Review mode: skip the current song without downloading it."""
        if not self._queue_running:
            return
        self._show_skip(False)
        self._status_bar.append("Skipped.", "dim")
        self._queue_advance_or_finish()

    def _queue_advance_or_finish(self):
        if self._queue:
            self.after(500, self._next_in_queue)
        else:
            self._finish_queue("Queue finished.", "success")

    def _make_tooltip(self, widget, text):
        """Show a themed tooltip under the widget after a short hover delay.

        `text` may be a string or a zero-arg callable resolved at show time.
        """
        state = {"win": None, "job": None}

        def show():
            state["job"] = None
            if state["win"] is not None:
                return
            tw = tk.Toplevel(self)
            tw.wm_overrideredirect(True)
            tk.Label(
                tw, text=text() if callable(text) else text, font=FONT_SMALL,
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

        win, body = self._make_popup("Settings", body_bg=BG_SECTION)
        win.geometry(f"+{self.winfo_rootx() + self.winfo_width() - 320}"
                     f"+{self.winfo_rooty() + 50}")
        self._settings_win = win

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
        radio_row([("M4A (AAC 256kbps)", "m4a"), ("MP3 (VBR Q0)", "mp3"),
                   ("Opus (original)", "opus")],
                  self._format_var, self._on_format_change)
        tk.Label(
            body, text="Opus keeps YouTube's original audio with no re-encode.\n"
                       "Use M4A for iPhone and iTunes.",
            font=FONT_SMALL, bg=BG_SECTION, fg=FG_DIM, justify="left",
        ).pack(anchor="w", pady=(2, 0))

        section("Preferred metadata")
        radio_row([("iTunes", "itunes"), ("YouTube", "youtube")],
                  self._pref_meta_var, self._on_pref_meta_change)

        section("Preferred artwork")
        radio_row([("iTunes", "itunes"), ("YouTube", "youtube")],
                  self._pref_artwork_var, self._on_pref_artwork_change)

        section("Playlist links")
        tk.Checkbutton(
            body, text="Ask what to download from playlists",
            variable=self._ask_playlist_var, font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_TEXT, selectcolor=BG_INPUT,
            activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0,
            command=self._on_ask_playlist_change,
        ).pack(anchor="w")
        tk.Checkbutton(
            body, text="Ignore auto-generated Mixes (radio links)",
            variable=self._ignore_mixes_var, font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_TEXT, selectcolor=BG_INPUT,
            activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0,
            command=self._on_ignore_mixes_change,
        ).pack(anchor="w")
        tk.Label(
            body, text="Mix links (RD...) always grab just the pasted video\n"
                       "when ignored; real playlists and albums still ask.\n"
                       "With Turbo on and asking on, Turbo downloads the\n"
                       "whole playlist.",
            font=FONT_SMALL, bg=BG_SECTION, fg=FG_DIM, justify="left",
        ).pack(anchor="w", pady=(2, 0))

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
        self._reveal_file(self._last_download_path)

    def _on_open_after_change(self):
        self.config.open_after_download = self._open_after_var.get()
        self.config.save()

    def _on_download(self):
        if not self._current_url:
            self._status_bar.append("No URL to download.", "warning")
            if self._queue_running:
                self._finish_queue("Queue stopped (no URL).", "warning")
            return

        if not self.config.download_folder or not os.path.isdir(self.config.download_folder):
            self._status_bar.append("Please set a valid download folder.", "warning")
            if self._queue_running:
                # Without this, an auto-mode queue would wedge forever with
                # no visible way to cancel it
                self._finish_queue(
                    "Queue stopped: set a valid download folder first.",
                    "warning")
            return

        self._set_ui_state("downloading")
        self._show_skip(False)
        if not self._queue_running:
            # Keep the running queue narrative visible across items
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
        self._status_bar.append("Download complete.", "success")
        self._progress_var.set(100)
        self._set_ui_state("ready")
        self._append_history(result.filename, result.output_path,
                             self._current_url)

        if self._open_after_var.get() and os.path.isfile(result.output_path):
            try:
                os.startfile(result.output_path)
            except Exception:
                pass

        if self._queue_running:
            self._queue_advance_or_finish()

    def _on_download_cancelled(self):
        self._status_bar.append("Download cancelled.", "warning")
        self._progress_var.set(0)
        self._set_ui_state("ready")

    def _on_download_error(self, error_msg):
        self._status_bar.append(f"Error: {error_msg}", "error")
        self._progress_var.set(0)
        self._set_ui_state("ready")
        if self._queue_running:
            if self._queue:
                self._status_bar.append("Skipping to the next video in the queue...", "warning")
                self.after(600, self._next_in_queue)
            else:
                self._finish_queue("Queue finished (last video failed).", "warning")

    def _on_cancel(self):
        if self._queue_running:
            self._finish_queue("Queue cancelled.", "warning")
        self.pipeline.cancel()
        self._cancel_btn.config(state="disabled")
        self._status_bar.append("Cancelling...", "warning")

    def _on_status(self, msg):
        self._status_bar.append(msg, "dim")

    def _on_progress(self, pct, msg):
        self._progress_var.set(pct)

    # ---- UI state ----

    def _set_ui_state(self, state):
        self._ui_state = state
        states = {
            "idle":        ("normal",   "normal",   "disabled", "disabled"),
            "fetching":    ("disabled", "disabled", "disabled", "disabled"),
            "ready":       ("normal",   "normal",   "normal",   "disabled"),
            "downloading": ("disabled", "disabled", "disabled", "normal"),
        }
        fetch, url, dl, cancel = states.get(state, states["idle"])
        self._fetch_btn.config(state=fetch, cursor="hand2" if fetch == "normal" else "")
        self._search_btn.config(state=fetch, cursor="hand2" if fetch == "normal" else "")
        self._url_entry.config(state=url)
        self._download_btn.config(state=dl, cursor="hand2" if dl == "normal" else "")
        self._cancel_btn.config(state=cancel, cursor="hand2" if cancel == "normal" else "")

    # ---- Startup tasks ----

    def run_startup_tasks(self):
        def worker():
            # Surface a crash from the previous session, once
            crash_path = os.path.join(app_dir(), "crash.log")
            try:
                if os.path.isfile(crash_path) and os.path.getsize(crash_path) > 0:
                    self.after(0, self._status_bar.append,
                               "The previous session hit an error. Details are in "
                               "crash.log next to the app.", "warning")
                    old = crash_path + ".old"
                    if os.path.exists(old):
                        os.remove(old)
                    os.rename(crash_path, old)
            except OSError:
                pass

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
            self.after(0, self._search_btn.config, state="normal", cursor="hand2")
            self.after(0, self._status_bar.append, "Ready.", "success")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_self_update(self):
        self._status_bar.append(
            "Update ready. YTYoink will close and restart itself...", "success")
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
