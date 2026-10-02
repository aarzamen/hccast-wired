"""User-only desktop/autostart preparation; no root or running-session changes."""

from .desktop_setup import DesktopSetup


class UserSetup(DesktopSetup):
    """Manage desktop blocks and the fixed installed session autostart template.

    install(outputs), uninstall() and status() use a shared durable journal with
    DesktopSetup. Legacy desktop-only originals survive autostart enrollment.
    Successful removal describes config bytes only, never live output geometry.
    """

    include_autostart = True
