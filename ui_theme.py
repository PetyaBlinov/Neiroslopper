"""Desktop palette and quiet, code-drawn circuit accents."""
import tkinter as tk
from tkinter import ttk

BACKGROUND = "#081a30"
SURFACE = "#102943"
FIELD = "#0b2038"
INK = "#edf3fa"
MUTED = "#a3b7ce"
GOLD = "#e9bd62"
EDGE = "#294560"


class CircuitDivider(tk.Canvas):
    """A decorative trace in the header; never an interactive overlay."""
    def __init__(self, parent):
        super().__init__(parent, height=18, bg=BACKGROUND, highlightthickness=0,
                         takefocus=0, borderwidth=0)
        self.bind("<Configure>", self.draw)

    def draw(self, event):
        self.delete("all")
        w = event.width
        self.create_line(0, 11, w * .59, 11, w * .59 + 8, 3, w - 38, 3,
                         fill="#7b653c", width=1)
        self.create_line(0, 5, 112, 5, 120, 13, 208, 13, fill=GOLD, width=2)
        for x, y in ((208, 13), (w - 38, 3)):
            self.create_oval(x - 3, y - 3, x + 3, y + 3,
                             fill=BACKGROUND, outline=GOLD, width=1)


def apply_theme(root):
    root.configure(bg=BACKGROUND)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", "#315274")
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), foreground=INK, background=SURFACE)
    style.configure("TFrame", background=SURFACE)
    style.configure("App.TFrame", background=BACKGROUND)
    style.configure("Card.TFrame", background=SURFACE, relief="flat", borderwidth=0)
    style.configure("TLabel", background=SURFACE, foreground=INK)
    style.configure("Title.TLabel", background=BACKGROUND, foreground=INK, font=("Segoe UI", 23, "bold"))
    style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED)
    style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Section.TLabel", background=SURFACE, foreground=GOLD, font=("Segoe UI", 11, "bold"))
    style.configure("Status.TLabel", background=BACKGROUND, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("TButton", background="#1c3b59", foreground=INK, bordercolor="#1c3b59",
                    lightcolor="#1c3b59", darkcolor="#1c3b59", padding=(12, 8), relief="flat", borderwidth=0)
    style.map("TButton", background=[("disabled", "#142d46"), ("pressed", "#355c7e"), ("active", "#294d6e")],
              foreground=[("disabled", "#8196ac")], bordercolor=[("focus", GOLD)])
    style.configure("Accent.TButton", background=GOLD, foreground=BACKGROUND,
                    font=("Segoe UI", 10, "bold"), padding=(20, 10))
    style.map("Accent.TButton", background=[("disabled", "#394a58"), ("pressed", "#c69a45"), ("active", "#f7d58e")],
              foreground=[("disabled", "#98a5b2")])
    style.configure("Quiet.TButton", background=SURFACE, foreground=MUTED, padding=(8, 6))
    style.map("Quiet.TButton", background=[("active", "#1c3b59")], foreground=[("active", INK), ("disabled", "#8196ac")])
    style.configure("Header.TButton", background=BACKGROUND, foreground=MUTED)
    style.configure("Jog.TButton", font=("Segoe UI", 17), padding=(10, 3))
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=SURFACE, foreground=INK, padding=(0, 3),
                        indicatorbackground=FIELD, indicatorforeground=GOLD)
        style.map(name, background=[("active", SURFACE)], foreground=[("disabled", "#8196ac")],
                  indicatorbackground=[("disabled", "#20344a"), ("selected", GOLD), ("active", "#315274")],
                  indicatorforeground=[("selected", BACKGROUND)])
    style.configure("TLabelframe", background=SURFACE, bordercolor=EDGE, relief="flat", borderwidth=0)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=GOLD, font=("Segoe UI", 10, "bold"))
    style.configure("TEntry", fieldbackground=FIELD, foreground=INK, insertcolor=INK,
                    bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, padding=6)
    style.map("TEntry", fieldbackground=[("disabled", "#172c41")], foreground=[("disabled", "#8196ac")],
              bordercolor=[("focus", GOLD)])
    style.configure("TCombobox", fieldbackground=FIELD, foreground=INK, background="#1c3b59",
                    arrowcolor=GOLD, bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, padding=5)
    style.map("TCombobox", fieldbackground=[("disabled", "#172c41"), ("readonly", FIELD)],
              foreground=[("disabled", "#8196ac"), ("readonly", INK)], bordercolor=[("focus", GOLD)])
    style.configure("TNotebook", background=SURFACE, bordercolor=SURFACE, lightcolor=SURFACE,
                    darkcolor=SURFACE, borderwidth=0, tabmargins=(0, 0, 0, 10))
    style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED, padding=(13, 10),
                    lightcolor=SURFACE, darkcolor=SURFACE, bordercolor=SURFACE, borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", "#1c3b59"), ("active", "#1c3b59")],
              foreground=[("selected", GOLD)], padding=[("selected", (13, 10))],
              lightcolor=[("selected", "#1c3b59"), ("!selected", SURFACE)],
              darkcolor=[("selected", "#1c3b59"), ("!selected", SURFACE)],
              bordercolor=[("selected", "#1c3b59"), ("!selected", SURFACE)])
    style.configure("TSeparator", background=EDGE, lightcolor=EDGE, darkcolor=EDGE, bordercolor=EDGE)
    style.configure("Horizontal.TSeparator", background=EDGE, lightcolor=EDGE, darkcolor=EDGE, bordercolor=EDGE)
    style.configure("Vertical.TScrollbar", background="#294560", troughcolor=SURFACE,
                    arrowcolor=MUTED, lightcolor=SURFACE, darkcolor=SURFACE,
                    bordercolor=SURFACE, borderwidth=0, arrowsize=12)
    style.configure("Horizontal.TProgressbar", background=GOLD, troughcolor=FIELD,
                    bordercolor=FIELD, lightcolor=GOLD, darkcolor=GOLD, thickness=4)
    return style
