"""Explicit local-address choice; this module never opens a network connection."""
import ipaddress
from urllib.parse import urlsplit

from network_transport import parse_tcp_endpoint


def normalize_board_address(value):
    if not isinstance(value, str):
        raise ValueError("Введите адрес платы в общей сети")
    text = value.strip()
    if not text or len(text) > 300 or any(char.isspace() or ord(char) < 32 for char in text):
        raise ValueError("Введите адрес без пробелов, например fluidnc.local или 192.168.1.50")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        candidate = text if "://" in text else "tcp://" + text
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError as exc:
            raise ValueError("Проверьте адрес платы и номер порта") from exc
        if port is None:
            if parts.netloc.endswith(":"):
                raise ValueError("Укажите номер порта после двоеточия")
            candidate = parts._replace(netloc=parts.netloc + ":23").geturl()
    else:
        host = f"[{address}]" if address.version == 6 else str(address)
        candidate = f"tcp://{host}:23"
    return parse_tcp_endpoint(candidate).url


def ask_board_address(parent, *, initial="fluidnc.local"):
    """Compact, themed modal. Returning an address does not connect the app."""
    import tkinter as tk
    from tkinter import ttk
    from branding import apply_app_icon
    from display_scaling import ui_scale, scale_widget_tree
    from ui_theme import BACKGROUND

    result = None
    window = tk.Toplevel(parent)
    window.title("Neiroslopper · Адрес платы")
    window.configure(bg=BACKGROUND)
    window.transient(parent)
    window.resizable(False, False)
    scale = ui_scale(parent)
    width, height = round(530 * scale), round(250 * scale)
    x = max(0, (parent.winfo_screenwidth() - width) // 2)
    y = max(0, (parent.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window._neiroslopper_geometry_scaled = True
    apply_app_icon(window)
    body = ttk.Frame(window, style="App.TFrame", padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Адрес платы в общей сети", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
    value = tk.StringVar(master=window, value=initial)
    status = tk.StringVar(master=window, value="IP или имя, например fluidnc.local. Порт по умолчанию — 23.")
    entry = ttk.Entry(body, textvariable=value)
    entry.pack(fill="x")
    ttk.Label(body, textvariable=status, style="Status.TLabel", wraplength=490).pack(fill="x", pady=(10, 12))
    buttons = ttk.Frame(body, style="App.TFrame")
    buttons.pack(side="bottom", fill="x")

    def accept(event=None):
        nonlocal result
        try:
            result = normalize_board_address(value.get())
        except ValueError as exc:
            status.set(str(exc))
            return "break"
        window.destroy()
        return "break"

    ttk.Button(buttons, text="Отмена", command=window.destroy).pack(side="left")
    ttk.Button(buttons, text="Выбрать", command=accept, style="Accent.TButton").pack(side="right")
    window.bind("<Return>", accept)
    window.bind("<Escape>", lambda event: window.destroy())
    scale_widget_tree(window)
    window.grab_set()
    entry.focus_set()
    entry.selection_range(0, "end")
    parent.wait_window(window)
    return result
