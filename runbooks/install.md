# Runbook: Installation

Deploy the daemon, the systemd user service, and the root-owned hidraw gate into their
live locations.

## Preconditions

- A modern Linux desktop with a **user systemd session** and a StatusNotifierItem tray.
- Runtime packages present: `python3-evdev`, `python3-dbus` (`dbus-python`),
  `python3-gi` (PyGObject).
- Write access to `/dev/uinput` (group membership on most distributions).
- `sudo` rights for the privileged step (installing the helper and sudoers rule).
- The repository checked out locally.

## Steps

```bash
cd controller-manager
./install.sh
```

`install.sh` performs:

1. **User space** (no privileges):
   - `controller-manager.py` -> `~/.local/bin/controller-manager.py`
   - `controller-gui.py` -> `~/.local/bin/controller-gui.py`
   - `controller-setup.py` -> `~/.local/bin/controller-setup.py`
   - `controller-overlay.py` -> `~/.local/bin/controller-overlay.py`
   - `controller_setup/*.py` -> `~/.local/bin/controller_setup/`
   - `controller-manager.service` -> `~/.config/systemd/user/controller-manager.service`
2. **Root space** (asks for a password):
   - `controller-hidraw-gate` -> `/usr/local/bin/controller-hidraw-gate` (root, `0755`)
   - `controller-led` -> `/usr/local/bin/controller-led` (root, `0755`)
   - `72-controller-manager.rules` -> `/etc/udev/rules.d/` (root, `0644`), then
     `udevadm control --reload` - gated hidraw nodes must be born inaccessible
   - `controller-hidraw.sudoers` -> `/etc/sudoers.d/controller-hidraw` (root, `0440`)
   - `visudo -c` to validate the sudoers files
3. **Service**: `daemon-reload`, `enable` (autostart with the session) and restart, then
   prints the active state.

If Steam is used on the machine, set **Settings -> Controller -> PlayStation controller
support** to **"Enabled in Games w/o Support"** and **Xbox controller support off** in the
Steam client (turning PlayStation support off entirely is the one broken state) - see
[Steam coexistence](../docs/decisions/steam-coexistence.md) for why and what it costs.

## Verification

```bash
# Service is running
systemctl --user is-active controller-manager.service        # -> active

# Privileged helper is in place and root-owned
ls -l /usr/local/bin/controller-hidraw-gate                  # -> -rwxr-xr-x root root
sudo visudo -cf /etc/sudoers.d/controller-hidraw             # -> parsed OK

# Optional pairing/overlay commands are installed
ls -l ~/.local/bin/controller-setup.py
ls -l ~/.local/bin/controller-overlay.py
ls -l ~/.local/bin/controller_setup/
```

The tray icon should appear and list connected controllers with their modes. To confirm
the remapping path end to end, follow [verify a remap](verify-remapping.md).

## Failure modes

| Symptom | Check |
|---|---|
| `install.sh` aborts at the root step | `sudo` requires a password / is unavailable; run it in an interactive shell |
| Service is `failed` after restart | `journalctl --user -u controller-manager.service -n 30` - usually a missing Python dependency |
| Tray icon never appears | StatusNotifierItem tray host missing - see [troubleshooting](../docs/troubleshooting.md) |
| Remap produces double input | hidraw gate not installed or active - see [the hidraw gate](../docs/decisions/hidraw-gate.md) |

## Rollback

```bash
# Stop and disable the service
systemctl --user disable --now controller-manager.service

# Remove user-space files
rm -f ~/.local/bin/controller-manager.py \
      ~/.local/bin/controller-gui.py \
      ~/.local/bin/controller-setup.py \
      ~/.local/bin/controller-overlay.py \
      ~/.config/systemd/user/controller-manager.service
rm -rf ~/.local/bin/controller_setup
systemctl --user daemon-reload

# Remove root-space files
sudo rm -f /usr/local/bin/controller-hidraw-gate /etc/sudoers.d/controller-hidraw \
           /etc/udev/rules.d/72-controller-manager.rules
sudo udevadm control --reload
```

The configuration at `~/.config/controller-modes.json` is left in place; delete it too for
a clean slate.
