from __future__ import annotations

import builtins
import time
from dataclasses import dataclass
from typing import Any

import pytest

from hccast_wired import cli, host_usb
from hccast_wired.host_usb import CANDIDATE_IDS, HostUSBTransport
from hccast_wired.transport import TransportError


@dataclass
class _FakeDevice:
    idVendor: int = 0x1CBE
    idProduct: int = 0x0005


class _FakeUtil:
    pass


def test_missing_pyusb_explains_uv_host_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def missing_usb(name: str, *args: object, **kwargs: object) -> object:
        if name == "usb.core":
            raise ImportError("PyUSB unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_usb)

    with pytest.raises(TransportError) as raised:
        host_usb._load_usb()

    message = str(raised.value)
    assert "uv sync --extra host" in message
    assert "installed distributions" in message
    assert "optional `host` extra" in message
    package_tool = "p" + "ip"
    assert "python -m " + package_tool + " install" not in message


@dataclass
class _Endpoint:
    bEndpointAddress: int
    bmAttributes: int = 2
    wMaxPacketSize: int = 512


class _Interface(list[_Endpoint]):
    def __init__(self, *endpoints: _Endpoint, number: int = 0) -> None:
        super().__init__(endpoints)
        self.bInterfaceNumber = number


class _OpenDevice(_FakeDevice):
    def __init__(
        self,
        configurations: list[Any],
        *,
        set_error: Exception | None = None,
        kernel_driver_active: bool = False,
        driver_check_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.configurations = configurations
        self.set_error = set_error
        self.kernel_driver_active = kernel_driver_active
        self.driver_check_error = driver_check_error
        self.calls: list[str] = []

    def get_active_configuration(self) -> Any:
        self.calls.append("get_active_configuration")
        result = self.configurations.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def set_configuration(self) -> None:
        self.calls.append("set_configuration")
        if self.set_error is not None:
            raise self.set_error

    def is_kernel_driver_active(self, interface_number: int) -> bool:
        self.calls.append(f"is_kernel_driver_active:{interface_number}")
        if self.driver_check_error is not None:
            raise self.driver_check_error
        return self.kernel_driver_active

    def detach_kernel_driver(self, interface_number: int) -> None:
        self.calls.append(f"detach_kernel_driver:{interface_number}")

    def attach_kernel_driver(self, interface_number: int) -> None:
        self.calls.append(f"attach_kernel_driver:{interface_number}")


class _OpenUtil:
    ENDPOINT_TYPE_BULK = 2
    ENDPOINT_IN = 0x80

    def __init__(self, *, claim_error: Exception | None = None) -> None:
        self.claim_error = claim_error
        self.calls: list[str] = []

    def endpoint_type(self, attributes: int) -> int:
        return attributes & 0x03

    def endpoint_direction(self, address: int) -> int:
        return address & 0x80

    def claim_interface(self, dev: _OpenDevice, interface_number: int) -> None:
        self.calls.append(f"claim_interface:{interface_number}")
        if self.claim_error is not None:
            raise self.claim_error

    def release_interface(self, dev: _OpenDevice, interface_number: int) -> None:
        self.calls.append(f"release_interface:{interface_number}")

    def dispose_resources(self, dev: _OpenDevice) -> None:
        self.calls.append("dispose_resources")


class _ExplicitCore:
    def __init__(self, device: _FakeDevice, misses: int) -> None:
        self.device = device
        self.misses = misses
        self.calls: list[tuple[int, int]] = []

    def find(self, *, idVendor: int, idProduct: int) -> _FakeDevice | None:
        self.calls.append((idVendor, idProduct))
        if len(self.calls) <= self.misses:
            return None
        return self.device


class _DefaultCore:
    def __init__(self, device: _FakeDevice, available_round: int) -> None:
        self.device = device
        self.available_round = available_round
        self.calls: list[tuple[int, int]] = []
        self.round = 0

    def find(self, *, idVendor: int, idProduct: int) -> _FakeDevice | None:
        pair = (idVendor, idProduct)
        self.calls.append(pair)
        if pair == CANDIDATE_IDS[0]:
            self.round += 1
        is_target = pair == (self.device.idVendor, self.device.idProduct)
        if is_target and self.round >= self.available_round:
            return self.device
        return None


class _FakeClock:
    def __init__(self) -> None:
        self.now = 10.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


def _patch_discovery_only(
    monkeypatch: pytest.MonkeyPatch,
    core: Any,
    clock: _FakeClock,
) -> None:
    monkeypatch.setattr(host_usb, "_load_usb", lambda: (core, _FakeUtil()))
    monkeypatch.setattr(host_usb.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(host_usb.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        HostUSBTransport,
        "_open",
        lambda self: setattr(self, "_dev", self._find_device()),
    )


def _bulk_configuration() -> list[_Interface]:
    return [_Interface(_Endpoint(0x81), _Endpoint(0x02))]


def _patch_open_device(
    monkeypatch: pytest.MonkeyPatch,
    device: _OpenDevice,
    util: _OpenUtil,
) -> None:
    monkeypatch.setattr(
        host_usb,
        "_load_usb",
        lambda: (_ExplicitCore(device, misses=0), util),
    )


def test_hardware_observed_candidate_id_is_present() -> None:
    assert (0x1CBE, 0x0005) in CANDIDATE_IDS


def test_one_shot_miss_does_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _FakeDevice()
    core = _ExplicitCore(device, misses=1)
    clock = _FakeClock()
    _patch_discovery_only(monkeypatch, core, clock)

    with pytest.raises(TransportError, match="1cbe:0005 not found"):
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert core.calls == [(0x1CBE, 0x0005)]
    assert clock.sleeps == []


@pytest.mark.parametrize("explicit", [False, True])
def test_discovery_polls_until_transient_device_appears(
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    device = _FakeDevice()
    core: _ExplicitCore | _DefaultCore
    if explicit:
        core = _ExplicitCore(device, misses=2)
    else:
        core = _DefaultCore(device, available_round=2)
    clock = _FakeClock()
    _patch_discovery_only(monkeypatch, core, clock)

    kwargs = {"vendor_id": 0x1CBE, "product_id": 0x0005} if explicit else {}
    transport = HostUSBTransport(
        **kwargs,
        wait_seconds=0.5,
        poll_interval=0.02,
    )

    assert transport._dev is device
    if explicit:
        assert core.calls == [(0x1CBE, 0x0005)] * 3
        assert clock.sleeps == [0.02, 0.02]
    else:
        assert core.calls == list(CANDIDATE_IDS) * 2
        assert clock.sleeps == [0.02]


def test_discovery_wait_stops_at_monotonic_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _ExplicitCore(_FakeDevice(), misses=100)
    clock = _FakeClock()
    _patch_discovery_only(monkeypatch, core, clock)

    with pytest.raises(TransportError, match="1cbe:0005 not found"):
        HostUSBTransport(
            vendor_id=0x1CBE,
            product_id=0x0005,
            wait_seconds=0.05,
            poll_interval=0.02,
        )

    assert sum(clock.sleeps) == pytest.approx(0.05)
    assert max(clock.sleeps) <= 0.02


def test_open_uses_existing_active_configuration_without_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([_bulk_configuration()])
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    transport = HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)
    transport.close()

    assert device.calls[:2] == [
        "get_active_configuration",
        "is_kernel_driver_active:0",
    ]
    assert "set_configuration" not in device.calls
    assert util.calls == [
        "claim_interface:0",
        "release_interface:0",
        "dispose_resources",
    ]


def test_open_activates_configuration_only_after_initial_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice(
        [RuntimeError("not configured"), _bulk_configuration()],
    )
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    transport = HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)
    transport.close()

    assert device.calls[:4] == [
        "get_active_configuration",
        "set_configuration",
        "get_active_configuration",
        "is_kernel_driver_active:0",
    ]


def test_claim_only_fails_closed_without_configuration_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([RuntimeError("not configured")])
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError) as raised:
        HostUSBTransport(
            vendor_id=0x1CBE,
            product_id=0x0005,
            allow_configuration_activation=False,
        )

    message = str(raised.value)
    assert "claim-only safety" in message
    assert "configuration activation is disabled" in message
    assert "not configured" in message
    assert "set_configuration" not in device.calls
    assert util.calls == ["dispose_resources"]


def test_configuration_activation_failure_disposes_and_preserves_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice(
        [RuntimeError("not configured")],
        set_error=RuntimeError("activation denied"),
    )
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError) as raised:
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert "not configured" in str(raised.value)
    assert "activation denied" in str(raised.value)
    assert util.calls == ["dispose_resources"]


