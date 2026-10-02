"""Portable semantic tests for continuous admission and bounded timing state."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RuntimePolicyTests(unittest.TestCase):
    def test_continuous_admission_and_timestamp_correlation(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        native = Path(__file__).resolve().parent.parent / "native" / "wfd"
        with tempfile.TemporaryDirectory(prefix="panelbridge-runtime-") as directory:
            binary = Path(directory) / "test-runtime"
            build = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 str(native / "pb-wfd-runtime.c"), str(native / "test-runtime.c"),
                 "-o", str(binary)], capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
