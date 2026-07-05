"""Custom compound widgets for the YTYoink GUI."""

import io
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from gui.styles import (
    BG_INPUT, BG_MAIN, BG_SECTION, BORDER_COLOR, FG_ACCENT, FG_DIM,
    FG_ERROR, FG_LABEL, FG_SUCCESS, FG_TEXT, FG_WARN, FONT_INPUT,
    FONT_LABEL, FONT_MONO, FONT_SMALL, PAD_X, PAD_Y,
)

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_SS = 4  # supersampling factor for crisp rounded corners


def _round_rect_image(size, radius, fill, outline=None, outline_width=0):
    """Render a smooth rounded rectangle as an RGBA PIL image."""
    w, h = size
    img = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = (outline_width * _SS) // 2 if outline else 0
    draw.rounded_rectangle(
        (inset, inset, w * _SS - 1 - inset, h * _SS - 1 - inset),
        radius=radius * _SS, fill=fill,
        outline=outline, width=outline_width * _SS if outline else 0,
    )
    return img.resize((w, h), Image.LANCZOS)


class RoundButton(tk.Canvas):
    """Rounded, hover-aware button. Drop-in for the old flat tk.Button:
    supports config(state=..., cursor=...) and cget("state")."""

    def __init__(self, parent, text, command, bg, fg, hover_bg, hover_fg=None,
                 font=None, padx=16, pady=5, radius=9, state="normal"):
        self._btn_font = font or FONT_LABEL
        f = tkfont.Font(font=self._btn_font)
        w = f.measure(text) + padx * 2
        h = f.metrics("linespace") + pady * 2
        super().__init__(parent, width=w, height=h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self._command = command
        self._fg = fg
        self._hover_fg = hover_fg or fg
        self._hovered = False
        self._photos = {}
        if HAS_PIL:
            for key, color in (("normal", bg), ("hover", hover_bg),
                               ("disabled", BG_INPUT)):
                self._photos[key] = ImageTk.PhotoImage(
                    _round_rect_image((w, h), radius, color))
            self._img_id = self.create_image(0, 0, anchor="nw")
        else:
            self._img_id = self.create_rectangle(0, 0, w, h, fill=bg, width=0)
            self._rect_colors = {"normal": bg, "hover": hover_bg,
                                 "disabled": BG_INPUT}
        self._txt_id = self.create_text(w // 2, h // 2, text=text,
                                        font=self._btn_font, fill=fg)
        super().configure(state=state,
                          cursor="hand2" if state == "normal" else "")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._redraw()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        state = kw.pop("state", None)
        result = super().configure(**kw) if kw else None
        if state is not None:
            super().configure(state=state)
            self._hovered = False
            self._redraw()
        return result

    config = configure

    def _redraw(self):
        if str(self.cget("state")) == "disabled":
            key, fg = "disabled", FG_DIM
        elif self._hovered:
            key, fg = "hover", self._hover_fg
        else:
            key, fg = "normal", self._fg
        if HAS_PIL:
            self.itemconfig(self._img_id, image=self._photos[key])
        else:
            self.itemconfig(self._img_id, fill=self._rect_colors[key])
        self.itemconfig(self._txt_id, fill=fg)

    def _on_enter(self, event):
        if str(self.cget("state")) != "disabled":
            self._hovered = True
            self._redraw()

    def _on_leave(self, event):
        self._hovered = False
        self._redraw()

    def _on_click(self, event):
        if str(self.cget("state")) != "disabled" and self._command:
            self._command()


class RoundField(tk.Canvas):
    """Rounded container around a borderless Entry, with a focus ring.
    The inner Entry is exposed as `.entry`."""

    def __init__(self, parent, height=32, radius=9, entry_padx=10,
                 **entry_kwargs):
        super().__init__(parent, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self._radius = radius
        self._entry_padx = entry_padx
        self._h = height
        self._photo = None
        self._focused = False

        defaults = dict(
            font=FONT_INPUT, bg=BG_INPUT, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", bd=0,
            highlightthickness=0,
            disabledbackground=BG_INPUT, disabledforeground=FG_DIM,
            readonlybackground=BG_INPUT,
        )
        defaults.update(entry_kwargs)
        self.entry = tk.Entry(self, **defaults)

        self._bg_id = self.create_image(0, 0, anchor="nw")
        self._win_id = self.create_window(entry_padx, height // 2, anchor="w",
                                          window=self.entry)
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))

    def _set_focus(self, focused):
        self._focused = focused
        self._render()

    def _on_resize(self, event):
        self.itemconfig(self._win_id,
                        width=max(event.width - self._entry_padx * 2, 10))
        self._render()

    def _render(self):
        if not HAS_PIL:
            return
        w = self.winfo_width()
        if w <= 1:
            return
        outline = FG_ACCENT if self._focused else BORDER_COLOR
        img = _round_rect_image(
            (w, self._h), self._radius, BG_INPUT,
            outline=outline, outline_width=2 if self._focused else 1,
        )
        self._photo = ImageTk.PhotoImage(img)
        self.itemconfig(self._bg_id, image=self._photo)


