"""Mode selection runs locally; optional GND test binary never uses networking."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


class ProfilePolicyTests(unittest.TestCase):
    def test_receiver_mode_intersection_and_bitrate_limits(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        native = Path(__file__).resolve().parent.parent / "native" / "wfd"
        with tempfile.TemporaryDirectory(prefix="panelbridge-profile-") as directory:
            binary = Path(directory) / "test-profile"
            build = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 str(native / "pb-wfd-profile.c"), str(native / "test-profile.c"),
                 "-o", str(binary)], capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gnd_advertisement_parser_and_encoder_properties(self):
        binary = os.environ.get("PB_WFD_PROFILE_TEST")
        if not binary:
            self.skipTest("Set PB_WFD_PROFILE_TEST on the GND build host")
        result = subprocess.run([binary], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
