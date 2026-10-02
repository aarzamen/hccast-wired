from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.health import HealthMonitor, safety_action


def test_cpu_delta_temperature_and_throttle_bits_are_real_readings():
    data = {
        "/proc/stat": "cpu 10 0 10 80 0 0 0 0 0 0\n",
        "/sys/class/thermal/thermal_zone0/temp": "51250\n",
    }
    monitor = HealthMonitor(read=lambda path: data[path], throttle=lambda: "throttled=0x0\n")
    first = monitor.sample()
    assert first["cpu_percent"] is None and first["temperature_c"] == 51.25
    data["/proc/stat"] = "cpu 20 0 20 100 0 0 0 0 0 0\n"
    assert monitor.sample()["cpu_percent"] == 50


def test_missing_sensors_are_unknown_not_zero():
    def missing(*_):
        raise OSError("missing")

    sample = HealthMonitor(read=missing, throttle=missing).sample()
    assert all(sample[k] is None for k in ("cpu_percent", "temperature_c", "throttled_bits"))
    assert safety_action(sample) == "unknown"


def test_active_fault_stops_history_is_not_current_and_heat_backs_off():
    assert safety_action({"temperature_c": 55, "throttled_bits": 0x50000}) == "ok"
    assert safety_action({"temperature_c": 55, "throttled_bits": 1}) == "stop"
    assert safety_action({"temperature_c": 70, "throttled_bits": 0}) == "backoff"
    assert safety_action({"temperature_c": 75, "throttled_bits": 0}) == "stop"


@pytest.mark.parametrize("bits", [1, 2, 4, 8, 0xF, 0xF000F])
def test_every_active_supply_frequency_or_temperature_flag_stops(bits):
    assert safety_action({"temperature_c": 55, "throttled_bits": bits}) == "stop"


@pytest.mark.parametrize("bits", [0x10000, 0x20000, 0x40000, 0x80000, 0xF0000])
def test_history_only_is_not_an_active_fault(bits):
    assert safety_action({"temperature_c": 55, "throttled_bits": bits}) == "ok"


@pytest.mark.parametrize("bits", [True, -1, 0.0, "0", 0x10, 0x100000, 0x100000000])
def test_malformed_or_unrecognized_flags_fail_closed(bits):
    assert safety_action({"temperature_c": 55, "throttled_bits": bits}) == "unknown"


@pytest.mark.parametrize("temperature", [True, "55", float("nan"), float("inf"), -21, 151])
def test_malformed_or_out_of_range_temperature_fails_closed(temperature):
    assert safety_action({"temperature_c": temperature, "throttled_bits": 0}) == "unknown"


@pytest.mark.parametrize("health", [None, [], "healthy", 1])
def test_nonobject_health_fails_closed(health):
    assert safety_action(health) == "unknown"


def test_known_emergency_is_preserved_when_the_other_sensor_is_missing():
    assert safety_action({"temperature_c": 76, "throttled_bits": None}) == "stop"
    assert safety_action({"temperature_c": None, "throttled_bits": 1}) == "stop"


def test_timed_cpu_intervals_follow_counter_reads_not_later_sensor_latency():
    now = [1_000_000_000]
    counters = ["cpu 10 0 10 80 0 0 0 0\n"]

    def read(path):
        if path == "/proc/stat":
            now[0] += 10_000
            return counters[0]
        return "55000"

    def throttle():
        now[0] += 500_000_000  # Delayed command completion must not retime CPU.
        return "throttled=0x0"

    monitor = HealthMonitor(read=read, throttle=throttle, clock_ns=lambda: now[0])
    first = monitor.sample_timed()
    assert first["cpu_percent"] is None and first["cpu_interval"] is None
    counters[0] = "cpu 30 0 30 140 0 0 0 0\n"
    now[0] = 3_000_000_000
    second = monitor.sample_timed()
    assert second["cpu_percent"] == 40
    assert second["cpu_interval"] == {"start_us": 1_000_010, "end_us": 3_000_010}
    assert second["monotonic_us"] == 3_500_010
    assert set(monitor.sample()) == {"cpu_percent", "temperature_c", "throttled_bits"}


def test_failed_counter_read_clears_timed_cpu_baseline():
    now = [1_000_000_000]
    failed = [False]

    def read(path):
        if path == "/proc/stat":
            if failed[0]:
                raise OSError("counter temporarily unavailable")
            return f"cpu {now[0] // 1_000_000} 0 0 100 0 0 0 0\n"
        return "55000"

    monitor = HealthMonitor(read=read, throttle=lambda: "throttled=0x0", clock_ns=lambda: now[0])
    monitor.sample_timed()
    failed[0] = True
    assert monitor.sample_timed()["cpu_interval"] is None
    failed[0] = False
    now[0] += 1_000_000_000
    recovered = monitor.sample_timed()
    assert recovered["cpu_percent"] is None and recovered["cpu_interval"] is None
