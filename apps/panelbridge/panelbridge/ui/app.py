"""GTK4 interface for the PanelBridge session controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import gi

from .presenter import (
    PAGES, PRESETS, PROFILE_FIELDS, STATUSES, Presenter, about_text, preview_state, profile_summary,
)

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402 (versions precede GI imports)
from .client import SessionClient  # noqa: E402


CSS = """
window.panelbridge { background: #FAFCFD; color: #18303D; font-size: 15px; }
.pb-toolbar { background: #15354A; color: white; padding: 6px 10px; }
.pb-brand { font-size: 17px; font-weight: 700; }
.pb-toolbar button, .pb-toolbar dropdown { color: #18303D; }
.pb-page { padding: 14px 18px 22px; }
.pb-heading { font-size: 22px; font-weight: 700; }
.pb-section { font-size: 17px; font-weight: 700; margin-top: 12px; }
.pb-detail { font-size: 14px; color: #425E6F; }
.pb-status { font-size: 19px; font-weight: 700; color: #15354A; }
.pb-banner { background: #E7EEF3; padding: 6px 10px; }
.pb-preview { background: #D8E8F2; color: #15354A; font-weight: 700; padding: 5px 10px; }
.pb-trial { background: #FFF0C7; color: #583B00; padding: 5px 10px; }
.pb-warning { color: #845400; }
.pb-error { background: #FFF0C7; color: #583B00; padding: 6px 10px; }
button { min-height: 30px; padding: 3px 10px; }
button.pb-primary { background: #1C5D83; color: white; border-color: #164B68; }
button:focus-visible, dropdown:focus-visible, entry:focus-visible {
  outline: 3px solid #24658F; outline-offset: 2px;
}
entry { min-height: 30px; }
.pb-divider { margin-top: 8px; margin-bottom: 2px; }
"""


def label(text: str = "", css: str | None = None, *, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0, wrap=True, selectable=selectable)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_hexpand(True)
    if css:
        widget.add_css_class(css)
    return widget


def button(text: str, callback: Any, *, primary: bool = False) -> Gtk.Button:
    widget = Gtk.Button(label=text, use_underline=True)
    widget.connect("clicked", callback)
    if primary:
        widget.add_css_class("pb-primary")
    return widget


def row(*widgets: Gtk.Widget, spacing: int = 8) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
    for widget in widgets:
        box.append(widget)
    return box


def asset_path() -> Path | None:
    paths = (Path(__file__).resolve().parents[2] / "assets" / "icons" / "hicolor" /
             "scalable" / "apps" / "org.panelbridge.PanelBridge.svg",
             Path("/usr/share/icons/hicolor/scalable/apps/org.panelbridge.PanelBridge.svg"),
             Path("/usr/share/panelbridge/org.panelbridge.PanelBridge.svg"))
    return next((path for path in paths if path.is_file()), None)


class PanelBridgeWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, *, preview: bool, width: int, height: int,
                 initial_page: str = "Connection"):
        super().__init__(application=app, title="PanelBridge")
        self.add_css_class("panelbridge")
        self.set_default_size(width, height)
        self.set_size_request(480, 300)
        self.set_icon_name("org.panelbridge.PanelBridge")
        self.presenter = Presenter(preview=preview)
        self.client = None if preview else SessionClient(self._state_changed, self._service_error)
        self._buttons: list[tuple[Gtk.Button, str, Gtk.Label | None]] = []
        self._candidate_snapshot = ""
        self._candidates: list[dict[str, Any]] = []
        self._dirty = False
        self._setting_fields = False
        self._about_loaded = False
        self._about_loading = False
        self._about_attempted = False
        self._cpu_snapshots: dict[str, list[int]] = {}
        self._pairing_window: Gtk.Window | None = None
        self._timer = 0

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._style_provider = provider
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        toolbar = row(spacing=8)
        toolbar.add_css_class("pb-toolbar")
        icon = asset_path()
        if icon:
            image = Gtk.Image.new_from_file(str(icon))
            image.set_pixel_size(30)
            toolbar.append(image)
        brand = label("PanelBridge", "pb-brand")
        brand.set_hexpand(True)
        toolbar.append(brand)
        self.navigation = Gtk.DropDown.new_from_strings(list(PAGES))
        self.navigation.set_tooltip_text("Choose a page. Alt+1 through Alt+6 also navigate.")
        toolbar.append(self.navigation)
        toolbar.append(button("_Recovery", lambda _: self.show_page("Recovery")))
        outer.append(toolbar)

        self.preview_banner = label("Preview — no hardware", "pb-preview")
        self.preview_banner.set_visible(preview)
        outer.append(self.preview_banner)
        self.error_box = row()
        self.error_box.add_css_class("pb-error")
        self.error_label = label()
        self.error_label.set_lines(2)
        self.error_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.error_box.append(self.error_label)
        self.retry = button("_Retry", lambda _: self._retry())
        self.error_box.append(self.retry)
        self.error_box.append(button("Details", lambda _: self.show_page("Connection")))
        self.dismiss = button("Dismiss", lambda _: self._dismiss_error())
        self.error_box.append(self.dismiss)
        outer.append(self.error_box)

        self.trial_box = row()
        self.trial_box.add_css_class("pb-trial")
        self.trial_label = label()
        self.trial_box.append(self.trial_label)
        self.keep_button = self._command_button("_Keep", "confirm_profile")
        self.revert_button = self._command_button("Re_vert", "revert_profile")
        self.trial_box.append(self.keep_button)
        self.trial_box.append(self.revert_button)
        outer.append(self.trial_box)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.pages: dict[str, Gtk.Box] = {}
        for name in PAGES:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            page.add_css_class("pb-page")
            page.append(label(name, "pb-heading"))
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(page)
            self.stack.add_named(scroll, name)
            self.pages[name] = page
        outer.append(self.stack)
        self._build_connection()
        self._build_display()
        self._build_presets()
        self._build_advanced()
        self._build_recovery()
        self._build_about()
        self.navigation.connect("notify::selected", self._page_selected)
        self.show_page(initial_page)
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._key_pressed)
        self.add_controller(controller)
        self.connect("close-request", self._close)
        self._timer = GLib.timeout_add(250, self._tick)
        if preview:
            self._state_changed(preview_state())
        else:
            self.presenter.error = "Connecting to the desktop session service…"
            self._render()
            GLib.idle_add(self._connect_once)

    def _command_button(self, text: str, verb: str, *, primary: bool = False,
                        reason: Gtk.Label | None = None, payload: Any = None) -> Gtk.Button:
        control = button(text, lambda _: self._send(verb, payload() if callable(payload) else payload),
                         primary=primary)
        self._buttons.append((control, verb, reason))
        return control

    def _section(self, page: str, title: str, text: str | None = None) -> None:
        self.pages[page].append(label(title, "pb-section"))
        if text:
            self.pages[page].append(label(text, "pb-detail"))

    def _build_connection(self) -> None:
        page = self.pages["Connection"]
        self.status_label = label("○ Waiting for the controller", "pb-status")
        self.message_label = label()
        self.connection_error = label(css="pb-warning", selectable=True)
        self.receiver_label = label()
        self.connection_reason = label(css="pb-detail")
        page.append(self.status_label)
        page.append(self.message_label)
        page.append(self.connection_error)
        page.append(self.receiver_label)
        page.append(row(self._command_button("_Find displays", "discover"),
                        self._command_button("_Connect", "start", primary=True),
                        self._command_button("_Disconnect", "stop")))
        page.append(self.connection_reason)
        self._section("Connection", "Choose your display",
                      "Wake the screen, find displays, then select the unit beside you.")
        self.candidate_list = Gtk.DropDown.new_from_strings(["No displays found"])
        self.candidate_list.set_hexpand(True)
        self.candidate_list.connect("notify::selected", lambda *_: self._render_candidate())
        self.select_button = button("_Select display", lambda _: self._select_candidate())
        page.append(self.candidate_list)
        page.append(self.select_button)
        self.candidate_detail = label(css="pb-detail")
        page.append(self.candidate_detail)
        self._section("Connection", "Transports")
        page.append(label("Wireless uses Wi-Fi Direct / Miracast. Verification is shown for the selected pairing.",
                          "pb-detail"))
        page.append(label("USB video: Unavailable. A wired video backend for this screen has not been verified.",
                          "pb-detail"))

    def _build_display(self) -> None:
        page = self.pages["Display"]
        self.profile_label = label(selectable=True)
        page.append(self.profile_label)
        page.append(label(self.presenter.native_resolution_text(), "pb-detail"))
        page.append(label("Stream dimensions describe the accepted video signal. Content fps is the rate of "
                          "new desktop frames; stream refresh can be higher.", "pb-detail"))
        self._section("Display", "Manual settings",
                      "Experimental changes are checked by the controller and use timed rollback. "
                      "Keep a change only when the screen is readable.")
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.fields: dict[str, Gtk.Entry] = {}
        for index, (key, title) in enumerate(PROFILE_FIELDS.items()):
            field_label = label(title)
            field_label.set_hexpand(True)
            entry = Gtk.Entry()
            entry.set_width_chars(9)
            entry.set_max_width_chars(12)
            entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
            entry.set_tooltip_text(title)
            field_label.set_mnemonic_widget(entry)
            entry.connect("changed", self._field_changed)
            grid.attach(field_label, 0, index, 1, 1)
            grid.attach(entry, 1, index, 1, 1)
            self.fields[key] = entry
        page.append(grid)
        self.display_reason = label(css="pb-detail")
        self.apply_button = button("_Try display settings", lambda _: self._apply_profile(), primary=True)
        self.reload_button = button("Reload current", lambda _: self._reload_fields())
        page.append(row(self.apply_button, self.reload_button))
        page.append(self.display_reason)
        page.append(label("The controller restores the previous mode if the confirmation timer expires, "
                          "even if this window closes.", "pb-detail"))

    def _build_presets(self) -> None:
        page = self.pages["Presets"]
        page.append(label("Presets become available after calibration for this device and software setup. "
                          "Only recorded results appear here.", "pb-detail"))
        self.preset_widgets: dict[str, tuple[Gtk.Label, Gtk.Label, Gtk.Label, Gtk.Button]] = {}
        for key, title in PRESETS.items():
            self._section("Presets", title)
            summary = label()
            measurement = label(css="pb-detail")
            reason = label(css="pb-detail")
            apply = button(f"Use {title}", lambda _, chosen=key: self._send("apply_profile", {"preset": chosen}))
            for widget in (summary, measurement, apply, reason):
                page.append(widget)
            self.preset_widgets[key] = (summary, measurement, reason, apply)
        self._section("Presets", "Calibration",
                      "Measured compute load does not establish measured input power or physical latency.")
        reason = label(css="pb-detail")
        page.append(self._command_button("_Calibrate", "calibrate", reason=reason))
        page.append(reason)

    def _build_advanced(self) -> None:
        page = self.pages["Advanced"]
        self.health_label = label()
        page.append(self.health_label)
        self._section("Advanced", "CPU limits",
                      "Choose only a frequency validated for the current host. Profiles keep their recorded limits.")
        self.cpu_reference = label("Factory reference: not reported\nSaved baseline: not reported\n"
                                   "Current limit: not reported", "pb-detail")
        page.append(self.cpu_reference)
        self.cpu_widgets: dict[str, tuple[Gtk.DropDown, Gtk.Button, Gtk.Label]] = {}
        for verb, title in (("set_cpu_cap", "Apply CPU cap"), ("trial_clock", "Try CPU frequency")):
            if verb == "trial_clock":
                self._section("Advanced", "Frequency trial",
                              "Available only after supply, cooling, baseline and independent recovery checks. "
                              "Thermal backoff starts at 70 °C; trials abort at 75 °C or a new health fault.")
            choices = Gtk.DropDown.new_from_strings(["No validated choices"])
            choices.set_hexpand(True)
            apply = button(title, lambda _, operation=verb: self._apply_cpu(operation))
            reason = label(css="pb-detail")
            page.append(row(choices, apply))
            page.append(reason)
            self.cpu_widgets[verb] = (choices, apply, reason)
        self._section("Advanced", "Saved defaults",
                      "Restore the recorded app baseline for this installation.")
        reason = label(css="pb-detail")
        page.append(self._command_button("Restore saved _defaults", "restore_defaults", reason=reason))
        page.append(reason)

    def _build_recovery(self) -> None:
        page = self.pages["Recovery"]
        page.append(label("Use recovery if the screen becomes unreadable or a change fails.", "pb-detail"))
        self.recovery_label = label()
        page.append(self.recovery_label)
        self._section("Recovery", "Return to a known display")
        reason = label(css="pb-detail")
        page.append(self._command_button("Revert display change", "revert_profile", reason=reason))
        page.append(reason)
        reason = label(css="pb-detail")
        page.append(self._command_button("Restore saved defaults", "restore_defaults", reason=reason))
        page.append(reason)
        self._section("Recovery", "Independent access",
                      "Keep the Pi's Ethernet connection available. Use your existing SSH access if the "
                      "desktop service cannot respond. A lost screen alone does not prove the Pi has stopped.")
        self._section("Recovery", "LAN recovery enrollment",
                      "Enrollment is completed by you. A private pairing code appears only in a temporary window.")
        reason = label(css="pb-detail")
        page.append(self._command_button("Start recovery _pairing", "start_recovery_pairing", reason=reason))
        page.append(reason)
        page.append(button("Retry desktop service", lambda _: self._retry()))

    def _build_about(self) -> None:
        page = self.pages["About"]
        page.append(label("PanelBridge", "pb-status"))
        page.append(label("A hobbyist display bridge for the normal Raspberry Pi desktop. Video-only v1.",
                          "pb-detail"))
        self._section("About", "Contributions")
        page.append(label("Direction and physical testing: project owner.\n"
                          "Implementation assistance: OpenAI Codex. Model/version: not recorded.",
                          "pb-detail"))
        self._section("About", "Installed component notices")
        self.about_label = label("Connect to the controller to read the installed credits and license records.",
                                 "pb-detail", selectable=True)
        page.append(self.about_label)
        reason = label(css="pb-detail")
        page.append(self._command_button("Refresh installed notices", "about", reason=reason))
        page.append(reason)
        page.append(label("Upstream components include GTK, PyGObject, GLib/Gio, GStreamer and "
                          "GNOME Network Displays. Existing HCCAST MIT notices remain with their source.",
                          "pb-detail"))

    def show_page(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        self.navigation.set_selected(PAGES.index(name))
        if name == "About":
            self._load_about()

    def _page_selected(self, control: Gtk.DropDown, _spec: Any) -> None:
        index = control.get_selected()
        if index < len(PAGES):
            name = PAGES[index]
            self.stack.set_visible_child_name(name)
            if name == "About":
                self._load_about()

    def _key_pressed(self, _controller: Any, keyval: int, _keycode: int,
                     modifiers: Gdk.ModifierType) -> bool:
        if modifiers & Gdk.ModifierType.ALT_MASK and Gdk.KEY_1 <= keyval <= Gdk.KEY_6:
            self.show_page(PAGES[keyval - Gdk.KEY_1])
            return True
        if keyval == Gdk.KEY_Escape:
            self.show_page("Connection")
            return True
        return False

    def _connect_once(self) -> bool:
        if self.client:
            self.client.connect()
        return GLib.SOURCE_REMOVE

    def _retry(self) -> None:
        if self.presenter.preview:
            return
        self.presenter.disconnect("Connecting to the desktop session service…")
        self._about_loaded = self._about_loading = False
        self._about_attempted = False
        self._render()
        self._connect_once()

    def _dismiss_error(self) -> None:
        if self.presenter.connected:
            self.presenter.error = ""
            self._render()

    def _service_error(self, message: str) -> None:
        self.presenter.disconnect(message)
        self._about_loaded = self._about_loading = False
        self._about_attempted = False
        self._render()

    def _state_changed(self, state: dict[str, Any]) -> None:
        try:
            self.presenter.update(state)
        except ValueError:
            self._render()
            return
        self._render()
        if self.stack.get_visible_child_name() == "About":
            self._load_about()

    def _send(self, verb: str, payload: dict[str, Any] | None = None) -> None:
        try:
            command, arguments = self.presenter.begin(verb, payload)
        except ValueError as error:
            self.presenter.error = str(error)
            self._render()
            return
        self._render()
        if not self.client:
            self.presenter.finish(verb, "Preview — no hardware. Actions are disabled.")
            self._render()
            return

        def finished(result: dict[str, Any] | None, error: str | None) -> None:
            self.presenter.finish(verb, error)
            if verb == "about":
                self._about_loading = False
                self._about_loaded = error is None
            if error is None and result is not None:
                if verb == "about":
                    self._render_about(result)
                elif verb == "start_recovery_pairing":
                    self._show_pairing(result)
                elif verb in {"apply_profile", "restore_defaults", "revert_profile"}:
                    self._dirty = False
            self._render()

        self.client.command(command, arguments, finished)

    def _select_candidate(self) -> None:
        index = self.candidate_list.get_selected()
        if index < len(self._candidates):
            self._send("select", {"candidate_id": self._candidates[index]["id"]})

    def _field_changed(self, _entry: Gtk.Entry) -> None:
        if not self._setting_fields:
            self._dirty = True

    def _reload_fields(self) -> None:
        self._dirty = False
        self._render_fields()

    def _apply_profile(self) -> None:
        self._send("apply_profile", {key: entry.get_text() for key, entry in self.fields.items()})

    def _apply_cpu(self, verb: str) -> None:
        index = self.cpu_widgets[verb][0].get_selected()
        choices = self.presenter.cpu_choices(verb)
        if index < len(choices):
            self._send(verb, {"mhz": choices[index]})

    def _render_fields(self) -> None:
        profile = self.presenter.state.get("profile") or {}
        gate = self.presenter.availability("apply_profile")
        self.apply_button.set_sensitive(gate.enabled)
        self.apply_button.set_tooltip_text(gate.reason or "Try this profile with timed confirmation")
        self.display_reason.set_text(gate.reason)
        self.display_reason.set_visible(bool(gate.reason))
        self.reload_button.set_sensitive(self.presenter.connected)
        for key, entry in self.fields.items():
            entry.set_sensitive(gate.enabled)
            if not self._dirty:
                self._setting_fields = True
                value = profile.get(key)
                entry.set_text("" if value is None else str(value))
                self._setting_fields = False

    def _render_candidate(self) -> None:
        if not hasattr(self, "candidate_detail"):
            return
        gate = self.presenter.availability("select")
        index = self.candidate_list.get_selected()
        available = index < len(self._candidates)
        if available:
            candidate = self._candidates[index]
            usb = "usb" in str(candidate.get("transport", "")).lower()
            details = f"{candidate.get('manufacturer') or 'Manufacturer unknown'}\n" \
                      f"Transport: {candidate.get('transport') or 'unknown'}"
            if usb:
                details += "\nUSB video is unavailable."
            elif gate.reason:
                details += f"\n{gate.reason}"
            self.select_button.set_sensitive(gate.enabled and not usb)
        else:
            details = "No receiver candidates. Wake your display, then choose Find displays."
            self.select_button.set_sensitive(False)
        self.candidate_detail.set_text(details)
        self.candidate_list.set_sensitive(available and gate.enabled)

    def _render(self) -> None:
        state = self.presenter.state
        self.error_box.set_visible(bool(self.presenter.error))
        self.error_label.set_text(self.presenter.error)
        self.error_label.set_tooltip_text(self.presenter.error)
        self.connection_error.set_text(self.presenter.error)
        self.connection_error.set_visible(bool(self.presenter.error))
        self.retry.set_visible(not self.presenter.connected)
        self.retry.set_sensitive(not self.presenter.preview)
        self.dismiss.set_visible(self.presenter.connected)
        if self.presenter.connected:
            self.status_label.set_text(STATUSES.get(state.get("status"), "? Unknown"))
            self.message_label.set_text(str(state.get("message") or ""))
        else:
            self.status_label.set_text("! Desktop service unavailable")
            self.message_label.set_text("Retry the service connection. Recovery is available above.")
        receiver = state.get("receiver") or {}
        if receiver:
            self.receiver_label.set_text(f"{receiver.get('name') or 'Selected display'}\n"
                                         f"{receiver.get('transport') or 'Transport unknown'}\n"
                                         f"Verification: {receiver.get('verification') or 'Experimental'}")
        else:
            self.receiver_label.set_text("No display selected")
        for control, verb, reason in self._buttons:
            gate = self.presenter.availability(verb)
            control.set_sensitive(gate.enabled)
            control.set_tooltip_text(gate.reason or None)
            if reason is not None:
                reason.set_text(gate.reason)
                reason.set_visible(bool(gate.reason))
        reasons = [self.presenter.availability(verb).reason for verb in ("discover", "start")]
        self.connection_reason.set_text("\n".join(dict.fromkeys(value for value in reasons if value)))
        candidates = [item for item in state.get("candidates", [])
                      if isinstance(item, dict) and isinstance(item.get("id"), str)]
        snapshot = json.dumps(candidates, sort_keys=True)
        if snapshot != self._candidate_snapshot:
            old_index = self.candidate_list.get_selected()
            old_id = self._candidates[old_index]["id"] if old_index < len(self._candidates) else None
            self._candidates = candidates
            self._candidate_snapshot = snapshot
            self.candidate_list.set_model(Gtk.StringList.new(
                [str(item.get("name") or "Unnamed display") for item in candidates] or ["No displays found"]))
            index = next((i for i, item in enumerate(candidates) if item["id"] == old_id), 0)
            self.candidate_list.set_selected(index)
        self._render_candidate()
        profile_text = profile_summary(state.get("profile"))
        if not self.presenter.connected:
            profile_text = "Last reported profile; live status unavailable.\n" + profile_text
        self.profile_label.set_text(profile_text)
        self._render_fields()
        for key, widgets in self.preset_widgets.items():
            preset = self.presenter.preset(key)
            summary, measurement, reason, apply = widgets
            summary.set_text(preset.summary)
            measurement.set_text(preset.measurement)
            reason.set_text(preset.reason)
            reason.set_visible(bool(preset.reason))
            apply.set_sensitive(preset.enabled)
            apply.set_tooltip_text(preset.reason or None)
        self.health_label.set_text(self.presenter.health_text())
        for verb, (dropdown, apply, reason) in self.cpu_widgets.items():
            choices = self.presenter.cpu_choices(verb)
            if self._cpu_snapshots.get(verb) != choices:
                self._cpu_snapshots[verb] = choices
                dropdown.set_model(Gtk.StringList.new([f"{value} MHz" for value in choices]
                                                       or ["No validated choices"]))
                dropdown.set_selected(0)
            gate = self.presenter.availability(verb)
            dropdown.set_sensitive(gate.enabled)
            apply.set_sensitive(gate.enabled)
            reason.set_text(gate.reason)
            reason.set_visible(bool(gate.reason))
        recovery = state.get("recovery") or {}
        if not self.presenter.connected:
            recovery = {"message": "Live recovery status unavailable. Use your existing independent access."}
        def yes_no(value: Any) -> str:
            return "Available" if value is True else "Unavailable" if value is False else "Unknown"
        self.recovery_label.set_text(
            f"Existing SSH access: {yes_no(recovery.get('ssh_available'))}\n"
            f"LAN recovery: {'Enrolled' if recovery.get('lan_enrolled') is True else 'Not enrolled' if recovery.get('lan_enrolled') is False else 'Unknown'}\n"
            f"{recovery.get('message') or 'Recovery state has not been reported.'}"
        )
        self._render_trial()

    def _render_trial(self) -> None:
        trial = self.presenter.state.get("trial")
        self.trial_box.set_visible(bool(trial))
        if not trial:
            return
        seconds = self.presenter.trial_remaining()
        if not self.presenter.connected:
            text = "Confirmation unavailable. Automatic rollback remains with the controller."
        elif seconds in (None, 0):
            text = "Waiting for automatic restoration…"
        else:
            text = f"Keep this change? {seconds} s"
        self.trial_label.set_text(text)
        self.trial_label.set_tooltip_text(str(trial.get("message") or text))
        self.keep_button.set_sensitive(self.presenter.availability("confirm_profile").enabled)
        self.revert_button.set_sensitive(self.presenter.availability("revert_profile").enabled)

    def _tick(self) -> bool:
        self._render_trial()
        return GLib.SOURCE_CONTINUE

    def _load_about(self) -> None:
        if self._about_attempted or self._about_loading or not self.presenter.availability("about").enabled:
            return
        self._about_attempted = True
        self._about_loading = True
        self._send("about")

    def _render_about(self, result: dict[str, Any]) -> None:
        self.about_label.set_text(about_text(result))

    def _show_pairing(self, result: dict[str, Any]) -> None:
        if self._pairing_window:
            self._pairing_window.close()
        dialog = Gtk.Window(title="Recovery enrollment", transient_for=self, modal=True)
        dialog.set_default_size(420, 230)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in (box.set_margin_top, box.set_margin_bottom, box.set_margin_start, box.set_margin_end):
            setter(18)
        dialog.set_child(box)
        box.append(label("Complete recovery enrollment", "pb-section"))
        code = result.get("pairing_code", result.get("code"))
        box.append(label(str(result.get("message") or "Use this private code to complete enrollment.")))
        private_label = label(str(code) if code else "No pairing code was returned.", selectable=True)
        box.append(private_label)
        if isinstance(result.get("url"), str):
            box.append(label(result["url"], selectable=True))
        box.append(button("_Close", lambda _: dialog.close()))

        def closing(_window: Gtk.Window) -> bool:
            private_label.set_text("")
            self._pairing_window = None
            return False

        dialog.connect("close-request", closing)
        self._pairing_window = dialog
        dialog.present()

    def _close(self, _window: Gtk.Window) -> bool:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self.client:
            self.client.close()
        if self._pairing_window:
            self._pairing_window.close()
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self._style_provider)
        return False


class PanelBridgeApplication(Gtk.Application):
    def __init__(self, args: argparse.Namespace):
        super().__init__(application_id="org.panelbridge.PanelBridge",
                         flags=Gio.ApplicationFlags.NON_UNIQUE if args.preview else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.args = args
        self.window: PanelBridgeWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = PanelBridgeWindow(self, preview=self.args.preview, width=self.args.width,
                                           height=self.args.height, initial_page=self.args.page)
            self.window.connect("destroy", lambda _: setattr(self, "window", None))
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PanelBridge native desktop interface")
    parser.add_argument("--preview", action="store_true", help="Show synthetic state; never connect to hardware")
    parser.add_argument("--width", type=int, default=780)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--page", choices=PAGES, default="Connection")
    args = parser.parse_args(argv)
    if args.width < 480 or args.height < 300:
        parser.error("The minimum window size is 480 by 300.")
    application = PanelBridgeApplication(args)
    return application.run([sys.argv[0]])
