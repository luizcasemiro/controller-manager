# Controller Manager

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![Linux](https://img.shields.io/badge/Linux-evdev%20%2F%20uinput-FCC624?logo=linux&logoColor=black)](https://www.kernel.org/doc/html/latest/input/uinput.html) [![D-Bus](https://img.shields.io/badge/Tray-StatusNotifierItem-4A86CF)](https://www.freedesktop.org/wiki/Specifications/StatusNotifierItem/) [![systemd](https://img.shields.io/badge/Service-systemd%20user-30D475?logo=systemd&logoColor=white)](https://www.freedesktop.org/software/systemd/man/systemd.user.html) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small system-wide daemon that remaps game controllers between protocols on Linux,
controlled from a tray icon. It presents a physical controller to applications under a
different identity - for example, exposing a Sony DualSense as an Xbox 360 pad so that
applications which only speak the XInput protocol see a controller they understand.

## About

Many games and applications support only one controller protocol. A title that expects
**XInput** (the Xbox protocol) will not recognise a controller that registers itself as a
PlayStation device, even though both are ordinary gamepads. The usual workarounds are
per-application and fragile.

Controller Manager solves this once, at the system level: it grabs the physical device
on the kernel's input layer and re-emits its events through a **virtual** controller of
the target type, before any application enumerates devices. The remap is selected per
controller from a tray menu and applies uniformly to every launcher and application -
there is no per-application configuration.

This repository is a self-contained tool and a worked example of a Linux input pipeline:
`evdev` device grabbing, `uinput` virtual devices, raw HID access control, and a
`StatusNotifierItem` tray served directly over D-Bus.

## Features

- **Per-controller remapping** - each connected controller has its own mode (native or
  remapped), chosen from the tray.
- **Per-button bindings (Per-Button Remapping GUI)** - a small GTK window can rebuild any
  button of any controller press-by-press: record a physical button, pick its output
  (including *program default* to fall back to the mode's own mapping), per controller and
  independent of its mode. Bindings persist in `~/.config/controller-modes.json` under
  `_bindings`; see [the GUI decision](docs/decisions/button-binding-ui.md).
- **Multiple controllers at once** - two identical pads are tracked independently via a
  stable per-device identity, so one can be native while the other is remapped.
- **Launcher-agnostic** - works at the device layer, so every application sees the result
  regardless of how it was started.
- **Raw HID gating** - closes the double-input gap that an `evdev` grab alone leaves
  open, including against processes that opened the pad before the gate (Steam Input)
  (see [the hidraw gate decision](docs/decisions/hidraw-gate.md)).
- **Lightbar status colours (DualSense)** - the pad shows its active mode at a glance:
  **blue** = native, **green** = Xbox emulation. Games may take the lightbar over while
  they run; the resting colour returns when they exit (see
  [Steam coexistence](docs/decisions/steam-coexistence.md)).
- **Cinnamon / Linux Mint fallback tray** - where a desktop hosts no
  StatusNotifierItem (Cinnamon, Linux Mint), the same menu is re-served as an
  `XApp.StatusIcon`, which Mint's panel `xapp-status` applet displays. The two
  never double up (see [the XApp fallback decision](docs/decisions/xapp-tray-fallback.md)).
- **Hotplug aware** - controllers may be connected and disconnected at any time; the tray
  menu updates automatically.
- **No daemon dependencies beyond the standard desktop stack** - `python-evdev`,
  `dbus-python`, and `PyGObject`. GTK+3 is linked only for the Cinnamon/Mint tray
  fallback (already present in Mint's desktop stack).

## How it works

```
Physical controller (evdev /dev/input/eventX)
   │
   ├── native mode ──────────> device passes through unchanged
   │
   └── remap mode
         ├── evdev grab (EVIOCGRAB)     exclusive kernel access to the source
         ├── uinput virtual device      the target-protocol controller
         ├── event loop                 forward EV_KEY / EV_ABS / EV_REL
         └── hidraw gate                hide the source's raw HID node from applications
```

A `StatusNotifierItem` tray icon (served over D-Bus, no toolkit dependency) exposes the
per-controller mode menu. On desktops without a StatusNotifierItem host (Cinnamon, Linux
Mint) the identical menu is re-served as an `XApp.StatusIcon`, so Mint's panel shows it
without extra software. See [docs/architecture/overview.md](docs/architecture/overview.md)
for the full design.

## Requirements

- A modern Linux desktop with a **user systemd session**.
- A tray host that implements the **StatusNotifierItem** specification (most desktops do,
  some require an extension).
- Write access to **`/dev/uinput`** (granted by group membership on most distributions).
- Runtime packages:

  | Package (typical name) | Purpose |
  |---|---|
  | `python3-evdev` | read input devices, create `uinput` virtual devices |
  | `python3-dbus` (`dbus-python`) | serve the tray item and menu over D-Bus |
  | `python3-gi` (PyGObject) | GLib main loop; GTK+3 for the Cinnamon/Mint XApp tray fallback |

- *Optional, for the Bluetooth Xbox pad reporting product `0x02FD`:* `udev-hid-bpf`,
  `clang`, `bpftool`, `libbpf-devel`, and a kernel with `CONFIG_HID_BPF` + BTF. `install.sh`
  builds a HID-BPF descriptor fixup for that pad; without these it skips the fixup with a
  warning and everything else installs normally (see
  [the Xbox 0x02FD decision](docs/decisions/xbox-02fd-hid-bpf.md)).

## Installation

```bash
git clone https://github.com/NicolasPogorzelski/controller-manager.git
cd controller-manager
./install.sh
```

`install.sh` deploys the user-space daemon and service, enables it for autostart with the
session, then installs the root-owned hidraw gate helper, its udev rule and its sudoers rule
(this step asks for a password). For the full procedure with verification and rollback,
follow the [installation runbook](runbooks/install.md).

**If you use Steam:** set *Settings -> Controller -> PlayStation controller support* to
**"Enabled in Games w/o Support"** (not "Enabled", not off). The Steam client then holds
the pad permanently, but the daemon classifies game-vs-idle and restores the resting
lightbar colour whenever no game runs; titles keep their native DualSense features. Also
set *Xbox controller support* off. Turning PlayStation support off entirely is the one
broken middle ground - Steam still opens the pad to identify it and latches the lightbar
for no benefit. Details and trade-offs: [Steam coexistence](docs/decisions/steam-coexistence.md).

## Usage

Open the tray icon to see every connected controller and its available modes as radio
items. Selecting a mode applies it immediately and persists it.

| Controller family | Modes offered |
|---|---|
| PlayStation (DualSense) | **Native**; **Output as Xbox** |
| Xbox | **Native** |

The reverse direction (output a PlayStation identity from an Xbox pad) is intentionally
not offered; see [output protocol constraints](docs/decisions/output-protocol-constraints.md)
for why.

## Configuration

Modes are persisted per device in `~/.config/controller-modes.json`. The key is the
controller's stable identity (its `uniq`, e.g. the Bluetooth MAC) so that two identical
controllers keep separate settings:

```json
{
  "ac:36:1b:70:70:e8": "ps5-xbox",
  "48:18:8d:53:37:6e": "ps5-native"
}
```

A global `invert_y_axes` option (default `false`) mirrors the analog-stick Y axes
(`ABS_Y`, `ABS_RY`) on the virtual output of every remapped controller, useful when a
controller reports Y axes upside down. It is read at daemon start; restart the service
after editing it:

```json
{
  "invert_y_axes": true,
  "ac:36:1b:70:70:e8": "ps5-xbox"
}
```

The file is managed by the daemon; it is normally not edited by hand. A stored mode that
is no longer offered for a controller family falls back to that family's native default.

Per-button bindings live under the reserved `_bindings` key, keyed by controller identity
(stable per-device, same as modes). Each value maps a *device* button code (as the pad
reports it) to the *output* code of the selected identity, so a binding applies whichever
mode the controller is in:

```json
{
  "_bindings": {
    "ac:36:1b:70:70:e8": {
      "304": 305,
      "308": 311
    }
  }
}
```

A controller with no entry falls back to the mode's own quirk mapping; an empty
per-controller map means bindings are off for that controller. The files are normally
written through the remapping GUI, never by hand.

## Supported controllers

Controllers are recognised by **vendor plus gamepad capability**, not a fixed product-ID
list, so unlisted models of a known vendor still work. Product IDs are used only for
nicer display names. Tested families: Sony DualSense and Microsoft Xbox One / Series
pads, over USB and Bluetooth.

## Documentation

- [Architecture Overview](docs/architecture/overview.md)
- Design decisions:
  - [Remapping engine - evdev grab + uinput](docs/decisions/remapping-engine.md)
  - [The hidraw gate](docs/decisions/hidraw-gate.md)
  - [Per-device identity](docs/decisions/per-device-identity.md)
  - [Output protocol constraints](docs/decisions/output-protocol-constraints.md)
  - [Steam coexistence & lightbar ownership](docs/decisions/steam-coexistence.md)
  - [Daemon-owned player numbers](docs/decisions/player-leds.md)
  - [dbusmenu item model](docs/decisions/tray-menu-model.md)
  - [XApp.StatusIcon tray fallback for Cinnamon](docs/decisions/xapp-tray-fallback.md)
  - [Xbox 0x02FD HID-BPF descriptor fixup](docs/decisions/xbox-02fd-hid-bpf.md)
  - [Per-button remapping GUI & bindings](docs/decisions/button-binding-ui.md)
  - [SDL gamepad database import](docs/decisions/sdl-gamecontrollerdb.md)
- [Troubleshooting / known issues](docs/troubleshooting.md)
- Runbooks:
  - [Installation](runbooks/install.md)
  - [Update](runbooks/update.md)
  - [Verify a remap](runbooks/verify-remapping.md)
  - [Verify multi-controller operation](runbooks/verify-multi-controller.md)

## Contributing

Contributions are welcome - see [CONTRIBUTING.md](CONTRIBUTING.md) for the commit format,
validation script, and documentation conventions.

## License

[MIT](LICENSE)
