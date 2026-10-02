"""Validated records shared by the unprivileged controller and UI."""

from dataclasses import asdict, dataclass, fields
import math


@dataclass(frozen=True)
class Profile:
    source_width: int = 1280
    source_height: int = 720
    content_fps: int = 30
    wire_width: int = 1280
    wire_height: int = 720
    wire_fps: int = 30
    bitrate_kbps: int = 4096
    # 640x360 logical desktop at 720p: physically checked on the small panel.
    scale: float = 2.0
    rotation: int = 0

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name != "scale" and type(value) is not int:
                raise ValueError(f"{field.name} must be an integer")
        if (self.source_width, self.source_height) not in (
            (640, 360),
            (960, 540),
            (1280, 720),
            (1920, 1080),
        ):
            raise ValueError("Choose a supported 16:9 desktop resolution")
        if self.content_fps not in (5, 10, 15, 24, 30):
            raise ValueError("Content frame rate must be 5, 10, 15, 24 or 30")
        if (self.wire_width, self.wire_height, self.wire_fps) not in (
            (1280, 720, 30),
            (1920, 1080, 30),
        ):
            raise ValueError("Requested receiver mode must be 720p30 or 1080p30")
        if not 512 <= self.bitrate_kbps <= 8000:
            raise ValueError("Bitrate must be between 512 and 8000 kb/s")
        if (
            type(self.scale) not in (int, float)
            or not math.isfinite(self.scale)
            or self.scale not in (1, 1.25, 1.5, 2)
        ):
            raise ValueError("Scale must be 1, 1.25, 1.5 or 2")
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError("Rotation must be 0, 90, 180 or 270 degrees")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("A complete profile with only supported fields is required")
        return cls(**value)

    def to_dict(self):
        return asdict(self)

    def worker_arguments(self):
        return [
            "--width",
            str(self.source_width),
            "--height",
            str(self.source_height),
            "--fps",
            str(self.content_fps),
            "--wire-width",
            str(self.wire_width),
            "--wire-height",
            str(self.wire_height),
            "--wire-fps",
            str(self.wire_fps),
            "--bitrate-kbps",
            str(self.bitrate_kbps),
        ]
