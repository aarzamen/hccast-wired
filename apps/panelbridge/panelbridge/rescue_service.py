"""Fixed service entry point for the dedicated unprivileged rescue account."""

import asyncio
import json
import os
from pathlib import Path
import pwd
import re
import signal

from helper.service import _read_json_at, _trusted_directory
from .network_client import NetworkClient
from .rescue import RescueError, RescueRuntime

CONFIG = Path("/etc/panelbridge/rescue.json")


def validate_receiver_config(value):
    if not isinstance(value, dict) or set(value) != {"api_version", "receiver_address", "seconds"}:
        raise ValueError("Invalid rescue binding")
    address, seconds = value["receiver_address"], value["seconds"]
    if (type(value["api_version"]) is not int or value["api_version"] != 1
            or not isinstance(address, str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address)
            or int(address[:2], 16) & 1 or type(seconds) is not int or not 30 <= seconds <= 600):
        raise ValueError("Invalid rescue binding")
    return address.lower(), seconds


def load_binding():
    directory = _trusted_directory(CONFIG.parent, "RescueConfigInvalid")
    try:
        value = _read_json_at(directory, CONFIG.name, "RescueConfigInvalid")
    finally:
        os.close(directory)
    return validate_receiver_config(value)


async def run():
    # Imported here so configuration checks have no sampler/service startup side effects.
    from .session_service import HealthSampler

    expected_uid = pwd.getpwnam("panelbridge-rescue").pw_uid
    if expected_uid <= 0 or os.getuid() != expected_uid or os.geteuid() != expected_uid:
        raise RescueError("dedicated_identity_required")
    address, seconds = load_binding()
    sampler = HealthSampler(timeout=3)
    network = None
    runtime = None
    stopped = False

    def stop():
        nonlocal stopped
        stopped = True
        if runtime:
            runtime.request_stop()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    try:
        # Opening the bus is independently bounded; no receiver work occurs here.
        network = NetworkClient()
        runtime = RescueRuntime(network, receiver_address=address,
                                expected_uid=expected_uid, health=sampler.sample)
        if stopped:
            runtime.request_stop()
        result = await runtime.run(seconds=seconds)
        print(json.dumps({"claim": "OBSERVED", **result}, allow_nan=False), flush=True)
    finally:
        # Runtime already closes its connection, but initialization failures must also
        # relinquish ownership. The client close method is idempotent.
        try:
            if network:
                await network.close()
        finally:
            await sampler.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)


def main():
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("The rescue renderer must not run as root")
    try:
        asyncio.run(run())
    except Exception as error:
        # Receiver, network and arbitrary exception text never enter exported logs.
        code = str(error) if isinstance(error, RescueError) else "rescue_failed"
        print(json.dumps({"claim": "OBSERVED", "status": "failed", "reason": code}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
