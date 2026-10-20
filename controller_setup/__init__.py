"""Bluetooth controller pairing session for Controller Manager.

The package is deliberately separate from the tray daemon: discovery, pairing
and the fullscreen pairing UI must never block hotplug adoption or the tray
menu. The daemon keeps doing what it already does once BlueZ has connected a
pad and the kernel exposes it through evdev.
"""

__all__ = ["bluez", "combo", "gamepad", "health", "overlay", "session",
           "support", "ui"]
