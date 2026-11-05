"""Experimental wired driver for HCCAST/DrongScreen/ElfCast displays.

``__version__`` below is the single version source for the legacy driver:
``pyproject.toml`` reads it at build time and ``hccast-wired --version``
reports it. It is the driver's version only. A receiver's firmware version is
whatever the screen reports in its SETV reply, and the optional PanelBridge
application under ``apps/panelbridge`` is versioned separately.
"""

from .protocol import (
    Command,
    DeviceInfo,
    Frame,
    FrameCodec,
    FrameStreamParser,
    ScreenInfo,
    Settings,
)

__all__ = [
    "Command",
    "DeviceInfo",
    "Frame",
    "FrameCodec",
    "FrameStreamParser",
    "ScreenInfo",
    "Settings",
]

__version__ = "0.2.0"
