from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import constants


def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    c = constants.COLORS
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=c["bg"], foreground=c["text"])
    style.configure("TFrame", background=c["bg"])
    style.configure("TLabel", background=c["bg"], foreground=c["text_dim"], font=(None, 10))
    style.configure("Header.TLabel", foreground=c["accent"], font=(None, 14, "bold"))
    style.configure("Section.TLabel", foreground=c["accent"], font=(None, 11, "bold"))
    style.configure("TLabelframe", background=c["bg_panel"], foreground=c["accent"])
    style.configure("TLabelframe.Label", background=c["bg_panel"], foreground=c["accent"])
    style.map("TNotebook", background=[("selected", c["tab_selected"])])
    style.configure("TNotebook", background=c["bg"], borderwidth=0, relief=tk.FLAT)
    style.configure("TNotebook.Tab", background=c["tab_bg"], foreground=c["text_dim"], padding=(12, 6))
    style.map(
        "TNotebook.Tab",
        background=[("selected", c["tab_selected"])],
        foreground=[("selected", c["accent"])],
    )
    style.configure(
        "TEntry",
        fieldbackground=c["bg_input"],
        foreground=c["text"],
        insertcolor="white",
        bordercolor=c["border_input"],
    )
    style.configure(
        "TCombobox",
        fieldbackground=c["bg_input"],
        background=c["bg_input"],
        foreground=c["text"],
        arrowcolor=c["text"],
        bordercolor=c["border_input"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", c["bg_input"])],
        selectbackground=[("readonly", c["bg_input"])],
        selectforeground=[("readonly", c["text"])],
    )
    style.configure(
        "Horizontal.TScale",
        background=c["bg"],
        troughcolor=c["border_input"],
        sliderrelief=tk.FLAT,
    )
    style.configure(
        "TCheckbutton",
        background=c["bg"],
        foreground=c["text_dim"],
        font=(None, 10),
    )
    style.map("TCheckbutton", background=[("active", c["bg"])])
    root.configure(bg=c["bg"])
    return style


def tk_button(parent, text, command=None, bg="#2A3E55", fg="white", small=False) -> tk.Button:
    c = constants.COLORS
    font = (None, 10) if small else (None, 11)
    padx, pady = (8, 3) if small else (12, 5)
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=bg,
        activeforeground=fg,
        relief=tk.FLAT,
        cursor="hand2",
        font=font,
        padx=padx,
        pady=pady,
    )
