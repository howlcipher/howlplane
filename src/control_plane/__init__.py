"""Backward-compatibility package shim redirecting to howlplane.control_plane."""

from howlplane import control_plane as _cp

__path__ = list(_cp.__path__)
__doc__ = _cp.__doc__