def test_configuration_retry_failure_disposes_and_preserves_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice(
        [RuntimeError("not configured"), RuntimeError("descriptor unavailable")],
    )
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError) as raised:
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert "not configured" in str(raised.value)
    assert "descriptor unavailable" in str(raised.value)
    assert util.calls == ["dispose_resources"]


def test_missing_bulk_endpoints_disposes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([[_Interface(_Endpoint(0x81))]])
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError, match="both bulk IN and bulk OUT"):
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert util.calls == ["dispose_resources"]


def test_kernel_driver_check_failure_disposes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice(
        [_bulk_configuration()],
        driver_check_error=RuntimeError("driver state unavailable"),
    )
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(RuntimeError, match="driver state unavailable"):
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert util.calls == ["dispose_resources"]


def test_claim_failure_reattaches_detached_driver_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([_bulk_configuration()], kernel_driver_active=True)
    util = _OpenUtil(claim_error=RuntimeError("claim denied"))
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError, match="cannot claim USB interface 0"):
        HostUSBTransport(
            vendor_id=0x1CBE,
            product_id=0x0005,
            detach_kernel=True,
        )

    assert device.calls[-2:] == [
        "detach_kernel_driver:0",
        "attach_kernel_driver:0",
    ]
    assert util.calls == ["claim_interface:0", "dispose_resources"]


