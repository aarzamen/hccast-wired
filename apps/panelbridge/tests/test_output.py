"""Output changes must affect only the selected virtual connector."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.models import Profile
from panelbridge.output import OutputAdapter


class OutputTests(unittest.TestCase):
    def test_configures_virtual_output_without_disabling_hdmi(self):
        calls = []
        outputs = [
            {
                "name": "HDMI-A-1",
                "enabled": True,
                "position": {"x": 0, "y": 0},
                "transform": "270",
                "scale": 1,
                "modes": [{"width": 400, "height": 1280, "current": True}],
            },
            {
                "name": "HEADLESS-1",
                "enabled": True,
                "position": {"x": 1280, "y": 0},
                "transform": "normal",
                "scale": 1,
                "modes": [{"width": 1280, "height": 720, "current": True}],
            },
        ]

        def run(args):
            calls.append(args)
            return json.dumps(outputs) if "--json" in args else ""

        adapter = OutputAdapter(run=run)
        self.assertEqual(adapter.select(), "HEADLESS-1")
        adapter.apply(Profile())
        changes = [c for c in calls if "--json" not in c]
        self.assertTrue(changes)
        for command in changes:
            self.assertNotIn("HDMI-A-1", command)
            self.assertNotIn("--off", command)
        self.assertIn("1280,0", changes[-1])
        scale_index = changes[-1].index("--scale")
        self.assertEqual(float(changes[-1][scale_index + 1]), 2.0)

    def test_missing_or_ambiguous_virtual_output_is_not_guessed(self):
        for names in (["HDMI-A-1"], ["HEADLESS-1", "HEADLESS-2"]):
            adapter = OutputAdapter(
                run=lambda _: json.dumps([{"name": n, "enabled": True} for n in names])
            )
            with self.assertRaises(RuntimeError):
                adapter.select()

    def test_select_never_accepts_a_physical_output(self):
        with self.assertRaises(ValueError):
            OutputAdapter(name="HDMI-A-1")

    def test_unverified_rotation_rejected_before_applying(self):
        calls = []
        adapter = OutputAdapter(name="HEADLESS-1", run=lambda c: calls.append(c))
        profile = Profile.from_dict(dict(Profile().to_dict(), rotation=90))
        with self.assertRaises(ValueError):
            adapter.apply(profile)
        self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
