"""Read-only Pi health samples; unavailable measurements stay unknown."""

from pathlib import Path
import re
import subprocess
import time


class HealthMonitor:
    def __init__(self, *, read=None, throttle=None, clock_ns=time.monotonic_ns):
        self.read = read or (lambda path: Path(path).read_text())
        self.throttle = throttle or self._throttle
        self.previous_cpu = None
        self.clock_ns = clock_ns

    @staticmethod
    def _throttle():
        return subprocess.run(
            ["/usr/bin/vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout

    def sample(self):
        result = self.sample_timed()
        return {key: result[key] for key in ("temperature_c", "cpu_percent", "throttled_bits")}

    def sample_timed(self):
        """Producer timestamps for calibration; command/IPC delay cannot retime CPU.

        CPU counters are timestamped immediately after /proc/stat is read.
        The health timestamp follows the last sensor read. The optional CPU
        interval is therefore independent of this later health timestamp.
        """
        result = {"temperature_c": None, "cpu_percent": None, "throttled_bits": None}
        result["cpu_interval"] = None
        try:
            temperature = int(self.read("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000
            if -20 <= temperature <= 150:
                result["temperature_c"] = temperature
        except (OSError, ValueError):
            pass
        try:
            raw = self.read("/proc/stat")
            counter_us = self.clock_ns() // 1000
            fields = raw.splitlines()[0].split()
            if fields[0] != "cpu":
                raise ValueError("CPU counters unavailable")
            values = [int(x) for x in fields[1:9]]
            if len(values) < 5 or min(values) < 0:
                raise ValueError("Invalid CPU counters")
            total = sum(values)
            idle = values[3] + values[4]
            if self.previous_cpu:
                delta = total - self.previous_cpu[0]
                idle_delta = idle - self.previous_cpu[1]
                if delta > 0 and 0 <= idle_delta <= delta and counter_us > self.previous_cpu[2]:
                    result["cpu_percent"] = round(100 * (1 - idle_delta / delta), 1)
                    result["cpu_interval"] = {"start_us": self.previous_cpu[2], "end_us": counter_us}
            self.previous_cpu = (total, idle, counter_us)
        except (OSError, ValueError, IndexError):
            self.previous_cpu = None
        try:
            match = re.fullmatch(r"throttled=0x([0-9a-fA-F]{1,8})\s*", self.throttle())
            if match:
                result["throttled_bits"] = int(match.group(1), 16)
        except (OSError, subprocess.SubprocessError):
            pass
        result["monotonic_us"] = self.clock_ns() // 1000
        return result


def safety_action(health):
    if not isinstance(health, dict):
        return "unknown"
    temperature = health.get("temperature_c")
    bits = health.get("throttled_bits")
    valid_temperature = type(temperature) in (int, float) and -20 <= temperature <= 150
    # Current flags are bits 0..3; history is bits 16..19. New/unknown
    # firmware flags require diagnosis, never an implicit healthy result.
    # https://www.raspberrypi.com/documentation/computers/os.html#get_throttled
    valid_bits = type(bits) is int and 0 <= bits <= 0xFFFFFFFF and not bits & ~0xF000F
    if (valid_bits and bits & 0xF) or (valid_temperature and temperature >= 75):
        return "stop"
    if not valid_temperature or not valid_bits:
        return "unknown"
    if temperature >= 70:
        return "backoff"
    return "ok"
