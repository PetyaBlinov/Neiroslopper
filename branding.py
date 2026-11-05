"""Neiroslopper identity and the user-supplied application icon."""
import sys
from pathlib import Path

APP_NAME = 'Neiroslopper'
APP_VERSION = '0.2.0'
APP_TITLE = f'{APP_NAME} {APP_VERSION} · Плоттер'
ASSETS = Path(__file__).resolve().parent / 'assets'


def _set_windows_window_icons(window):
    """Set WM icons on the mapped Windows frame, not only Tk's default."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                     wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        hwnd = user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT: native outer frame.
        handles = []
        for kind, metric in ((0, 49), (1, 11)):  # SMALL/CXSMICON, BIG/CXICON
            size = max(16, user32.GetSystemMetrics(metric))
            handle = user32.LoadImageW(None, str(ASSETS / 'icon.ico'), 1, size, size, 0x10)
            if handle:
                user32.SendMessageW(hwnd, 0x80, kind, handle)  # WM_SETICON
                handles.append(handle)
        old_handles = getattr(window, '_neiroslopper_win_icons', ())
        window._neiroslopper_win_icons = tuple(handles)
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        for handle in old_handles:
            user32.DestroyIcon(handle)
    except (AttributeError, OSError, RuntimeError):
        pass


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
            window.after_idle(lambda: _set_windows_window_icons(window))
            window.bind('<Map>', lambda event: _set_windows_window_icons(window)
                        if event.widget is window else None, add='+')
        else:
            image = tk.PhotoImage(master=window, file=str(ASSETS / 'icon.png'))
            window.iconphoto(True, image)
            window._neiroslopper_icon = image
        return True
    except (OSError, tk.TclError):
        # A missing optional resource must not prevent the machine UI opening.
        return False