class CheckboxEntry(tk.Frame):
    """A checkbox + label + text entry on one row.

    When unchecked: entry is disabled, shows auto-detected value in dim color.
    When checked: entry is enabled for user override.
    """

    def __init__(self, parent, label_text: str, width: int = 40, **kwargs):
        super().__init__(parent, bg=BG_SECTION, **kwargs)

        self._auto_value = ""
        self._var = tk.BooleanVar(value=False)

        self._check = tk.Checkbutton(
            self,
            variable=self._var,
            command=self._on_toggle,
            bg=BG_SECTION,
            activebackground=BG_SECTION,
            selectcolor=BG_INPUT,
            fg=FG_TEXT,
            activeforeground=FG_TEXT,
            highlightthickness=0,
            bd=0,
        )
        self._check.grid(row=0, column=0, padx=(0, 2))

        self._label = tk.Label(
            self,
            text=label_text,
            font=FONT_LABEL,
            bg=BG_SECTION,
            fg=FG_LABEL,
            width=6,
            anchor="w",
        )
        self._label.grid(row=0, column=1, padx=(0, 6))

        self._field = RoundField(
            self, height=30,
            font=FONT_INPUT, width=width, fg=FG_DIM, state="disabled",
        )
        self._field.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        self._entry = self._field.entry

        self.columnconfigure(2, weight=1)

    def set_auto_value(self, value: str) -> None:
        self._auto_value = value or ""
        if not self._var.get():
            self._entry.config(state="normal")
            self._entry.delete(0, tk.END)
            self._entry.insert(0, self._auto_value)
            self._entry.config(state="disabled", fg=FG_DIM)

    def get_value(self) -> str:
        if self._var.get():
            return self._entry.get().strip()
        return self._auto_value

    def is_overridden(self) -> bool:
        return self._var.get()

    def reset(self) -> None:
        self._var.set(False)
        self._auto_value = ""
        self._entry.config(state="normal")
        self._entry.delete(0, tk.END)
        self._entry.config(state="disabled", fg=FG_DIM)

    def _on_toggle(self):
        if self._var.get():
            self._entry.config(state="normal", fg=FG_TEXT)
            if not self._entry.get().strip():
                self._entry.insert(0, self._auto_value)
        else:
            self._entry.config(state="normal")
            self._entry.delete(0, tk.END)
            self._entry.insert(0, self._auto_value)
            self._entry.config(state="disabled", fg=FG_DIM)


