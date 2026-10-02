"""Inspect real native calibration fixture output; no Pi/network operations.

Set PB_WFD_CALIBRATION_TEST to the built test-calibration executable.
"""
import json
import os
import subprocess
import unittest


class NativeCalibrationTests(unittest.TestCase):
    def test_real_carrier_loss_injection_and_cohort_boundaries(self):
        binary = os.environ.get("PB_WFD_CALIBRATION_TEST")
        if not binary:
            self.skipTest("PB_WFD_CALIBRATION_TEST requires native GStreamer build")
        result = subprocess.run([binary], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        cases = []
        for fixture, telemetry in zip(records[::2], records[1::2]):
            self.assertEqual(fixture["fixture"], "native-calibration-v1")
            self.assertEqual(telemetry["event"], "pipeline-telemetry")
            accounting = telemetry["local_frame_accounting"]
            self.assertTrue(accounting["closed"])
            self.assertTrue(accounting["encoder_src_eos_seen"])
            self.assertGreaterEqual(accounting["started_us"] - accounting["streaming_us"], 200_000)
            self.assertGreaterEqual(accounting["ended_us"] - accounting["started_us"], 600_000)
            self.assertGreaterEqual(accounting["closed_us"], accounting["ended_us"])
            self.assertGreater(telemetry["raw_frames_pushed"], accounting["accepted_source_frames"])
            self.assertGreater(accounting["encoded_frames"], 0)
            self.assertEqual(accounting["encoded_frames"], accounting["encoder_sink_to_src_us"]["matched"])
            self.assertIsNone(telemetry["receiver_frames_dropped"])
            self.assertIsNone(telemetry["panel_latency_us"])
            if fixture["strip"]:
                self.assertFalse(accounting["complete"])
                self.assertEqual(accounting["invalid_reason"],
                                 "wrong-cohort-epoch" if fixture["zero_epoch"] else "missing-frame-metadata")
                self.assertIsNone(accounting["pipeline_frames_dropped"])
            else:
                self.assertTrue(accounting["complete"], accounting)
                self.assertEqual(
                    accounting["accepted_source_frames"] + accounting["rate_frames_duplicated"],
                    accounting["emitted_wire_frames"] + accounting["pipeline_frames_dropped"],
                )
                self.assertEqual(
                    accounting["pipeline_frames_dropped"],
                    sum(accounting["boundary_frames_dropped"].values()) + accounting["rate_source_frames_dropped"],
                )
            cases.append((fixture, accounting))
        self.assertEqual(len(cases), 7)
        self.assertGreater(cases[1][1]["rate_frames_duplicated"], 0)
        self.assertGreater(cases[2][1]["rate_source_frames_dropped"], 0)
        self.assertGreater(cases[3][1]["boundary_frames_dropped"]["appsrc"], 0)
        self.assertEqual(cases[4][1]["boundary_frames_dropped"]["convert"], 1)


if __name__ == "__main__":
    unittest.main()
