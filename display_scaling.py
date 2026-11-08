"""Process-local DPI setup and one-time pixel layout scaling.

Tk font sizes remain in points. Explicit widget distances are multiplied once
using the initial display DPI; this is not dynamic mixed-monitor DPI handling.
"""
import ctypes
import math
import re
import sys
from tkinter import TclError


def _signature(function, arguments, result):
    """Also permits small callable fakes in tests without loading Windows DLLs."""
    try:
        function.argtypes = arguments
        function.restype = result
    except (AttributeError, TypeError):
        pass
    return function


def configure_process_dpi(*, platform=None, user32=None, shcore=None):
    """Call before Tk()/any HWND; never change machine settings or thread DPI.

    Unsupported APIs allow older Windows fallbacks. Access denied means the
    host/manifest has already chosen an awareness level, which we respect.
    """
    if (sys.platform if platform is None else platform) != "win32":
        return "unsupported"
    try:
        user32 = user32 if user32 is not None else ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError):
        return "unsupported"
    modern = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if modern is not None:
        try:
            modern = _signature(modern, [ctypes.c_void_p], ctypes.c_int)
            if modern(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
                return "per-monitor-v2"
            error = ctypes.get_last_error()
            if error == 5:  # ERROR_ACCESS_DENIED: context is already fixed.
                return "already-set"
            if error not in (87, 120, 127):  # Unsupported context/API only.
                return "unchanged"
        except AttributeError:
            pass
        except OSError as exc:
            error = getattr(exc, "winerror", None)
            if error == 5:
                return "already-set"
            if error not in (87, 120, 127):
                return "unchanged"
    if shcore is None:
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
        except (AttributeError, OSError):
            shcore = None
    older = getattr(shcore, "SetProcessDpiAwareness", None) if shcore is not None else None
    if older is not None:
        try:
            older = _signature(older, [ctypes.c_int], ctypes.c_long)
            result = int(older(2)) & 0xffffffff  # PROCESS_PER_MONITOR_DPI_AWARE
            if result == 0:
                return "per-monitor"
            if result == 0x80070005:
                return "already-set"
            if result not in (0x80070057, 0x80004001):
                return "unchanged"
        except AttributeError:
            pass
        except OSError as exc:
            error = getattr(exc, "winerror", None)
            if error == 5:
                return "already-set"
            if error not in (87, 120, 127):
                return "unchanged"
    legacy = getattr(user32, "SetProcessDPIAware", None)
    if legacy is not None:
        try:
            legacy = _signature(legacy, [], ctypes.c_int)
            return "system" if legacy() else "unchanged"
        except (AttributeError, OSError):
            pass
    return "unsupported"


def ui_scale(root):
    """Return initial physical pixels per 96-DPI design pixel, within 1..4."""
    try:
        value = float(root.winfo_fpixels("1i")) / 96.0
    except (AttributeError, TypeError, ValueError, TclError):
        return 1.0
    if not math.isfinite(value):
        return 1.0
    value = min(4.0, max(1.0, value))
    nearest = round(value)
    return float(nearest) if abs(value - nearest) < .025 else value


_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _distance(value, factor, widget):
    """Scale numeric pixel distances, preserving explicit physical units."""
    if isinstance(value, (tuple, list)):
        values = value
    else:
        try:
            values = widget.tk.splitlist(value)
        except (TypeError, ValueError):
            values = (value,)
    result = []
    for item in values:
        text = str(item)
        if _NUMBER.fullmatch(text):
            result.append(round(float(text) * factor))
        else:
            result.append(item)
    if len(result) == 1:
        return result[0]
    return tuple(result)


def scale_widget_tree(window):
    """Scale explicit pixel configuration once, without scaling font points.

    Width/height of entries, labels, buttons and Text are character counts and
    are intentionally excluded. Owned Toplevels get their own map-time pass.
    """
    if getattr(window, "_neiroslopper_widgets_scaled", False):
        return
    window._neiroslopper_widgets_scaled = True
    factor = ui_scale(window)
    if factor == 1:
        return
    import tkinter as tk

    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            if child.winfo_class() != "Toplevel":
                yield from walk(child)

    pixel_sizes = {"Frame", "TFrame", "Labelframe", "TLabelframe", "Canvas"}
    for widget in walk(window):
        try:
            keys = set(widget.keys())
            names = {"padding", "wraplength", "padx", "pady"} & keys
            if widget.winfo_class() in pixel_sizes:
                names |= {"width", "height"} & keys
            changes = {}
            for name in names:
                value = widget.cget(name)
                # Empty ttk options inherit the already-scaled theme. Keep
                # them unset rather than shadowing a style with an empty tuple.
                if str(value) == "" or isinstance(value, (tuple, list)) and not value:
                    continue
                changes[name] = _distance(value, factor, widget)
            if changes:
                widget.configure(**changes)
            manager = widget.winfo_manager()
            if manager in ("pack", "grid"):
                info = widget.pack_info() if manager == "pack" else widget.grid_info()
                spacing = {key: _distance(info[key], factor, widget)
                           for key in ("padx", "pady", "ipadx", "ipady") if key in info}
                if spacing:
                    if manager == "pack":
                        widget.pack_configure(**spacing)
                    else:
                        widget.grid_configure(**spacing)
        except tk.TclError:
            # A disappearing optional widget must not prevent the window opening.
            continue


def scale_toplevel(window):
    """Scale an owned dialog's initial geometry and explicit pixels once."""
    if getattr(window, "_neiroslopper_geometry_scaled", False):
        scale_widget_tree(window)
        return
    window._neiroslopper_geometry_scaled = True
    factor = ui_scale(window)
    if factor != 1:
        import tkinter as tk
        try:
            geometry = window.geometry()
            minimum = window.minsize()
            window.minsize(*(round(value * factor) for value in minimum))
            match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geometry)
            if match:
                width, height, x, y = match.groups()
                window.geometry(f"{round(int(width) * factor)}x{round(int(height) * factor)}{x}{y}")
        except tk.TclError:
            pass
    scale_widget_tree(window)