class ImagePreview(tk.Frame):
    """Displays a rounded-corner image preview with a label underneath."""

    ZOOM_SIZE = 320

    def __init__(self, parent, size: tuple[int, int] = (140, 140), **kwargs):
        super().__init__(parent, bg=BG_SECTION, **kwargs)
        self._size = size
        self._photo = None
        self._pil_base = None
        self._pil_full = None  # original-res square crop for hover zoom
        self._selected = False
        self._zoom_win = None
        self._zoom_job = None
        self._zoom_photo = None

        self._canvas = tk.Label(
            self, bg=BG_SECTION, bd=0, highlightthickness=0,
            compound="center", font=FONT_SMALL, fg=FG_DIM,
        )
        self._canvas.pack(padx=2, pady=2)
        self._canvas.bind("<Enter>", self._schedule_zoom)
        self._canvas.bind("<Leave>", self._hide_zoom)
        self._canvas.bind("<Button-1>", self._hide_zoom)

        self._label = tk.Label(
            self,
            text="",
            font=FONT_SMALL,
            bg=BG_SECTION,
            fg=FG_LABEL,
        )
        # Label only packs when it has text — no reserved gap otherwise

    def _set_label(self, text: str) -> None:
        self._label.config(text=text)
        if text:
            if not self._label.winfo_ismapped():
                self._label.pack()
        else:
            self._label.pack_forget()

    def _render_tile(self) -> None:
        """Apply rounded-corner mask (and selection ring) to the base image."""
        if not HAS_PIL or self._pil_base is None:
            return
        img = self._pil_base.copy().convert("RGBA")
        w, h = img.size
        radius = max(8, min(w, h) // 9)

        big_mask = Image.new("L", (w * _SS, h * _SS), 0)
        ImageDraw.Draw(big_mask).rounded_rectangle(
            (0, 0, w * _SS - 1, h * _SS - 1), radius=radius * _SS, fill=255)
        img.putalpha(big_mask.resize((w, h), Image.LANCZOS))

        if self._selected:
            ring = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
            ImageDraw.Draw(ring).rounded_rectangle(
                (_SS, _SS, w * _SS - 1 - _SS, h * _SS - 1 - _SS),
                radius=radius * _SS, outline=FG_ACCENT, width=3 * _SS)
            img.alpha_composite(ring.resize((w, h), Image.LANCZOS))

        self._photo = ImageTk.PhotoImage(img)
        self._canvas.config(image=self._photo, width=0, height=0)

    def set_image(self, image_data: bytes, label: str = "") -> None:
        if not HAS_PIL:
            self._label.config(text="Pillow not installed")
            return

        try:
            img = Image.open(io.BytesIO(image_data))
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            # Keep an original-res copy (capped) for the hover zoom preview
            full = img if side <= 700 else img.resize((700, 700), Image.LANCZOS)
            self._pil_full = full
            self._pil_base = img.resize(self._size, Image.LANCZOS)
            self._canvas.config(text="")
            self._render_tile()
        except Exception:
            self._canvas.config(
                image="", text="Preview\nunavailable",
                width=self._size[0] // 8, height=self._size[1] // 16,
            )
            self._photo = None
            self._pil_base = None
            self._pil_full = None

        self._set_label(label)

    def set_placeholder(self, text: str = "No preview") -> None:
        self._pil_full = None
        self._hide_zoom()
        if HAS_PIL:
            self._pil_base = Image.new("RGBA", self._size, BG_INPUT)
            self._canvas.config(text=text)
            self._render_tile()
        else:
            self._photo = None
            self._canvas.config(
                image="", text=text,
                width=self._size[0] // 8, height=self._size[1] // 16,
            )
        self._set_label("")

    def clear(self) -> None:
        self._photo = None
        self._pil_base = None
        self._pil_full = None
        self._hide_zoom()
        self._canvas.config(image="", text="")
        self._set_label("")

    # ---- Hover zoom: larger preview above the tile ----

    def _schedule_zoom(self, event=None):
        if self._pil_full is None or self._zoom_win is not None:
            return
        if self._zoom_job:
            self.after_cancel(self._zoom_job)
        self._zoom_job = self.after(350, self._show_zoom)

    def _show_zoom(self):
        self._zoom_job = None
        if self._pil_full is None or self._zoom_win is not None:
            return
        size = self.ZOOM_SIZE
        img = self._pil_full.resize((size, size), Image.LANCZOS).convert("RGBA")
        radius = 14
        mask = Image.new("L", (size * _SS, size * _SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size * _SS - 1, size * _SS - 1), radius=radius * _SS, fill=255)
        img.putalpha(mask.resize((size, size), Image.LANCZOS))
        ring = Image.new("RGBA", (size * _SS, size * _SS), (0, 0, 0, 0))
        ImageDraw.Draw(ring).rounded_rectangle(
            (_SS, _SS, size * _SS - 1 - _SS, size * _SS - 1 - _SS),
            radius=radius * _SS, outline=BORDER_COLOR, width=2 * _SS)
        img.alpha_composite(ring.resize((size, size), Image.LANCZOS))

        self._zoom_photo = ImageTk.PhotoImage(img)
        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        key = "#010203"  # transparency key color for the rounded corners
        tw.configure(bg=key)
        try:
            tw.attributes("-transparentcolor", key)
        except tk.TclError:
            pass
        tk.Label(tw, image=self._zoom_photo, bg=key, bd=0).pack()

        # Above the tile when there's room, otherwise below — never over it,
        # or Enter/Leave would flicker.
        screen_w = self.winfo_screenwidth()
        x = self.winfo_rootx() + self.winfo_width() // 2 - size // 2
        x = max(8, min(x, screen_w - size - 8))
        y = self.winfo_rooty() - size - 10
        if y < 5:
            y = self.winfo_rooty() + self.winfo_height() + 10
        tw.wm_geometry(f"+{x}+{y}")
        self._zoom_win = tw

    def _hide_zoom(self, event=None):
        if self._zoom_job:
            self.after_cancel(self._zoom_job)
            self._zoom_job = None
        if self._zoom_win is not None:
            self._zoom_win.destroy()
            self._zoom_win = None
            self._zoom_photo = None


class CoverTile(ImagePreview):
    """ImagePreview that acts as a selectable button — click to choose it.

    The tile keeps its name label ("iTunes", "YouTube", ...) across image
    and placeholder updates, and shows an accent border when selected.
    """

    def __init__(self, parent, name: str, command=None,
                 size: tuple[int, int] = (92, 92), **kwargs):
        super().__init__(parent, size=size, **kwargs)
        self._name = name
        self._command = command
        self._set_label(name)
        for w in (self, self._canvas, self._label):
            # add="+" keeps ImagePreview's hover-zoom bindings alive
            w.bind("<Button-1>", self._on_click, add="+")
            w.config(cursor="hand2")

    def _on_click(self, event=None):
        if self._command:
            self._command()

    def set_image(self, image_data: bytes, label: str = "") -> None:
        super().set_image(image_data, self._name)

    def set_placeholder(self, text: str = "No preview") -> None:
        super().set_placeholder(text)
        self._set_label(self._name)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._render_tile()
        self._label.config(fg=FG_ACCENT if selected else FG_LABEL)


class CollapsibleStatus(tk.Frame):
    """Adaptive status area, driven by window size (via set_log_visible):

    - Room available → full command-window log, growing with the window.
    - Window small   → log disappears, a single status line takes its place.

    Exposes the same append/update_last/clear API as StatusBar.
    """

    _COLORS = None  # populated lazily below (needs style constants)

    def __init__(self, parent, height: int = 3, **kwargs):
        super().__init__(parent, bg=BG_MAIN, **kwargs)
        self._log_visible = False
        if CollapsibleStatus._COLORS is None:
            CollapsibleStatus._COLORS = {
                "info": FG_TEXT, "success": FG_SUCCESS, "warning": FG_WARN,
                "error": FG_ERROR, "dim": FG_DIM,
            }

        self._row = tk.Frame(self, bg=BG_MAIN)
        self._row.pack(fill="x")

        self._line = tk.Label(
            self._row, text="", font=FONT_SMALL, bg=BG_MAIN, fg=FG_DIM,
            anchor="w",
        )
        self._line.pack(side="left", fill="x", expand=True, padx=(2, 0))

        self._log = StatusBar(self, height=height, font=FONT_SMALL)
        # Log not packed until the window offers enough room

    @property
    def log_visible(self) -> bool:
        return self._log_visible

    def set_log_visible(self, visible: bool) -> None:
        """Swap between the full log and the one-line status readout."""
        if visible == self._log_visible:
            return
        self._log_visible = visible
        if visible:
            self._row.pack_forget()
            self._log.pack(fill="both", expand=True, pady=(2, 0))
        else:
            self._log.pack_forget()
            self._row.pack(fill="x")

    def line_req(self) -> int:
        return self._row.winfo_reqheight()

    def log_min_req(self) -> int:
        return self._log.winfo_reqheight()

    def append(self, text: str, tag: str = "info") -> None:
        self._log.append(text, tag)
        self._set_line(text, tag)

    def update_last(self, text: str, tag: str = "info") -> None:
        self._log.update_last(text, tag)
        self._set_line(text, tag)

    def clear(self) -> None:
        self._log.clear()
        self._line.config(text="")

    def _set_line(self, text: str, tag: str) -> None:
        first = text.splitlines()[0] if text else ""
        self._line.config(text=first, fg=self._COLORS.get(tag, FG_TEXT))


class StatusBar(tk.Frame):
    """Multi-line text area for status/progress messages with auto-scroll."""

    def __init__(self, parent, height: int = 8, font=FONT_MONO, **kwargs):
        super().__init__(parent, bg=BG_MAIN, **kwargs)

        self._text = tk.Text(
            self,
            font=font,
            bg=BG_SECTION,
            fg=FG_TEXT,
            height=height,
            state="disabled",
            wrap="word",
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
            highlightcolor=BORDER_COLOR,
            padx=8,
            pady=6,
            spacing1=1,
            spacing3=1,
        )

        self._scrollbar = ttk.Scrollbar(self, command=self._text.yview, style="Custom.Vertical.TScrollbar")
        self._text.config(yscrollcommand=self._on_text_scroll)
        self._scroll_visible = False

        self._text.pack(side="left", fill="both", expand=True)
        # Scrollbar packs itself only when the content actually overflows

        self._text.tag_config("info", foreground=FG_TEXT)
        self._text.tag_config("success", foreground=FG_SUCCESS)
        self._text.tag_config("warning", foreground=FG_WARN)
        self._text.tag_config("error", foreground=FG_ERROR)
        self._text.tag_config("dim", foreground=FG_DIM)

    def _on_text_scroll(self, first, last):
        """Auto-hide the scrollbar while all content fits in view."""
        overflows = not (float(first) <= 0.0 and float(last) >= 1.0)
        if overflows and not self._scroll_visible:
            self._scrollbar.pack(side="right", fill="y")
            self._scroll_visible = True
        elif not overflows and self._scroll_visible:
            self._scrollbar.pack_forget()
            self._scroll_visible = False
        self._scrollbar.set(first, last)

    def append(self, text: str, tag: str = "info") -> None:
        self._text.config(state="normal")
        # update_last leaves the last line without a trailing \n; inserting at
        # END would concatenate onto that line, so add the missing newline first.
        if self._text.get("end-2c", "end-1c") not in ("\n", ""):
            self._text.insert("end-1c", "\n")
        self._text.insert(tk.END, text + "\n", tag)
        self._text.see(tk.END)
        self._text.config(state="disabled")

    def update_last(self, text: str, tag: str = "info") -> None:
        """Replace the last line in place — used for download progress."""
        self._text.config(state="normal")
        self._text.delete("end-1c linestart", "end-1c")
        self._text.insert("end-1c", text, tag)
        self._text.see(tk.END)
        self._text.config(state="disabled")

    def clear(self) -> None:
        self._text.config(state="normal")
        self._text.delete("1.0", tk.END)
        self._text.config(state="disabled")