def test_active_driver_default_stops_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([_bulk_configuration()], kernel_driver_active=True)
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError, match="kernel driver owns interface 0"):
        HostUSBTransport(vendor_id=0x1CBE, product_id=0x0005)

    assert "detach_kernel_driver:0" not in device.calls
    assert util.calls == ["dispose_resources"]


def test_active_driver_claim_only_succeeds_without_detach_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _OpenDevice([_bulk_configuration()], kernel_driver_active=True)
    util = _OpenUtil()
    _patch_open_device(monkeypatch, device, util)

    with caplog.at_level("WARNING"):
        transport = HostUSBTransport(
            vendor_id=0x1CBE,
            product_id=0x0005,
            try_claim_with_kernel_driver=True,
        )
    transport.close()

    assert "detach_kernel_driver:0" not in device.calls
    assert "attach_kernel_driver:0" not in device.calls
    assert util.calls == [
        "claim_interface:0",
        "release_interface:0",
        "dispose_resources",
    ]
    assert "non-detaching claim" in caplog.text


def test_active_driver_claim_only_failure_has_context_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _OpenDevice([_bulk_configuration()], kernel_driver_active=True)
    util = _OpenUtil(claim_error=RuntimeError("claim denied"))
    _patch_open_device(monkeypatch, device, util)

    with pytest.raises(TransportError) as raised:
        HostUSBTransport(
            vendor_id=0x1CBE,
            product_id=0x0005,
            try_claim_with_kernel_driver=True,
        )

    message = str(raised.value)
    assert "interface 0" in message
    assert "non-detaching claim" in message
    assert "claim denied" in message
    assert "detach_kernel_driver:0" not in device.calls
    assert util.calls == ["claim_interface:0", "dispose_resources"]


def test_detach_and_claim_only_combination_rejected_before_loading_usb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_usb,
        "_load_usb",
        lambda: pytest.fail("contradictory options should fail before loading PyUSB"),
    )

    with pytest.raises(ValueError, match="detach_kernel.*try_claim_with_kernel_driver"):
        HostUSBTransport(
            detach_kernel=True,
            try_claim_with_kernel_driver=True,
        )


