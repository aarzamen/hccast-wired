"""Pure presentation decisions shared by the native UI and deterministic tests."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import math
import time
from typing import Any, Callable


PAGES = ("Connection", "Display", "Presets", "Advanced", "Recovery", "About")
STATUSES = {
    "stopped": "○ Ready", "discovering": "◇ Finding displays",
    "connecting": "◇ Connecting", "streaming": "● Streaming",
    "retrying": "◇ Reconnecting", "rescue": "◆ Recovery active", "error": "! Attention needed",
}
PRESETS = {"recommended": "Recommended", "performance": "Performance",
           "battery_saver": "Battery Saver"}
PROFILE_FIELDS = {
    "source_width": "Desktop width", "source_height": "Desktop height",
    "content_fps": "Content rate (fps)", "wire_width": "Stream width",
    "wire_height": "Stream height", "wire_fps": "Stream refresh (Hz)",
    "bitrate_kbps": "Video bitrate (kbps)", "scale": "Desktop scale",
    "rotation": "Rotation (degrees)",
}
EMPTY_COMMANDS = {"discover", "start", "stop", "confirm_profile", "revert_profile",
                  "calibrate", "restore_defaults", "start_recovery_pairing", "about"}


@dataclass(frozen=True)
class Availability:
    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class PresetView:
    title: str
    enabled: bool
    reason: str
    summary: str
    measurement: str


def decode_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("The controller returned unreadable data. Retry the connection.") from error
    if not isinstance(value, dict) or type(value.get("api_version")) is not int:
        raise ValueError("The controller returned an incompatible response.")
    if value["api_version"] != 1:
        raise ValueError("The controller uses an unsupported API version.")
    return value


def decode_result(raw: str) -> dict[str, Any]:
    value = decode_object(raw)
    if value.get("ok") is not True:
        raise ValueError(str(value.get("error") or "The controller could not complete that action."))
    result = value.get("result")
    if not isinstance(result, dict):
        raise ValueError("The controller returned an incomplete result.")
    return result


def parse_profile(fields: dict[str, Any]) -> dict[str, int | float]:
    if set(fields) != set(PROFILE_FIELDS):
        raise ValueError("A display profile needs every displayed setting.")
    parsed: dict[str, int | float] = {}
    for key, title in PROFILE_FIELDS.items():
        value = fields[key]
        if isinstance(value, bool):
            raise ValueError(f"{title} must be a number.")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{title} must be a number.") from error
        if not math.isfinite(number) or (key != "scale" and not number.is_integer()):
            raise ValueError(f"{title} must be a finite {'number' if key == 'scale' else 'whole number'}.")
        if key == "rotation":
            valid = number in (0, 90, 180, 270)
        elif key == "scale":
            valid = 0.5 <= number <= 4
        elif key.endswith("fps"):
            valid = 1 <= number <= 240
        elif key == "bitrate_kbps":
            valid = 100 <= number <= 100000
        else:
            valid = 16 <= number <= 7680
        if not valid:
            raise ValueError(f"{title} is outside the supported input range.")
        parsed[key] = number if key == "scale" else int(number)
    return parsed


def profile_summary(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "No display profile reported"
    def shown(key: str) -> str:
        return str(value[key]) if value.get(key) is not None else "?"
    return (
        f"Desktop {shown('source_width')} × {shown('source_height')} at "
        f"{shown('content_fps')} fps content\n"
        f"Stream {shown('wire_width')} × {shown('wire_height')} at {shown('wire_fps')} Hz\n"
        f"{shown('bitrate_kbps')} kbps • Scale {shown('scale')} • Rotation {shown('rotation')}°"
    )


def _number(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def about_text(result: dict[str, Any]) -> str:
    def readable(value: Any) -> str:
        if isinstance(value, list):
            return "\n\n".join(readable(item) for item in value)
        if isinstance(value, dict):
            return "\n".join(f"{key.replace('_', ' ').capitalize()}: {readable(item)}"
                             for key, item in value.items() if item is not None)
        return str(value)
    lines = []
    for key, title in (("version", "Application version"), ("credits", "Credits"),
                       ("licenses", "Licenses"), ("source", "Corresponding source"),
                       ("note", "Project notice")):
        if result.get(key) is not None:
            lines.append(f"{title}\n{readable(result[key])}")
    return "\n\n".join(lines) or "No installed license records were returned."


class Presenter:
    def __init__(self, *, preview: bool = False, clock: Callable[[], float] = time.monotonic):
        self.preview = preview
        self.clock = clock
        self.state: dict[str, Any] = {}
        self.connected = False
        self.error = ""
        self.pending: set[str] = set()
        self._trial_received = clock()

    def update(self, state: dict[str, Any]) -> None:
        try:
            if (not isinstance(state, dict) or type(state.get("api_version")) is not int
                    or state.get("api_version") != 1):
                raise ValueError("Incompatible controller state. Retry the connection.")
            if not isinstance(state.get("status"), str) or state["status"] not in STATUSES:
                raise ValueError("The controller returned an unknown connection state.")
            for key in ("features", "candidates"):
                if not isinstance(state.get(key, []), list):
                    raise ValueError("The controller returned incomplete connection state.")
            if not all(isinstance(verb, str) for verb in state.get("features", [])):
                raise ValueError("The controller returned invalid available actions.")
            for key in ("profile", "presets", "health", "recovery", "capabilities", "feature_reasons"):
                if state.get(key) is not None and not isinstance(state[key], dict):
                    raise ValueError("The controller returned incomplete display state.")
            for key in ("trial", "receiver"):
                if state.get(key) is not None and not isinstance(state[key], dict):
                    raise ValueError("The controller returned incomplete display state.")
            if state.get("trial") and _number(state["trial"].get("seconds_remaining")) is None:
                raise ValueError("The controller returned an invalid confirmation timer.")
        except ValueError as error:
            self.disconnect(str(error))
            raise
        was_connected = self.connected
        self.state = copy.deepcopy(state)
        self.connected = True
        self._trial_received = self.clock()
        if not was_connected:
            self.error = ""

    def disconnect(self, error: str) -> None:
        self.connected = False
        self.error = error
        self.pending.clear()

    def trial_remaining(self, *, now: float | None = None) -> int | None:
        trial = self.state.get("trial")
        if not isinstance(trial, dict):
            return None
        seconds = _number(trial.get("seconds_remaining"))
        if seconds is None:
            return None
        elapsed = max(0.0, (self.clock() if now is None else now) - self._trial_received)
        return max(0, math.ceil(seconds - elapsed))

    def cpu_choices(self, verb: str) -> list[int]:
        key = "cpu_caps_mhz" if verb == "set_cpu_cap" else "trial_clocks_mhz"
        values = (self.state.get("capabilities") or {}).get(key, [])
        if not isinstance(values, list):
            return []
        return sorted({value for value in values if type(value) is int and 0 < value <= 2700})

    def availability(self, verb: str, *, now: float | None = None) -> Availability:
        if self.preview:
            return Availability(False, "Preview — no hardware. Actions are disabled.")
        if not self.connected:
            return Availability(False, "Connect to the session controller first.")
        if verb not in self.state.get("features", []):
            reason = (self.state.get("feature_reasons") or {}).get(verb)
            return Availability(False, str(reason or "This operation is not available in this installation."))
        if verb in self.pending or (self.pending and verb not in {"stop", "revert_profile", "about"}):
            return Availability(False, "Waiting for the current action to finish.")
        trial = self.state.get("trial")
        if verb in {"confirm_profile", "revert_profile"}:
            if not trial:
                return Availability(False, "There is no display change to confirm.")
            if verb == "confirm_profile" and self.trial_remaining(now=now) in (None, 0):
                return Availability(False, "Confirmation expired. Waiting for automatic restoration.")
        if trial and verb in {"apply_profile", "set_cpu_cap", "trial_clock", "calibrate", "restore_defaults"}:
            return Availability(False, "Keep or revert the current change first.")
        status = self.state.get("status")
        if verb == "start":
            if status not in {"stopped", "error"}:
                return Availability(False, "A connection is already active.")
            if not self.state.get("receiver"):
                return Availability(False, "Select your display first.")
        if verb in {"discover", "select"} and status in {"connecting", "streaming", "retrying", "rescue"}:
            return Availability(False, "Disconnect before choosing another display.")
        if verb == "stop" and status == "stopped":
            return Availability(False, "The display is already disconnected.")
        if verb in {"set_cpu_cap", "trial_clock"} and not self.cpu_choices(verb):
            return Availability(False, "No validated frequencies are available for this operation.")
        return Availability(True)

    def preset(self, key: str) -> PresetView:
        raw = (self.state.get("presets") or {}).get(key) or {}
        if not isinstance(raw, dict):
            raw = {}
        gate = self.availability("apply_profile")
        available = raw.get("available") is True
        settings = raw.get("settings")
        try:
            parse_profile(settings if isinstance(settings, dict) else {})
        except ValueError:
            available = False
        reason = str(raw.get("reason") or "Run calibration to select tested settings.")
        if available:
            reason = gate.reason
        measured = raw.get("measurement")
        if isinstance(measured, dict):
            measured = measured.get("summary")
        return PresetView(PRESETS.get(key, key), available and gate.enabled, reason,
                          profile_summary(settings), str(measured or "Power and latency not measured."))

    def begin(self, verb: str, payload: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        gate = self.availability(verb)
        if not gate.enabled:
            raise ValueError(gate.reason)
        payload = dict(payload or {})
        if verb == "apply_profile":
            if set(payload) == {"preset"}:
                key = payload["preset"]
                if not isinstance(key, str) or key not in PRESETS:
                    raise ValueError("Choose an available preset.")
                chosen = self.preset(key)
                if not chosen.enabled:
                    raise ValueError(chosen.reason)
            else:
                payload = parse_profile(payload)
        elif verb == "select":
            found = next((item for item in self.state.get("candidates", [])
                          if isinstance(item, dict) and item.get("id") == payload.get("candidate_id")), None)
            if set(payload) != {"candidate_id"} or not found:
                raise ValueError("That display is no longer listed. Find displays again.")
            if "usb" in str(found.get("transport", "")).lower():
                raise ValueError("USB video is unavailable; choose a wireless display.")
        elif verb in {"set_cpu_cap", "trial_clock"}:
            if set(payload) != {"mhz"} or type(payload.get("mhz")) is not int:
                raise ValueError("Choose a validated frequency.")
            if payload["mhz"] not in self.cpu_choices(verb):
                raise ValueError("That frequency is not currently available.")
        elif verb not in EMPTY_COMMANDS or payload:
            raise ValueError("Unsupported command arguments.")
        self.error = ""
        self.pending.add(verb)
        return verb, payload

    def finish(self, verb: str, error: str | None = None) -> None:
        self.pending.discard(verb)
        if error:
            self.error = error

    def native_resolution_text(self) -> str:
        return "Native panel resolution: unknown"

    def health_text(self) -> str:
        if not self.connected:
            return "Live health unavailable. Reconnect to the desktop service."
        health = self.state.get("health") or {}
        temperature = _number(health.get("temperature_c"))
        cpu = _number(health.get("cpu_percent"))
        flags = health.get("throttled_bits")
        temp_text = "Unknown" if temperature is None else f"{temperature:g} °C"
        cpu_text = "Unknown" if cpu is None else f"{cpu:g}%"
        flag_text = "Unknown" if flags is None else ("No flags" if flags == 0 else f"Flags: {flags}")
        return f"Temperature: {temp_text}\nCPU use: {cpu_text}\nThrottling: {flag_text}"


def preview_state() -> dict[str, Any]:
    """A deliberately unmeasured, hardware-free presentation fixture."""
    return {
        "api_version": 1, "status": "stopped", "message": "Preview — no hardware",
        "receiver": {"name": "Example display", "transport": "Wi-Fi Direct",
                     "verification": "Preview only"},
        "candidates": [], "features": [],
        "profile": {"source_width": 1280, "source_height": 720, "content_fps": 15,
                    "wire_width": 1280, "wire_height": 720, "wire_fps": 30,
                    "bitrate_kbps": 3500, "scale": 1, "rotation": 0},
        "presets": {}, "trial": None,
        "health": {"temperature_c": None, "cpu_percent": None, "throttled_bits": None},
        "recovery": {"ssh_available": None, "lan_enrolled": False,
                     "message": "Preview only. No recovery service is connected."},
    }
