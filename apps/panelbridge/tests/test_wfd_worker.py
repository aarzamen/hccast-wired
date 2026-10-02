"""Software-only checks for the native worker; never discover or pair a receiver.

Run on the build host with its supported distro Python:
  python3 test_wfd_worker.py --binary /absolute/path/panelbridge-wfd-worker
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest

BINARY: Path | None = Path(os.environ["PB_WFD_WORKER"]) if "PB_WFD_WORKER" in os.environ else None


def events(output: str) -> list[dict]:
    parsed = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "event" in item:
            parsed.append(item)
    return parsed


class WorkerSoftwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if BINARY is None:
            raise unittest.SkipTest("Set PB_WFD_WORKER or run with --binary on the native build host")

    def run_worker(self, *args: str, data: bytes | None = None):
        result = subprocess.run(
            [str(BINARY), *args], input=data, capture_output=True, timeout=10
        )
        output = result.stdout.decode(errors="replace")
        return result, events(output)

    def test_invalid_frame_bounds_are_rejected_before_network_access(self):
        for width in ("0", "-1", "17", "2147483647"):
            with self.subTest(width=width):
                result, records = self.run_worker(
                    "--self-test-source", "--width", width
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(records[-1]["event"], "argument-error")

    def test_source_encodes_and_decodes_real_frames(self):
        result, records = self.run_worker(
            "--self-test-source", "--source", "test", "--seconds", "1",
            "--width", "64", "--height", "48", "--fps", "10",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = [r["value"] for r in records if r["event"] == "self-test-decoded-frames"]
        self.assertEqual(len(decoded), 1)
        self.assertGreater(decoded[0], 0)

    def test_real_encoder_reports_periodic_measured_pipeline_timing(self):
        result, records = self.run_worker(
            "--self-test-source", "--source", "test", "--seconds", "3",
            "--width", "64", "--height", "48", "--fps", "10",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        samples = [r for r in records if r["event"] == "pipeline-telemetry"]
        self.assertGreaterEqual(sum(not r["final"] for r in samples), 2)
        last = samples[-1]
        self.assertTrue(last["final"])
        self.assertGreater(last["source_buffers"], 0)
        self.assertGreater(last["encoder_input_buffers"], 0)
        self.assertGreater(last["encoded_frames"], 0)
        self.assertGreater(last["encoded_bytes"], 0)
        self.assertGreater(last["encoder_sink_to_src_us"]["matched"], 0)
        self.assertGreater(last["pre_encoder_queue_residence_us"]["matched"], 0)
        self.assertGreater(last["encoder_sink_to_src_us"]["mean_us"], 0)
        self.assertLess(last["encoder_sink_to_src_us"]["mean_us"], 1_000_000)
        self.assertIsNone(last["transport_frames_dropped"])
        self.assertIsNone(last["receiver_frames_dropped"])
        self.assertIsNone(last["panel_latency_us"])
        self.assertTrue(all(a["monotonic_us"] < b["monotonic_us"]
                            for a, b in zip(samples, samples[1:])))
        self.assertTrue(all(a["encoded_buffers"] <= b["encoded_buffers"]
                            for a, b in zip(samples, samples[1:])))

    def test_continuous_mode_rejects_non_adopted_raw_and_explicit_duration(self):
        cases = (
            ("--self-test-source",),
            ("--list",),
            ("--source", "raw"),
            ("--source", "test", "--connection-uuid", "11111111-1111-4111-8111-111111111111"),
            ("--source", "raw", "--connection-uuid", "11111111-1111-4111-8111-111111111111", "--seconds", "30"),
            ("--source", "raw", "--connection-uuid", "11111111-1111-4111-8111-111111111111", "--seconds=30"),
        )
        for args in cases:
            with self.subTest(args=args):
                result, records = self.run_worker("--continuous", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(records[-1]["event"], "argument-error")
                self.assertIn(records[-1]["detail"], (
                    "continuous-and-seconds-are-exclusive",
                    "continuous-requires-adopted-raw-session",
                ))

    def test_incomplete_raw_frame_fails_without_hanging(self):
        result, records = self.run_worker(
            "--self-test-source", "--source", "raw", "--seconds", "3",
            "--width", "64", "--height", "48", "--fps", "10", data=b"\x00" * 53,
        )
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertTrue(any(r["detail"] == "raw-input-eof-or-read-error" for r in records))

    def test_calibration_bounds_reject_before_network_access(self):
        adopted = ("--source", "raw", "--connection-uuid", "11111111-1111-4111-8111-111111111111")
        cases = (
            (adopted + ("--calibration-warmup-ms", "5000"), "calibration-requires-warmup-and-duration"),
            (adopted + ("--calibration-warmup-ms", "4999", "--calibration-duration-ms", "30000"), "calibration-time-bounds"),
            (adopted + ("--calibration-warmup-ms", "5000", "--calibration-duration-ms", "29999"), "calibration-time-bounds"),
            (adopted + ("--calibration-warmup-ms", "2147483647", "--calibration-duration-ms", "2147483647"), "calibration-time-bounds"),
            (adopted + ("--calibration-warmup-ms", "5000", "--calibration-duration-ms", "30000", "--seconds", "30"), "calibration-has-its-own-bounded-duration"),
            (("--self-test-source", "--source", "raw", "--calibration-warmup-ms", "5000", "--calibration-duration-ms", "30000"), "calibration-requires-adopted-raw-session"),
        )
        for args, reason in cases:
            with self.subTest(args=args):
                result, records = self.run_worker(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(records[-1]["event"], "argument-error")
                self.assertEqual(records[-1]["detail"], reason)

    def test_fragmented_raw_frames_encode_and_decode(self):
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                [str(BINARY), "--self-test-source", "--source", "raw", "--seconds", "2",
                 "--width", "64", "--height", "48", "--fps", "10"],
                stdin=subprocess.PIPE, stdout=output, stderr=errors,
            )

            def feed():
                assert proc.stdin is not None
                frame = bytes((0, 0, 0, 255)) * (64 * 48)
                try:
                    while proc.poll() is None:
                        # Deliberately split one frame across irregular writes.
                        for begin in range(0, len(frame), 137):
                            proc.stdin.write(frame[begin:begin + 137])
                            proc.stdin.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, OSError):
                    pass

            thread = threading.Thread(target=feed, daemon=True)
            thread.start()
            try:
                code = proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
                thread.join(timeout=3)
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
            output.seek(0)
            records = events(output.read().decode(errors="replace"))
            errors.seek(0)
            self.assertEqual(code, 0, errors.read())
            self.assertGreater(next(r["value"] for r in records
                                    if r["event"] == "raw-frames-pushed"), 0)
            self.assertGreater(next(r["value"] for r in records
                                    if r["event"] == "self-test-decoded-frames"), 0)

    def test_signal_cancels_a_raw_reader_waiting_for_input(self):
        reader, writer = os.pipe()
        try:
            proc = subprocess.Popen(
                [str(BINARY), "--self-test-source", "--source", "raw", "--fd", str(reader),
                 "--seconds", "30", "--width", "64", "--height", "48"],
                pass_fds=(reader,), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.5)
            proc.send_signal(signal.SIGTERM)
            try:
                output, error = proc.communicate(timeout=3)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=3)
            self.assertEqual(proc.returncode, 130, error)
            self.assertTrue(any(r["detail"] == "signal" for r in events(output.decode())))
        finally:
            os.close(reader)
            os.close(writer)

    def test_incomplete_adoption_contract_is_rejected_before_network_access(self):
        result, records = self.run_worker(
            "--interface", "synthetic-p2p", "--peer-name", "SYNTHETIC-RECEIVER",
            "--connection-uuid", "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(records[-1]["event"], "argument-error")

    def test_invalid_profile_requests_are_rejected_before_network_access(self):
        cases = (
            ("--wire-width", "1280"),
            ("--wire-width", "1280", "--wire-height", "720", "--wire-fps", "15"),
            ("--wire-width", "0", "--wire-height", "720", "--wire-fps", "30"),
            ("--bitrate-kbps", "511"),
            ("--bitrate-kbps", "8001"),
            ("--bitrate-kbps", "0"),
        )
        for args in cases:
            with self.subTest(args=args):
                result, records = self.run_worker("--self-test-source", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(records[-1]["event"], "argument-error")

    def test_source_only_check_cannot_claim_receiver_profile_validation(self):
        result, records = self.run_worker(
            "--self-test-source", "--wire-width", "1280",
            "--wire-height", "720", "--wire-fps", "30",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(records[-1]["detail"], "wire-profile-requires-a-receiver-session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    BINARY = args.binary.resolve(strict=True)
    unittest.main(argv=[__file__, *remaining])