@pytest.mark.parametrize(
    ("wait_seconds", "poll_interval", "message"),
    [
        (-0.01, 0.02, "wait_seconds must be non-negative"),
        (0.0, 0.0, "poll_interval must be positive"),
        (0.0, -0.01, "poll_interval must be positive"),
        (float("nan"), 0.02, "wait_seconds must be finite"),
        (float("inf"), 0.02, "wait_seconds must be finite"),
        (float("-inf"), 0.02, "wait_seconds must be finite"),
        (0.0, float("nan"), "poll_interval must be finite"),
        (0.0, float("inf"), "poll_interval must be finite"),
        (0.0, float("-inf"), "poll_interval must be finite"),
    ],
)
def test_invalid_wait_configuration_is_rejected_before_loading_usb(
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: float,
    poll_interval: float,
    message: str,
) -> None:
    monkeypatch.setattr(
        host_usb,
        "_load_usb",
        lambda: pytest.fail("invalid options should fail before loading PyUSB"),
    )

    with pytest.raises(ValueError, match=message):
        HostUSBTransport(wait_seconds=wait_seconds, poll_interval=poll_interval)


@pytest.mark.parametrize(
    ("command", "extra_args", "stream"),
    [
        ("host-handshake", [], False),
        ("host-stream", ["sample.h264"], True),
    ],
)
def test_cli_passes_wait_and_claim_options_to_host_transport(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra_args: list[str],
    stream: bool,
) -> None:
    captured: dict[str, Any] = {}

    class _Transport:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    def fake_run_session(transport: object, args: object, *, stream: bool) -> int:
        captured["transport"] = transport
        captured["stream"] = stream
        return 0

    monkeypatch.setattr(cli, "HostUSBTransport", _Transport)
    monkeypatch.setattr(cli, "_run_session", fake_run_session)
    args = cli.build_parser().parse_args(
        [
            command,
            *extra_args,
            "--wait-seconds",
            "1.25",
            "--poll-interval",
            "0.015",
            "--try-claim-with-kernel-driver",
        ]
    )

    assert args.func(args) == 0
    assert captured["wait_seconds"] == 1.25
    assert captured["poll_interval"] == 0.015
    assert captured["try_claim_with_kernel_driver"] is True
    assert captured["stream"] is stream


def test_host_claim_passes_options_closes_and_never_creates_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    sleep_calls: list[float] = []

    class _Transport:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["transport"] = self
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli, "HostUSBTransport", _Transport)
    monkeypatch.setattr(time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        cli,
        "HCCASTSession",
        lambda transport: pytest.fail("host-claim must not construct HCCASTSession"),
    )
    args = cli.build_parser().parse_args(
        [
            "host-claim",
            "--vendor-id",
            "0x1cbe",
            "--product-id",
            "0x0005",
            "--interface",
            "3",
            "--wait-seconds",
            "1.5",
            "--poll-interval",
            "0.01",
            "--try-claim-with-kernel-driver",
        ]
    )

    assert args.func(args) == 0
    assert captured["vendor_id"] == 0x1CBE
    assert captured["product_id"] == 0x0005
    assert captured["interface_number"] == 3
    assert captured["wait_seconds"] == 1.5
    assert captured["poll_interval"] == 0.01
    assert captured["try_claim_with_kernel_driver"] is True
    assert captured["detach_kernel"] is False
    assert captured["allow_configuration_activation"] is False
    assert args.hold_seconds == 0.0
    assert sleep_calls == []
    assert captured["transport"].closed is True
    output = capsys.readouterr().out
    assert "claim succeeded" in output
    assert "no HCCAST/application bulk-endpoint payload I/O" in output
    assert "no configuration activation" in output


