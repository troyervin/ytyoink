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
from paths import asset_dir
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
from gui.widgets import CheckboxEntry, ImagePreview, StatusBar

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
        self.after(50, self._enforce_min_height)

    def _build_window(self):
        self.title("YTYoink - SERG Edition")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.configure(bg=BG_MAIN)

    def _enforce_min_height(self):
        """Set minimum window height to the actual rendered content height."""
        self.update_idletasks()
        content_h = self._main_frame.winfo_reqheight()
        self.minsize(WINDOW_MIN_WIDTH, content_h)

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

    def _make_button(self, parent, text, command, bg, fg, hover_bg, hover_fg=None, font=None, padx=16, pady=3, state="normal"):
        """Create a styled button with hover effect."""
        btn = tk.Button(
            parent, text=text, font=font or FONT_BUTTON,
            bg=bg, fg=fg,
            activebackground=hover_bg, activeforeground=hover_fg or fg,
            relief="flat", padx=padx, pady=pady, bd=0,
            command=command, state=state,
            cursor="hand2" if state == "normal" else "",
        )
        apply_hover(btn, bg, hover_bg, fg, hover_fg)
        return btn

    def _build_layout(self):
        # Main canvas for scroll support
        self._canvas = tk.Canvas(self, bg=BG_MAIN, highlightthickness=0, bd=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview, style="Custom.Vertical.TScrollbar")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)

        self._main_frame = tk.Frame(self._canvas, bg=BG_MAIN)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._main_frame, anchor="nw")

        self._main_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        frame = self._main_frame

        # ---- Header: Logo + Title ----
        header_frame = tk.Frame(frame, bg=BG_MAIN)
        header_frame.pack(pady=(10, 2))

        self._logo_photo = self._load_logo()
        if self._logo_photo:
            tk.Label(
                header_frame, image=self._logo_photo, bg=BG_MAIN,
            ).pack(side="left", padx=(0, 8))

        title_frame = tk.Frame(header_frame, bg=BG_MAIN)
        title_frame.pack(side="left")

        tk.Label(
            title_frame, text="YTYoink", font=FONT_HEADING,
            bg=BG_MAIN, fg=FG_ACCENT,
        ).pack(anchor="w")

        tk.Label(
            title_frame, text="YouTube Audio Downloader", font=FONT_SUBHEADING,
            bg=BG_MAIN, fg=FG_DIM,
        ).pack(anchor="w")

        # Thin separator under header
        tk.Frame(frame, bg=BORDER_COLOR, height=1).pack(fill="x", padx=PAD_SECTION, pady=(6, 8))

        # ---- URL Input ----
        url_outer, url_inner = make_card(frame, "URL")
        url_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)

        url_row = tk.Frame(url_inner, bg=BG_SECTION)
        url_row.pack(fill="x")

        self._url_entry = tk.Entry(
            url_row, font=FONT_INPUT, bg=BG_INPUT, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat",
            highlightthickness=1, highlightcolor=FG_ACCENT, highlightbackground=BORDER_COLOR,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)

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

        # ---- Video Info Panel (hidden until fetched) ----
        self._info_outer = tk.Frame(frame, bg=BG_MAIN)
        info_label = tk.Label(
            self._info_outer, text="Video Info", font=FONT_LABEL_BOLD,
            bg=BG_MAIN, fg=FG_LABEL, anchor="w",
        )
        info_label.pack(fill="x", padx=2, pady=(0, 3))

        info_card = tk.Frame(
            self._info_outer, bg=BG_SECTION,
            highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
            highlightthickness=1, padx=PAD_CARD, pady=PAD_CARD,
        )
        info_card.pack(fill="x")

        info_inner = tk.Frame(info_card, bg=BG_SECTION)
        info_inner.pack(fill="x")

        self._thumb_preview = ImagePreview(info_inner, size=THUMBNAIL_SIZE)
        self._thumb_preview.pack(side="left", padx=(0, PAD_X))

        info_text = tk.Frame(info_inner, bg=BG_SECTION)
        info_text.pack(side="left", fill="both", expand=True)

        self._info_title = tk.Label(
            info_text, text="", font=FONT_LABEL_BOLD,
            bg=BG_SECTION, fg=FG_TEXT, wraplength=380, justify="left", anchor="nw",
        )
        self._info_title.pack(anchor="w", pady=(0, 4))

        self._info_uploader = tk.Label(
            info_text, text="", font=FONT_LABEL,
            bg=BG_SECTION, fg=FG_LABEL, anchor="w",
        )
        self._info_uploader.pack(anchor="w", pady=(0, 2))

        self._info_duration = tk.Label(
            info_text, text="", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_DIM, anchor="w",
        )
        self._info_duration.pack(anchor="w")
        # Not packed yet — shown after fetch

        # ---- Format + Cover Source (side by side) ----
        settings_row = tk.Frame(frame, bg=BG_MAIN)
        settings_row.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)
        settings_row.columnconfigure(0, weight=1, uniform="settings")
        settings_row.columnconfigure(1, weight=1, uniform="settings")

        # Format
        fmt_outer, fmt_inner = make_card(settings_row, "Output Format")
        fmt_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self._format_var = tk.StringVar(value=self.config.format)

        fmt_row = tk.Frame(fmt_inner, bg=BG_SECTION)
        fmt_row.pack(anchor="w")

        tk.Radiobutton(
            fmt_row, text="M4A (AAC 256kbps)", variable=self._format_var,
            value="m4a", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0,
            command=self._on_format_change,
        ).pack(side="left", padx=(0, 10))

        tk.Radiobutton(
            fmt_row, text="MP3 (VBR Q0)", variable=self._format_var,
            value="mp3", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0,
            command=self._on_format_change,
        ).pack(side="left")

        # Preferred Source
        pref_outer, pref_inner = make_card(settings_row, "Preferred Source")
        pref_outer.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self._pref_meta_var = tk.StringVar(value=self.config.metadata_source)
        self._pref_artwork_var = tk.StringVar(value=self.config.cover_source)

        # Metadata preference row
        meta_pref_row = tk.Frame(pref_inner, bg=BG_SECTION)
        meta_pref_row.pack(fill="x", pady=(0, 2))

        tk.Label(
            meta_pref_row, text="Metadata:", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_LABEL, width=9, anchor="w",
        ).pack(side="left")

        tk.Radiobutton(
            meta_pref_row, text="iTunes", variable=self._pref_meta_var,
            value="itunes", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_pref_meta_change,
        ).pack(side="left", padx=(0, 6))

        tk.Radiobutton(
            meta_pref_row, text="YouTube", variable=self._pref_meta_var,
            value="youtube", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_pref_meta_change,
        ).pack(side="left")

        # Artwork preference row
        art_pref_row = tk.Frame(pref_inner, bg=BG_SECTION)
        art_pref_row.pack(fill="x")

        tk.Label(
            art_pref_row, text="Artwork:", font=FONT_SMALL,
            bg=BG_SECTION, fg=FG_LABEL, width=9, anchor="w",
        ).pack(side="left")

        tk.Radiobutton(
            art_pref_row, text="iTunes", variable=self._pref_artwork_var,
            value="itunes", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_pref_artwork_change,
        ).pack(side="left", padx=(0, 6))

        tk.Radiobutton(
            art_pref_row, text="YouTube", variable=self._pref_artwork_var,
            value="youtube", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_pref_artwork_change,
        ).pack(side="left")

        # ---- Metadata Overrides ----
        meta_outer, meta_inner = make_card(frame, "Metadata  (check to override)")
        meta_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)

        # Metadata source toggle (shown only when iTunes data is available)
        self._keep_overrides_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            meta_inner, text="Keep overrides on fetch",
            variable=self._keep_overrides_var,
            font=FONT_SMALL, bg=BG_SECTION, fg=FG_LABEL,
            activebackground=BG_SECTION, activeforeground=FG_LABEL,
            selectcolor=BG_INPUT, highlightthickness=0, bd=0,
        ).pack(anchor="w", pady=(0, 4))

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

        # Cover art preview area (shown in "Ask" mode)
        self._cover_preview_frame = tk.Frame(meta_inner, bg=BG_SECTION)

        covers_row = tk.Frame(self._cover_preview_frame, bg=BG_SECTION)
        covers_row.pack(pady=(PAD_Y, 0))

        self._itunes_cover_frame = tk.Frame(covers_row, bg=BG_SECTION)
        self._itunes_cover_frame.pack(side="left", padx=(0, 12))
        self._itunes_cover_preview = ImagePreview(self._itunes_cover_frame, size=COVER_PREVIEW_SIZE)
        self._itunes_cover_preview.pack()

        self._yt_cover_preview = ImagePreview(covers_row, size=COVER_PREVIEW_SIZE)
        self._yt_cover_preview.pack(side="left")

        self._cover_choice_var = tk.StringVar(value="itunes")

        radio_row = tk.Frame(self._cover_preview_frame, bg=BG_SECTION)
        radio_row.pack(pady=(4, 0))

        self._itunes_art_radio = tk.Radiobutton(
            radio_row, text="Use iTunes", variable=self._cover_choice_var,
            value="itunes", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_artwork_choice_change,
        )
        self._itunes_art_radio.grid(row=0, column=0, padx=6)

        self._none_art_radio = tk.Radiobutton(
            radio_row, text="None", variable=self._cover_choice_var,
            value="none", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_artwork_choice_change,
        )
        self._none_art_radio.grid(row=0, column=1, padx=6)

        self._yt_art_radio = tk.Radiobutton(
            radio_row, text="Use YouTube", variable=self._cover_choice_var,
            value="youtube", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_artwork_choice_change,
        )
        self._yt_art_radio.grid(row=0, column=2, padx=6)

        self._custom_art_radio = tk.Radiobutton(
            radio_row, text="Custom", variable=self._cover_choice_var,
            value="custom", font=FONT_SMALL, bg=BG_SECTION, fg=FG_TEXT,
            selectcolor=BG_INPUT, activebackground=BG_SECTION, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0, command=self._on_artwork_choice_change,
        )
        self._custom_art_radio.grid(row=0, column=3, padx=6)

        # Custom artwork drop zone (shown when "Custom" selected)
        self._custom_drop_frame = tk.Frame(self._cover_preview_frame, bg=BG_SECTION)

        drop_hint = "Drop image, paste (Ctrl+V), or click to browse" if HAS_WINDND \
            else "Paste (Ctrl+V) or click to browse"
        self._drop_zone = tk.Canvas(
            self._custom_drop_frame, bg=BG_INPUT, height=32,
            highlightthickness=0, cursor="hand2",
        )
        self._drop_zone.pack(fill="x", pady=(4, 0))
        self._drop_zone.bind("<Configure>", self._draw_drop_zone_border)
        self._drop_zone.bind("<Button-1>", lambda e: self._on_browse_custom_art())
        self._dz_hint_text = drop_hint
        self._dz_file_text = ""

        if HAS_WINDND:
            windnd.hook_dropfiles(self._drop_zone, func=self._on_drop_files)

        # Bind Ctrl+V globally for clipboard paste
        self.bind_all("<Control-v>", self._on_paste_image)

        # Custom cover preview (added to covers_row, far right)
        self._custom_cover_frame = tk.Frame(covers_row, bg=BG_SECTION)
        self._custom_cover_preview = ImagePreview(self._custom_cover_frame, size=COVER_PREVIEW_SIZE)
        self._custom_cover_preview.pack()

        # ---- Download Folder ----
        folder_outer, folder_inner = make_card(frame, "Download Folder")
        folder_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y)

        folder_row = tk.Frame(folder_inner, bg=BG_SECTION)
        folder_row.pack(fill="x")

        self._folder_entry = tk.Entry(
            folder_row, font=FONT_INPUT, bg=BG_INPUT, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", state="readonly",
            readonlybackground=BG_INPUT,
            highlightthickness=1, highlightcolor=FG_LABEL, highlightbackground=BORDER_COLOR,
        )
        self._folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=2)

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

        # ---- Action Buttons ----
        btn_frame = tk.Frame(frame, bg=BG_MAIN)
        btn_frame.pack(fill="x", padx=PAD_SECTION, pady=(PAD_Y, 4))

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

        # Open after download checkbox
        self._open_after_var = tk.BooleanVar(value=self.config.open_after_download)
        tk.Checkbutton(
            btn_frame, text="Open file after download", variable=self._open_after_var,
            font=FONT_SMALL, bg=BG_MAIN, fg=FG_LABEL,
            selectcolor=BG_INPUT, activebackground=BG_MAIN, activeforeground=FG_TEXT,
            highlightthickness=0, bd=0,
            command=self._on_open_after_change,
        ).pack(side="right")

        # ---- Last Download (clickable — reveals file in Explorer) ----
        self._last_dl_label = tk.Label(
            frame, text="", font=FONT_SMALL,
            bg=BG_MAIN, fg=FG_DIM, anchor="w", cursor="hand2",
        )
        self._last_dl_label.pack(fill="x", padx=PAD_SECTION + 2, pady=(2, 0))
        self._last_dl_label.bind("<Button-1>", self._on_last_dl_click)

        # ---- Progress Bar ----
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(
            frame, variable=self._progress_var,
            maximum=100, mode="determinate",
            style="Custom.Horizontal.TProgressbar",
        )
        self._progress_bar.pack(fill="x", padx=PAD_SECTION, pady=(PAD_Y, 3))

        # ---- Status Area ----
        self._status_bar = StatusBar(frame, height=7)
        self._status_bar.pack(fill="both", expand=True, padx=PAD_SECTION, pady=(3, 6))

        # "SERG EDITION" badge at very bottom
        tk.Label(
            frame, text=" SERG EDITION ", font=("Cascadia Code", 6),
            bg=BG_MAIN, fg=FG_DIM,
            highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
            highlightthickness=1, padx=3, pady=0,
        ).pack(pady=(0, PAD_SECTION))

    # ---- Scrolling ----

    def _on_frame_configure(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
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
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        if canvas_w > 1:
            self._canvas.itemconfig(self._canvas_window, width=canvas_w)
        self._canvas.itemconfig(self._canvas_window, height=max(natural_h, canvas_h))
        self._update_scrollbar_visibility()

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
        self._info_duration.config(text=f"Duration: {mins}:{secs:02d}")

        if yt_thumb:
            self._thumb_preview.set_image(yt_thumb)
        else:
            self._thumb_preview.set_placeholder("No thumbnail")

        # Show video info panel after the URL card
        self._info_outer.pack(fill="x", padx=PAD_SECTION, pady=PAD_Y, before=self._get_widget_after_info())

        # Show/hide metadata source toggle
        if itunes_meta:
            self._meta_source_frame.pack(fill="x", pady=(0, 6), before=self._meta_title)
            # Default to saved preference (iTunes preferred when available)
            self._meta_source_var.set(self.config.metadata_source)
        else:
            self._meta_source_var.set("youtube")
            self._meta_source_frame.pack_forget()

        # Clear checked overrides unless "Keep overrides" is enabled
        if not self._keep_overrides_var.get():
            for w in (self._meta_title, self._meta_artist, self._meta_album,
                      self._meta_year, self._meta_genre):
                w.reset()

        self._apply_meta_source()
        self._update_cover_previews()

        self._status_bar.append("Video info fetched successfully.", "success")
        self._set_ui_state("ready")
        self.after(0, self._sync_canvas)

    def _on_fetch_error(self, error_msg):
        self._status_bar.append(f"Error: {error_msg}", "error")
        self._set_ui_state("idle")

    def _get_widget_after_info(self):
        children = self._main_frame.pack_slaves()
        for child in children:
            # Find the settings_row (format + cover) frame
            if isinstance(child, tk.Frame) and child.winfo_children():
                try:
                    grandchildren = child.grid_slaves()
                    if grandchildren:
                        return child
                except Exception:
                    pass
        return None

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

    def _on_artwork_choice_change(self):
        """User switched artwork choice on-the-fly in the cover art radios.

        Ephemeral per-fetch override — does NOT update the saved preference.
        Shows/hides the custom drop zone based on selection.
        """
        val = self._cover_choice_var.get()
        if val == "custom":
            self._custom_drop_frame.pack(fill="x", pady=(4, 0))
            if self._custom_cover_bytes:
                self._custom_cover_frame.pack(side="left", padx=(12, 0))
        else:
            self._custom_drop_frame.pack_forget()
            self._custom_cover_frame.pack_forget()
            # Clear custom art when user explicitly picks another option
            self._custom_cover_path = None
            self._custom_cover_bytes = None
            self._dz_file_text = ""
            self._draw_drop_zone_border()

    def _draw_drop_zone_border(self, event=None):
        """Draw dashed border and text on the drop zone canvas."""
        c = self._drop_zone
        c.delete("all")
        w = c.winfo_width() or 200
        h = c.winfo_height() or 32
        c.create_rectangle(
            3, 3, w - 3, h - 3,
            outline=FG_ACCENT, dash=(6, 4), width=1,
        )
        if self._dz_file_text:
            c.create_text(w // 2, h // 2, text=f"{self._dz_file_text}  \u2022  click to change",
                          fill=FG_ACCENT, font=FONT_SMALL, anchor="center")
        else:
            c.create_text(w // 2, h // 2, text=self._dz_hint_text,
                          fill=FG_DIM, font=FONT_SMALL, anchor="center")

    def _load_custom_image(self, data: bytes, path: str | None, label: str):
        """Shared loader for custom artwork from browse, paste, or drop."""
        import tempfile

        if path is None:
            # Clipboard paste — save to temp file for the downloader pipeline
            path = os.path.join(tempfile.gettempdir(), "ytyoink_custom_cover.png")
            with open(path, "wb") as f:
                f.write(data)

        self._custom_cover_path = path
        self._custom_cover_bytes = data
        self._custom_cover_preview.set_image(data, "Custom")
        self._custom_cover_frame.pack(side="left", padx=(12, 0))
        self._dz_file_text = label
        self._draw_drop_zone_border()

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
            self._load_custom_image(data, path, os.path.basename(path))
        except Exception as e:
            self._dz_file_text = f"Error: {e}"
            self._draw_drop_zone_border()

    def _on_paste_image(self, event=None):
        """Handle Ctrl+V — load image from clipboard if Custom artwork is active."""
        if self._cover_choice_var.get() != "custom":
            return
        if not self._cover_preview_frame.winfo_ismapped():
            return
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
            data = buf.getvalue()
            self._load_custom_image(data, None, "Pasted from clipboard")
        elif isinstance(clip, list):
            # List of file paths from clipboard
            img_exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp")
            for fpath in clip:
                if isinstance(fpath, str) and os.path.isfile(fpath) and \
                        fpath.lower().endswith(img_exts):
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self._load_custom_image(data, fpath, os.path.basename(fpath))
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
                    # Auto-select Custom radio if not already
                    self._cover_choice_var.set("custom")
                    self._custom_drop_frame.pack(fill="x", pady=(4, 0))
                    self._load_custom_image(data, fpath, os.path.basename(fpath))
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
        """Show cover previews after fetch. Hide iTunes section if unavailable."""
        if not (self._yt_thumb_bytes or self._itunes_cover_bytes):
            self._cover_preview_frame.pack_forget()
            return

        self._cover_preview_frame.pack(fill="x", pady=(PAD_Y, 0))

        if self._yt_thumb_bytes:
            self._yt_cover_preview.set_image(self._yt_thumb_bytes, "YouTube")
        else:
            self._yt_cover_preview.set_placeholder("Not available")

        if self._itunes_cover_bytes:
            self._itunes_cover_frame.pack(side="left", padx=(0, 12))
            self._itunes_cover_preview.set_image(self._itunes_cover_bytes, "iTunes")
            self._itunes_art_radio.grid()
        else:
            self._itunes_cover_frame.pack_forget()
            self._itunes_art_radio.grid_remove()

        # Custom artwork persists across fetches — takes priority over preference
        if self._custom_cover_bytes:
            self._cover_choice_var.set("custom")
            self._custom_cover_frame.pack(side="left", padx=(12, 0))
            self._custom_cover_preview.set_image(self._custom_cover_bytes, "Custom")
            self._custom_drop_frame.pack(fill="x", pady=(4, 0))
        else:
            self._custom_cover_frame.pack_forget()
            self._custom_drop_frame.pack_forget()
            # No custom art — pre-select from saved preference
            if self._itunes_cover_bytes:
                pref = self.config.cover_source
                if pref in ("itunes", "youtube", "none"):
                    self._cover_choice_var.set(pref)
                else:
                    self._cover_choice_var.set("itunes")
            else:
                if self._cover_choice_var.get() == "itunes":
                    self._cover_choice_var.set("youtube")

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
        self._status_bar.append("Restarting to apply update...", "success")
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
