"""Rasterize the original SVG with distro librsvg; run during packaging only."""

from pathlib import Path
import gi
import cairo

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402 (select the GI version before import)


def main():
    root = Path(__file__).resolve().parents[1] / "assets/icons/hicolor"
    source = root / "scalable/apps/org.panelbridge.PanelBridge.svg"
    handle = Rsvg.Handle.new_from_file(str(source))
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        rect = Rsvg.Rectangle()
        rect.x = rect.y = 0
        rect.width = rect.height = size
        handle.render_document(cairo.Context(surface), rect)
        target = root / f"{size}x{size}/apps/org.panelbridge.PanelBridge.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        surface.write_to_png(str(target))
        surface.finish()
    print("Rendered original icon at eight installed sizes")


if __name__ == "__main__":
    main()
