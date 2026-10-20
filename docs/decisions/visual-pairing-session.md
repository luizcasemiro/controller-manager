# Decision: Visual Bluetooth Pairing Session

## Context

Controller Manager deliberately knows only about controllers the kernel already exposes
through evdev. It does not discover, pair or trust Bluetooth devices. Users still had to
leave the couch, open a Bluetooth applet and pair each pad manually before the tray would
see it.

KDE Connect is already the couch-accessible trigger used for Game Mode. The new command
needs to combine three jobs without turning the tray daemon into a 60-second Bluetooth
scanner:

1. activate the existing Game Mode (TV and audio);
2. pair, trust and connect Bluetooth gamepads, showing progress on the TV;
3. open Heroic in console mode only after at least one controller is ready.

## Decision

A separate GTK3 process, `controller-setup.py`, owns the whole setup session:

- **Separate process.** The daemon keeps its two-second hotplug loop, tray menu and
  remapper untouched. A slow or failed scan can never freeze the tray or block mode
  changes.
- **BlueZ over D-Bus, not `bluetoothctl` output.** Device properties stay structured and
  a temporary `org.bluez.Agent1` can auto-confirm only the active candidate.
- **Conservative gamepad filter.** Icon and Bluetooth Class of Device win over the name.
  Keyboards, mice, phones, audio devices and unknown names are never paired
  automatically.
- **One success stops the window.** The first newly connected controller stops discovery
  and enables **Abrir Heroic**. **Conectar mais controles** starts another explicit
  60-second window; already-ready pads are excluded from the success condition.
- **The dashboard is couch-operable.** A non-grabbing evdev listener reads the connected
  controller (or the daemon's virtual pad when the pad is remapped): D-pad/left stick moves
  focus, A selects, B closes, Start opens Heroic and Select scans again. It only emits
  semantic actions to the GTK main loop and never grabs the device.
- **Existing controllers count as ready but not as a new success.** A pad connected before
  the session can launch Heroic immediately, while the scan still waits for a new pad.
- **Heroic is force-closed.** A second Flatpak invocation would not reliably apply
  `--console --fullscreen` to an existing instance. The session terminates every matching
  Heroic process with `SIGTERM`, waits briefly, then uses `SIGKILL` for anything left,
  before Game Mode and discovery. Unsaved launcher state is intentionally discarded.
- **Daemon health is a gate.** After a controller is paired and again before Heroic, the
  session checks `controller-manager.service` and the `ListControllers` D-Bus API. An
  inactive or unreachable daemon is restarted once; Heroic is not released until the
  controller is listed. The status is visible in the UI.
- **Game Mode is one-way here.** Closing the dashboard does not restore monitor or audio;
  the established Game Mode off command remains the single place for that transition.
- **The KDE command is installed idempotently.** `scripts/kdeconnect-install-command.py`
  edits only the Run Command `commands=` value, backs up the file and verifies the result.
  It does not touch certificates or other plugin settings.

## Consequences

- Pairing progress is visible on the TV and does not depend on terminal output.
- A full-screen GTK process is added to the project, but only while the command runs;
  the daemon stays headless.
- Some controllers require a physical pairing confirmation; the UI reports the failure
  rather than guessing another authentication method.
- KDE Connect Run Command configuration is per paired device. The helper updates every
  `kdeconnect_runcommand/config` it finds and can be skipped in favor of manual setup.
- The feature is optional: no daemon start-up dependency or new systemd service is added.
