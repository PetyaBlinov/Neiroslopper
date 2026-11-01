"""Shared desktop palette; intentionally independent of hardware and transport."""
from tkinter import ttk

BACKGROUND = "#f1f5f2"
WHITE = "#ffffff"
INK = "#17281f"
MUTED = "#64766b"
GREEN = "#17633f"


def apply_theme(root):
    root.configure(bg=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), foreground=INK)
    style.configure("TFrame", background=WHITE)
    style.configure("App.TFrame", background=BACKGROUND)
    style.configure("Card.TFrame", background=WHITE, bordercolor="#dce5de", relief="solid", borderwidth=1)
    style.configure("TLabel", background=WHITE, foreground=INK)
    style.configure("Title.TLabel", background=BACKGROUND, foreground=INK, font=("Segoe UI", 22, "bold"))
    style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=WHITE, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Section.TLabel", background=WHITE, foreground=GREEN, font=("Segoe UI", 11, "bold"))
    style.configure("Steps.TLabel", background=BACKGROUND, foreground=GREEN, font=("Segoe UI", 10, "bold"))
    style.configure("TButton", background="#edf3ee", foreground=INK, bordercolor="#d5dfd7", padding=(10, 7), relief="flat")
    style.map("TButton", background=[("active", "#dfece3"), ("disabled", "#f3f5f3")], foreground=[("disabled", "#93a097")])
    style.configure("Accent.TButton", background=GREEN, foreground=WHITE, bordercolor=GREEN, font=("Segoe UI", 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#104d30"), ("disabled", "#dbe6de")], foreground=[("disabled", "#8b9d91")])
    style.configure("Jog.TButton", font=("Segoe UI", 16), padding=(10, 4))
    style.configure("TCheckbutton", background=WHITE, foreground=INK, padding=(0, 2))
    style.map("TCheckbutton", background=[("active", WHITE)], foreground=[("disabled", "#98a39c")])
    style.configure("TLabelframe", background=WHITE, bordercolor="#dce5de", relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=WHITE, foreground=GREEN, font=("Segoe UI", 10, "bold"))
    style.configure("TEntry", fieldbackground=WHITE, bordercolor="#cad8ce", padding=6)
    style.configure("TCombobox", fieldbackground=WHITE, background="#eaf0eb", bordercolor="#cad8ce", padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", WHITE)], foreground=[("readonly", INK)])
    style.configure("TNotebook", background=WHITE, borderwidth=0)
    style.configure("TNotebook.Tab", background="#e6eee8", foreground=MUTED, padding=(18, 9))
    style.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", GREEN)])
    style.configure("Horizontal.TProgressbar", background=GREEN, troughcolor="#e8eee9", bordercolor="#e8eee9", thickness=7)
    return style
