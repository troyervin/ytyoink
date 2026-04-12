"""Custom compound widgets for the YTYoink GUI."""

import io
import tkinter as tk
from tkinter import ttk

from gui.styles import (
    BG_INPUT, BG_MAIN, BG_SECTION, BORDER_COLOR, FG_ACCENT, FG_DIM,
    FG_ERROR, FG_LABEL, FG_SUCCESS, FG_TEXT, FG_WARN, FONT_INPUT,
    FONT_LABEL, FONT_MONO, FONT_SMALL, PAD_X, PAD_Y,
)

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


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

        self._entry = tk.Entry(
            self,
            font=FONT_INPUT,
            width=width,
            bg=BG_INPUT,
            fg=FG_DIM,
            insertbackground=FG_TEXT,
            disabledbackground=BG_INPUT,
            disabledforeground=FG_DIM,
            state="disabled",
            relief="flat",
            highlightthickness=1,
            highlightcolor=FG_ACCENT,
            highlightbackground=BORDER_COLOR,
        )
        self._entry.grid(row=0, column=2, sticky="ew", padx=(0, 4), ipady=2)

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
    """Displays an image preview with a label underneath."""

    def __init__(self, parent, size: tuple[int, int] = (140, 140), **kwargs):
        super().__init__(parent, bg=BG_SECTION, **kwargs)
        self._size = size
        self._photo = None

        self._canvas = tk.Label(
            self,
            bg=BG_INPUT,
            width=size[0],
            height=size[1],
            relief="flat",
            highlightbackground=BORDER_COLOR,
            highlightthickness=1,
        )
        self._canvas.pack(padx=2, pady=2)

        self._label = tk.Label(
            self,
            text="",
            font=FONT_SMALL,
            bg=BG_SECTION,
            fg=FG_LABEL,
        )
        self._label.pack()

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
            img = img.resize(self._size, Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self._canvas.config(image=self._photo, width=0, height=0)
        except Exception:
            self._canvas.config(
                image="", text="Preview\nunavailable",
                fg=FG_DIM, font=FONT_SMALL,
                width=self._size[0] // 8, height=self._size[1] // 16,
            )
            self._photo = None

        self._label.config(text=label)

    def set_placeholder(self, text: str = "No preview") -> None:
        self._photo = None
        self._canvas.config(
            image="", text=text, fg=FG_DIM, font=FONT_SMALL,
            width=self._size[0] // 8, height=self._size[1] // 16,
        )
        self._label.config(text="")

    def clear(self) -> None:
        self._photo = None
        self._canvas.config(image="", text="", width=self._size[0], height=self._size[1])
        self._label.config(text="")


class StatusBar(tk.Frame):
    """Multi-line text area for status/progress messages with auto-scroll."""

    def __init__(self, parent, height: int = 8, **kwargs):
        super().__init__(parent, bg=BG_MAIN, **kwargs)

        self._text = tk.Text(
            self,
            font=FONT_MONO,
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

        scrollbar = ttk.Scrollbar(self, command=self._text.yview, style="Custom.Vertical.TScrollbar")
        self._text.config(yscrollcommand=scrollbar.set)

        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._text.tag_config("info", foreground=FG_TEXT)
        self._text.tag_config("success", foreground=FG_SUCCESS)
        self._text.tag_config("warning", foreground=FG_WARN)
        self._text.tag_config("error", foreground=FG_ERROR)
        self._text.tag_config("dim", foreground=FG_DIM)

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
