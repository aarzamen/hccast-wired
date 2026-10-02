"""Stock wlroots output management, restricted to a virtual connector."""

import json
import re
import subprocess

from .models import Profile


class OutputAdapter:
    def __init__(self, name=None, run=None):
        if name and not re.fullmatch(r"(?:HEADLESS|NOOP)-[0-9]+", name):
            raise ValueError("A virtual output name is required")
        self.name = name
        self.run = run or self._run

    @staticmethod
    def _run(args):
        result = subprocess.run(args, text=True, capture_output=True, timeout=8)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Display configuration failed")
        return result.stdout

    def outputs(self):
        data = json.loads(self.run(["/usr/bin/wlr-randr", "--json"]))
        if not isinstance(data, list):
            raise RuntimeError("Invalid compositor output response")
        return data

    def select(self):
        outputs = self.outputs()
        if self.name:
            if not any(o["name"] == self.name for o in outputs):
                raise RuntimeError("Configured virtual output is missing")
            return self.name
        candidates = [o for o in outputs if re.fullmatch(r"HEADLESS-[0-9]+", o["name"])]
        if not candidates:
            candidates = [o for o in outputs if re.fullmatch(r"NOOP-[0-9]+", o["name"])]
        if len(candidates) != 1:
            raise RuntimeError(
                "One app virtual output is required; restart the desktop after installation"
            )
        self.name = candidates[0]["name"]
        return self.name

    @staticmethod
    def _right_edge(output):
        mode = next((m for m in output.get("modes", []) if m.get("current")), None)
        if not mode:
            return 0
        width = (
            mode["height"]
            if output.get("transform") in ("90", "270", "flipped-90", "flipped-270")
            else mode["width"]
        )
        return output.get("position", {}).get("x", 0) + int(width / output.get("scale", 1))

    def apply(self, profile: Profile):
        # Rotated raw capture has not been verified; reject rather than stretch.
        if profile.rotation != 0:
            raise ValueError("Rotation is unavailable until rotated capture is verified")
        name = self.select()
        outputs = self.outputs()
        physical = [o for o in outputs if o.get("enabled") and o["name"] != name]
        x = max((self._right_edge(o) for o in physical), default=0)
        self.run(
            [
                "/usr/bin/wlr-randr",
                "--output",
                name,
                "--on",
                "--custom-mode",
                f"{profile.source_width}x{profile.source_height}@30Hz",
                "--scale",
                str(profile.scale),
                "--transform",
                "normal",
                "--pos",
                f"{x},0",
            ]
        )
        return name

    def capture_arguments(self, profile, fd):
        name = self.select()
        if type(fd) is not int or fd < 0:
            raise ValueError("Inherited capture descriptor required")
        return [
            "/usr/bin/wf-recorder",
            "-y",
            "--no-dmabuf",
            "-D",
            "-o",
            name,
            "-r",
            str(profile.content_fps),
            "-c",
            "rawvideo",
            "-x",
            "bgr0",
            "-m",
            "rawvideo",
            "-f",
            f"/dev/fd/{fd}",
        ]
