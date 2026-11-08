"""Desktop palette and quiet, code-drawn circuit accents."""
import tkinter as tk
from tkinter import ttk, font as tkfont
from display_scaling import ui_scale, scale_toplevel
from rounded_theme import install_rounded_surfaces

BACKGROUND = "#081a30"
SURFACE = "#102943"
FIELD = "#0b2038"
INK = "#f5f8ff"
MUTED = "#c5d5e8"
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
        s = ui_scale(self)
        self.create_line(0, 11*s, w * .59, 11*s, w * .59 + 8*s, 3*s, w - 38*s, 3*s,
                         fill="#9c8350", width=max(1, s))
        self.create_line(0, 5*s, 112*s, 5*s, 120*s, 13*s, 208*s, 13*s, fill=GOLD, width=2*s)
        for x, y in ((208*s, 13*s), (w - 38*s, 3*s)):
            self.create_oval(x - 3*s, y - 3*s, x + 3*s, y + 3*s,
                             fill=BACKGROUND, outline=GOLD, width=max(1, s))


def apply_theme(root):
    scale = ui_scale(root)
    def px(value):
        return round(value * scale)
    families = set(tkfont.families(root))
    family = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name, root=root).configure(family=family, size=11)
    root.configure(bg=BACKGROUND)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", "#315274")
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(family, 11), foreground=INK, background=SURFACE)
    style.configure("TFrame", background=SURFACE)
    style.configure("App.TFrame", background=BACKGROUND)
    style.configure("Card.TFrame", background=SURFACE, relief="flat", borderwidth=0)
    style.configure("TLabel", background=SURFACE, foreground=INK)
    style.configure("Title.TLabel", background=BACKGROUND, foreground=INK, font=(family, 23, "bold"))
    style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED)
    style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=(family, 10))
    style.configure("Section.TLabel", background=SURFACE, foreground=GOLD, font=(family, 11, "bold"))
    style.configure("Status.TLabel", background=BACKGROUND, foreground=MUTED, font=(family, 10))
    style.configure("TButton", background="#1c3b59", foreground=INK, bordercolor="#1c3b59",
                    lightcolor="#1c3b59", darkcolor="#1c3b59", padding=(px(13), px(9)), relief="flat", borderwidth=0)
    style.map("TButton", background=[("disabled", "#142d46"), ("pressed", "#355c7e"), ("active", "#294d6e")],
              foreground=[("disabled", "#b5c5d9")], bordercolor=[("focus", GOLD)])
    style.configure("Accent.TButton", background=GOLD, foreground=BACKGROUND,
                    font=(family, 11, "bold"), padding=(px(20), px(10)))
    style.map("Accent.TButton", background=[("disabled", "#394a58"), ("pressed", "#c69a45"), ("active", "#f7d58e")],
              foreground=[("disabled", "#d3deeb")])
    style.configure("Quiet.TButton", background=SURFACE, foreground=INK, padding=(px(10), px(8)))
    style.map("Quiet.TButton", background=[("active", "#1c3b59")], foreground=[("active", INK), ("disabled", "#b5c5d9")])
    style.configure("Header.TButton", background=BACKGROUND, foreground=MUTED)
    style.configure("Danger.TButton", foreground="#ffffff", font=(family, 11, "bold"), padding=(px(22), px(10)))
    style.configure("Jog.TButton", font=(family, 17), padding=(px(10), px(3)))
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=SURFACE, foreground=INK, padding=(0, px(3)),
                        indicatorbackground=FIELD, indicatorforeground=GOLD,
                        indicatorsize=px(14), indicatormargin=(0, 0, px(7), 0))
        style.map(name, background=[("active", SURFACE)], foreground=[("disabled", "#b5c5d9")],
                  indicatorbackground=[("disabled", "#20344a"), ("selected", GOLD), ("active", "#315274")],
                  indicatorforeground=[("selected", BACKGROUND)])
    style.configure("TLabelframe", background=SURFACE, bordercolor=EDGE, relief="flat", borderwidth=0)
    style.configure("Mode.TRadiobutton", padding=(px(12), px(8)), foreground=INK)
    style.map("Mode.TRadiobutton", foreground=[("disabled", "#b5c5d9"), ("selected", GOLD)])
    style.configure("TLabelframe.Label", background=SURFACE, foreground=GOLD, font=(family, 11, "bold"))
    style.configure("TEntry", fieldbackground=FIELD, foreground=INK, insertcolor=INK,
                    bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, padding=px(6))
    style.map("TEntry", fieldbackground=[("disabled", "#172c41")], foreground=[("disabled", "#b5c5d9")],
              bordercolor=[("focus", GOLD)])
    style.configure("TCombobox", fieldbackground=FIELD, foreground=INK, background="#1c3b59",
                    arrowcolor=GOLD, arrowsize=px(14), bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, padding=px(5))
    style.map("TCombobox", fieldbackground=[("disabled", "#172c41"), ("readonly", FIELD)],
              foreground=[("disabled", "#b5c5d9"), ("readonly", INK)], bordercolor=[("focus", GOLD)])
    style.configure("TNotebook", background=SURFACE, bordercolor=SURFACE, lightcolor=SURFACE,
                    darkcolor=SURFACE, borderwidth=0, tabmargins=(0, 0, 0, px(10)))
    style.configure("TNotebook.Tab", background=SURFACE, foreground=INK, padding=(px(13), px(10)),
                    lightcolor=SURFACE, darkcolor=SURFACE, bordercolor=SURFACE, borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", "#1c3b59"), ("active", "#1c3b59")],
              foreground=[("selected", GOLD)], padding=[("selected", (px(13), px(10)))],
              lightcolor=[("selected", "#1c3b59"), ("!selected", SURFACE)],
              darkcolor=[("selected", "#1c3b59"), ("!selected", SURFACE)],
              bordercolor=[("selected", "#1c3b59"), ("!selected", SURFACE)])
    style.configure("TSeparator", background=EDGE, lightcolor=EDGE, darkcolor=EDGE, bordercolor=EDGE)
    style.configure("Horizontal.TSeparator", background=EDGE, lightcolor=EDGE, darkcolor=EDGE, bordercolor=EDGE)
    style.configure("Vertical.TScrollbar", background="#294560", troughcolor=SURFACE,
                    arrowcolor=MUTED, lightcolor=SURFACE, darkcolor=SURFACE,
                    bordercolor=SURFACE, borderwidth=0, arrowsize=px(12))
    style.configure("Horizontal.TProgressbar", background=GOLD, troughcolor=FIELD,
                    bordercolor=FIELD, lightcolor=GOLD, darkcolor=GOLD, thickness=px(4))
    install_rounded_surfaces(root, style, scale, surface=SURFACE, background=BACKGROUND, gold=GOLD)
    root.bind_class("Toplevel", "<Map>", lambda event: scale_toplevel(event.widget), add="+")
    return style
