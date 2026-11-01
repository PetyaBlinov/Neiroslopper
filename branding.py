"""Neiroslopper identity and the user-supplied application icon."""
import sys
from pathlib import Path

APP_NAME = 'Neiroslopper'
APP_TITLE = 'Neiroslopper · Плоттер'
ASSETS = Path(__file__).resolve().parent / 'assets'


def configure_windows_identity():
    """Give this process its own taskbar grouping, without system-wide changes."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Neiroslopper.FluidNC.Desktop')
    except (AttributeError, OSError):
        pass


def apply_app_icon(window):
    """Apply to this root and future Toplevel windows; keep the Tk image alive."""
    import tkinter as tk
    try:
        if sys.platform == 'win32':
            icon = str(ASSETS / 'icon.ico')
            window.iconbitmap(default=icon)
            # -default supplies future windows; set this root explicitly too.
            window.iconbitmap(icon)
        else:
            image = tk.PhotoImage(master=window, file=str(ASSETS / 'icon.png'))
            window.iconphoto(True, image)
            window._neiroslopper_icon = image
        return True
    except (OSError, tk.TclError):
        # A missing optional resource must not prevent the machine UI opening.
        return False
