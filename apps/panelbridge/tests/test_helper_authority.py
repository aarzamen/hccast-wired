"""Authentication and trusted-state filesystem tests; no live D-Bus."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helper.network import HelperError, NetworkHelper
from helper.service import Authority, Enrollment, GioIdentities, load_enrollment, FileJournal


class Identities:
    uid = 1000
    pid = 4321
    graphical = True
    service = False
    def credentials(self, sender):
        return self.uid, self.pid
    def active_graphical(self, uid):
        return self.graphical
    def rescue_service(self, pid, method):
        return self.service


def test_enrolled_gui_user_can_mutate_but_non_gui_user_only_read_or_release():
    identities = Identities()
    authority = Authority(lambda: Enrollment(1000, 991), identities)
    assert authority.authorize(":1.42", "Discover") == 1000
    identities.graphical = False
    assert authority.authorize(":1.42", "Status") == 1000
    assert authority.authorize(":1.42", "Release") == 1000
    for method in ("Discover", "Connect", "KeepAlive"):
        with pytest.raises(HelperError, match="NotAuthorized"):
            authority.authorize(":1.42", method)


def test_non_gui_release_authority_cannot_release_another_unique_owner():
    identities = Identities()
    identities.graphical = False
    authority = Authority(lambda: Enrollment(1000, 991), identities)
    helper = NetworkHelper(None, SimpleNamespace(read=lambda: None))
    helper.owner = ":1.99"
    assert authority.authorize(":1.42", "Release") == 1000
    with pytest.raises(HelperError, match="Busy"):
        helper.release(":1.42")
    assert helper.owner == ":1.99"
    assert not helper.cancel.is_set()


def test_root_and_other_user_have_no_production_bypass():
    identities = Identities()
    authority = Authority(lambda: Enrollment(1000), identities)
    for uid in (0, 1001):
        identities.uid = uid
        for method in ("Status", "Discover", "Release"):
            with pytest.raises(HelperError, match="NotAuthorized"):
                authority.authorize(":1.42", method)


def test_rescue_identity_requires_fixed_service_for_every_method():
    identities = Identities()
    identities.uid = 991
    authority = Authority(lambda: Enrollment(1000, 991), identities)
    with pytest.raises(HelperError, match="NotAuthorized"):
        authority.authorize(":1.42", "Status")
    identities.service = True
    assert authority.authorize(":1.42", "Connect") == 991


@pytest.fixture
def systemd_rescue_authority(monkeypatch):
    def create(state, unit="panelbridge-rescue.service"):
        identities = GioIdentities(None, None, None)
        monkeypatch.setattr(identities, "credentials", lambda sender: (991, 4321))
        monkeypatch.setattr(identities, "call", lambda *args: ("/synthetic/rescue/unit",))
        monkeypatch.setattr(identities, "props", lambda *args: {
            "Id": unit, "ActiveState": state,
        })
        return Authority(lambda: Enrollment(1000, 991), identities)
    return create


@pytest.mark.parametrize("method", ["Status", "Discover", "Connect", "KeepAlive", "Release"])
def test_active_rescue_service_can_use_existing_api(systemd_rescue_authority, method):
    assert systemd_rescue_authority("active").authorize(":1.42", method) == 991


def test_stopping_rescue_service_can_release_for_orderly_restoration(systemd_rescue_authority):
    assert systemd_rescue_authority("deactivating").authorize(":1.42", "Release") == 991


@pytest.mark.parametrize("method", ["Status", "Discover", "Connect", "KeepAlive"])
def test_stopping_rescue_service_cannot_start_or_extend_work(systemd_rescue_authority, method):
    with pytest.raises(HelperError, match="NotAuthorized"):
        systemd_rescue_authority("deactivating").authorize(":1.42", method)


@pytest.mark.parametrize("state", ["inactive", "failed", "activating"])
def test_rescue_release_rejects_other_unit_states(systemd_rescue_authority, state):
    with pytest.raises(HelperError, match="NotAuthorized"):
        systemd_rescue_authority(state).authorize(":1.42", "Release")


@pytest.mark.parametrize("state", ["active", "deactivating"])
def test_other_service_cannot_release_as_rescue(systemd_rescue_authority, state):
    with pytest.raises(HelperError, match="NotAuthorized"):
        systemd_rescue_authority(state, "other.service").authorize(":1.42", "Release")


def test_unknown_method_and_well_known_sender_fail_closed():
    authority = Authority(lambda: Enrollment(1000), Identities())
    for sender, method in (("org.example.App", "Status"), (":1.1", "RunCommand")):
        with pytest.raises(HelperError):
            authority.authorize(sender, method)


def test_enrollment_requires_root_regular_nonwritable_file(tmp_path):
    path = tmp_path / "enrollment.json"
    path.write_text(json.dumps({"api_version": 1, "normal_uid": 1000}))
    path.chmod(0o600)
    if os.getuid() != 0:
        with pytest.raises(HelperError, match="EnrollmentInvalid"):
            load_enrollment(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(HelperError, match="EnrollmentInvalid"):
        load_enrollment(link)
    path.chmod(0o666)
    with pytest.raises(HelperError, match="EnrollmentInvalid"):
        load_enrollment(path)


def test_journal_rejects_untrusted_directory_without_writing(tmp_path):
    if os.getuid() == 0:
        tmp_path.chmod(0o777)
    with pytest.raises(HelperError, match="JournalInvalid"):
        FileJournal(tmp_path).write({"state": "pending"})
    assert not list(tmp_path.iterdir())