def test_host_claim_holds_after_claim_and_before_release_without_claiming_survival(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    events: list[str] = []

    class _Transport:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            events.append("claimed")

        def close(self) -> None:
            events.append("released")

    monkeypatch.setattr(cli, "HostUSBTransport", _Transport)
    monkeypatch.setattr(
        cli,
        "HCCASTSession",
        lambda transport: pytest.fail("host-claim must not construct HCCASTSession"),
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: events.append(f"slept:{seconds}"))
    args = cli.build_parser().parse_args(
        [
            "host-claim",
            "--hold-seconds",
            "2",
            "--try-claim-with-kernel-driver",
        ]
    )

    assert args.func(args) == 0
    assert events == ["claimed", "slept:2.0", "released"]
    assert captured["detach_kernel"] is False
    assert captured["allow_configuration_activation"] is False
    output = capsys.readouterr().out
    assert "requested 2.000-second observation window elapsed" in output.lower()
    assert "does not establish that the device remained attached" in output.lower()
    assert "no HCCAST/application bulk-endpoint payload I/O" in output


def test_host_claim_closes_when_hold_sleep_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Transport:
        closed = False

        def close(self) -> None:
            self.closed = True

    transport = _Transport()
    monkeypatch.setattr(cli, "HostUSBTransport", lambda **kwargs: transport)
    monkeypatch.setattr(
        time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(RuntimeError("hold interrupted")),
    )
    args = cli.build_parser().parse_args(["host-claim", "--hold-seconds", "2"])

    with pytest.raises(RuntimeError, match="hold interrupted"):
        args.func(args)

    assert transport.closed is True


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf", "10.000001"])
def test_host_claim_rejects_invalid_hold_before_usb_access(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accessed = False

    def forbidden_transport(**kwargs: Any) -> object:
        nonlocal accessed
        accessed = True
        pytest.fail("invalid hold must fail before USB transport construction")

    monkeypatch.setattr(cli, "HostUSBTransport", forbidden_transport)

    with pytest.raises(SystemExit) as raised:
        cli.main(["host-claim", f"--hold-seconds={value}"])

    assert raised.value.code == 2
    assert accessed is False
    error = capsys.readouterr().err
    assert "--hold-seconds" in error
    assert "must" in error
    assert "unrecognized arguments" not in error


def test_host_claim_accepts_maximum_hold() -> None:
    args = cli.build_parser().parse_args(["host-claim", "--hold-seconds", "10"])
    assert args.hold_seconds == 10.0


def test_host_claim_closes_if_success_reporting_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Transport:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    transport = _Transport()
    monkeypatch.setattr(cli, "HostUSBTransport", lambda **kwargs: transport)
    monkeypatch.setattr(
        builtins,
        "print",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("output failed")),
    )
    args = cli.build_parser().parse_args(["host-claim"])

    with pytest.raises(RuntimeError, match="output failed"):
        args.func(args)

    assert transport.closed is True


def test_host_claim_help_promises_no_detach_and_no_endpoint_io() -> None:
    help_text = " ".join(cli.build_parser().format_help().split())
    assert "host-claim" in help_text
    assert "no detach" in help_text
    assert "no HCCAST/application bulk payload I/O" in help_text


def test_host_claim_subcommand_help_promises_no_detach_and_no_endpoint_io(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["host-claim", "--help"])

    assert raised.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "no kernel-driver detach" in help_text
    assert "no HCCAST/application bulk-endpoint payload I/O" in help_text
    assert "no configuration activation" in help_text
    assert "--hold-seconds" in help_text
    assert "maximum: 10" in help_text


def test_probe_help_lists_hardware_observed_id() -> None:
    help_text = cli.build_parser().format_help()
    assert "1cbe:0005" in help_text
    assert "protocol unverified" in help_text


def test_probe_miss_distinguishes_apk_ids_from_unverified_candidate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "enumerate_candidates", lambda: [])

    assert cli.command_probe_host(object()) == 2

    output = capsys.readouterr().out
    assert "APK-derived: 05ac:12ad and abcd:0002" in output
    assert "hardware-observed pre-protocol candidate: 1cbe:0005" in output
    assert "TI assigns that VID:PID to its MSC example" in output
    assert "protocol unverified until SETV" in output
    assert "HCCAST USB-device personality" not in output


def test_default_not_found_error_does_not_call_observed_id_hccast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _DefaultCore(_FakeDevice(), available_round=100)
    clock = _FakeClock()
    _patch_discovery_only(monkeypatch, core, clock)

    with pytest.raises(TransportError) as raised:
        HostUSBTransport()

    message = str(raised.value)
    assert "APK-derived: 05ac:12ad and abcd:0002" in message
    assert "hardware-observed pre-protocol candidate: 1cbe:0005" in message
    assert "TI MSC-assigned identity" in message
    assert "protocol unverified until SETV" in message
    assert "HCCAST USB-device personality" not in message
