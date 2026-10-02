"""Installed rescue entry-point validation, without hardware or system writes."""

import pytest

from panelbridge.rescue_service import validate_receiver_config


def test_rescue_binding_accepts_only_saved_receiver_and_bounded_duration():
    assert validate_receiver_config({
        "api_version": 1, "receiver_address": "02:00:00:00:00:01", "seconds": 90,
    }) == ("02:00:00:00:00:01", 90)


@pytest.mark.parametrize("value", [
    [], None, {},
    {"api_version": 1, "receiver_address": "02:00:00:00:00:01", "seconds": 0},
    {"api_version": 1, "receiver_address": "02:00:00:00:00:01", "seconds": 601},
    {"api_version": 1, "receiver_address": "02:00:00:00:00:01", "seconds": True},
    {"api_version": 1, "receiver_address": "03:00:00:00:00:01", "seconds": 60},
    {"api_version": 1, "receiver_address": "not-a-receiver", "seconds": 60},
    {"api_version": 1, "receiver_address": "02:00:00:00:00:01", "seconds": 60,
     "worker": "/tmp/foreign-worker"},
])
def test_rejects_invalid_or_expanded_rescue_authority(value):
    with pytest.raises(ValueError, match="Invalid rescue binding"):
        validate_receiver_config(value)
