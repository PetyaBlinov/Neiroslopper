"""Sketch-only assistant adapted from the team DWIN assistant.

Shared geometry helpers are aliases, preserving Plan/scene identity with the
manual editor and saved projects rather than maintaining duplicate classes.
"""
from importlib import import_module
import sys

_SHARED = ('drawing_tools', 'free_preview', 'path_metrics', 'planner', 'route_opt', 'scene', 'text_block', 'text_paths')
for _name in _SHARED:
    _module = import_module('editor_tools.' + _name)
    sys.modules[__name__ + '.' + _name] = _module
    globals()[_name] = _module
