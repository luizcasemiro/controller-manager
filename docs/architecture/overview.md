# Architecture Overview

Controller Manager is a single user-space daemon (`controller-manager.py`) plus two small
root-owned helpers (`controller-hidraw-gate`, `controller-led`) and one udev rule. The
daemon detects controllers, applies a per-device mode, and serves a tray menu over D-Bus.
The helpers perform the privileged actions the daemon needs: gating raw HID access and
driving the DualSense lightbar.

```
                         controller-manager.py (systemd --user)
                         ┌──────────────────────────────────────────────┐
 /dev/input/event*  ───> │ ControllerManager   (detect, hotplug, modes)  │
                         │   └─ ControllerInstance (one per device)       │
                         │        └─ Remapper thread (grab + uinput)      │
                         │                                                │
 D-Bus (session) <─────> │ TrayIcon (StatusNotifierItem)                  │
                         │ DbusmenuServer (com.canonical.dbusmenu)        │
                         └───────────────┬────────────────────────────────┘
                                         │ sudo -n (NOPASSWD, scoped)
                                         v
                         controller-hidraw-gate ──> identity-keyed gate:
                         controller-led            marker + driver rebind
                                  ^                + lightbar colour
                                  │ udev-check (PROGRAM)
                         72-controller-manager.rules: gated hidraw
                         nodes are BORN inaccessible (MODE 0000)
```

## Component map

| Component | Path (installed) | Responsibility |
|---|---|---|
| Daemon | `~/.local/bin/controller-manager.py` | detection, remapping, tray, hotplug |
| Service | `~/.config/systemd/user/controller-manager.service` | autostart with the desktop session |
| Config | `~/.config/controller-modes.json` | persisted mode per device |
| HID gate | `/usr/local/bin/controller-hidraw-gate` | identity-keyed gating: markers, driver rebind (fd revoke + lightbar rearm), chmod |
| Gate udev rule | `/etc/udev/rules.d/72-controller-manager.rules` | gated hidraw nodes are born `MODE 0000`, without the uaccess tag |
| Gate state | `/run/controller-manager/gated/<uniq>` | marker per gated pad (tmpfs - reboots fail open) |
| LED helper | `/usr/local/bin/controller-led` | privileged write of a Sony `:rgb:indicator` lightbar |
| Sudoers rule | `/etc/sudoers.d/controller-hidraw` | scoped NOPASSWD for the two helpers |

## Detection and the device model

`ControllerManager` runs a monitor thread that scans `evdev.list_devices()` every two
seconds. A device is treated as a controller when:

1. its **vendor id** is known (`0x054c` -> PlayStation, `0x045e` -> Xbox), and
2. it advertises a **gamepad capability** - either `BTN_GAMEPAD` or `BTN_SOUTH` (whichever the driver exposes).

Recognising by vendor plus capability - rather than a fixed product-ID list - means
unlisted models of a known vendor still work, and non-gamepad nodes that share a vendor id
(headset, motion sensor, touchpad) are excluded. Product IDs are used only to choose a
nicer display name. Software-emulated pads that advertise a real Sony/Microsoft vendor id
(Sunshine/inputtino creates them over uhid for stream clients) are excluded by their
synthetic `phys` / "(virtual)" name - adopting one would remap a stream client's input.

Each detected device becomes a `ControllerInstance`, keyed by its **stable identity**
(`uniq`, e.g. the Bluetooth MAC; else the physical attachment point `phys`, which keeps two
identical serial-less USB pads apart; else `vendor:product`) - the same key configuration and the
tray label use, see [per-device identity](../decisions/per-device-identity.md). Keying on
identity rather than the `/dev/input/eventX` path matters because a Bluetooth pad
re-registers on a *new* evdev/hidraw node after every reconnect (e.g. when it is un/re-paired
for console use): the reconcile pass recognises the same identity and **rebinds** the instance
to the new node in place - restarting only the remapper - instead of tearing it down and
rebuilding it, which would churn the tray menu into stuck-disabled items. The same re-assert
also runs when a pad returns on a *reused* `eventX` path (detected as a previously-absent
instance reappearing, not a path change): the device underneath - and thus its remapper grab
and lightbar - is new even though the path number is not. A pad that vanishes is dropped only
after a short grace period, so a brief reconnect blip never removes it.

## Modes

| Mode | Family | Effect |
|---|---|---|
| `ps5-native` | PlayStation | pass-through, no grab |
| `ps5-xbox` | PlayStation | grab + emit a virtual Xbox 360 pad |
| `xbox-native` | Xbox | pass-through, no grab |

The modes offered per family are defined by `MODES_FOR_FAMILY`. The Xbox->PlayStation
direction is deliberately omitted (see
[output protocol constraints](../decisions/output-protocol-constraints.md)). A persisted
mode that is no longer offered for a family falls back to that family's native default, so
removing a mode never leaves a controller stuck applying it.

## The remapper

A remap runs in its own `Remapper` thread. On start it:

1. opens the source device and copies its capabilities (dropping `EV_SYN`/`EV_FF`);
2. creates a **`uinput`** virtual device with the target identity (`VIRTUAL_XBOX`);
3. grabs the source with `EVIOCGRAB` for exclusive access;
4. forwards `EV_KEY` / `EV_ABS` / `EV_REL` events to the virtual device.

Two details matter:

- **Translated capabilities are advertised.** Some controllers expose a non-standard
  evdev button layout. The remapper carries a per-source quirk map and advertises the
  *translated* (standard) key codes on the virtual device, so the consuming side maps the
  target identity against standard codes rather than the source's raw ones.
- **The loop is interruptible.** The event loop uses `selectors` over the source fd plus a
  self-pipe. `stop()` writes to the pipe, waking the loop immediately even when the
  controller is idle. This replaced an earlier design that closed the source fd from
  another thread - see [the remapping engine decision](../decisions/remapping-engine.md)
  for why that leaked.

The virtual device is created before the grab is attempted; if the grab fails, the virtual
device is closed so it cannot linger. Virtual devices are excluded from the detection scan
(by name and by path) so the daemon never tries to remap its own output.

## The hidraw gate

An `evdev` grab hides a device only on the evdev layer. Some applications read controllers
straight from `/dev/hidraw*`, bypassing the grab, which causes double input in a remapped
mode - and a `chmod` alone cannot stop a process that opened the node *before* the gate
(Steam Input opens every pad on connect and keeps the fd). The gate is therefore keyed by
the pad's **stable identity** and works in three parts: a marker file records the gate
state, a driver **rebind** revokes every existing fd (and resets the DualSense lightbar
latch), and a udev rule makes the reborn hidraw nodes be **born** inaccessible (`MODE
0000`, no uaccess ACL) so nothing can re-open them. Being identity-keyed, the gate
survives reconnects (the pad returns born-gated) and gates two identical controllers
independently; a reboot clears all markers, so the system always starts fail-open. Full
rationale and the rule-ordering pitfalls: [the hidraw gate](../decisions/hidraw-gate.md).

## The lightbar (DualSense)

A DualSense mode is signalled on the controller's lightbar: blue for `ps5-native`, green
for `ps5-xbox`. The colour is driven through the kernel **LED class** - the
hid-playstation `...:rgb:indicator` multicolor node, written by the `controller-led` helper -
not via raw hidraw output reports, which race the driver and get dropped over Bluetooth
(leaving the lightbar on its firmware default). The kernel does the USB/BT report framing,
so the colour is reliable.

The lightbar is fragile across reconnects for two reasons, and the daemon defends against
both:

- **The LED node renumbers.** Its name derives from the `inputN` instance counter, which
  bumps on *every* reconnect (`input38` -> `input47` -> ...) - even when the `/dev/input/eventX`
  path is reused, so a path-keyed check would miss it. `_apply_led()` therefore never trusts
  a cached node name: it re-resolves the `:rgb:indicator` node from the live evdev path on
  every write, so a colour can never land on a stale, now-dead node.
  `ControllerInstance.refresh_led()` (run each monitor tick, outside the reconcile lock
  because the write may shell out to `sudo`) repaints when the live node differs from the one
  last painted - covering a renumber that happens without a polled absence.
- **The firmware resets the lightbar on power-cycle.** A pad toggled off/on returns on its
  firmware-default blue and un-remapped (its old grab died with the disconnect). Because the
  evdev path is often reused, the daemon detects such a reconnect by a *presence transition*
  (a previously absent instance reappearing) rather than a path change, and re-asserts the
  whole mode - restarting the remapper, re-gating the hidraw node and repainting the lightbar.

In `ps5-native` the lightbar has a second legitimate writer: applications with raw HID
access (games with native DualSense support; and the Steam client itself, which with the
required "Enabled in Games w/o Support" setting holds every pad permanently). Because that
permanent hold no longer separates an idle desktop from a running game, the daemon
*classifies* game-vs-idle rather than merely counting holders - while a
game holds the pad the lightbar is its; when the last holder exits, the daemon rearms the
pad (driver rebind - a raw writer may have firmware-latched the lightbar dark) and
repaints the resting blue; a reopen shortly after one of our own rebinds (Steam writes its
defaults once and goes quiet) is outlasted by a delayed repaint. In the remap mode none of
this is needed - the gate guarantees exclusive LED ownership, so green simply holds. Full
policy: [Steam coexistence](../decisions/steam-coexistence.md).

## The tray

The tray is a `StatusNotifierItem` served directly over D-Bus, with its menu provided by a
`com.canonical.dbusmenu` object - no GUI toolkit is linked. The item is registered with
the `StatusNotifierWatcher` using its **object path**. The menu model is built once per
state change and cached; structural changes (hotplug) allocate **fresh, never-recycled
item ids**, while display-only changes (the radio checkmark) keep their ids and are pushed
as `ItemsPropertiesUpdated` deltas - hosts that cache item properties per id (GNOME's
appindicator extension) would otherwise keep stale properties for a reused id and render
entries stuck disabled. Each connected controller becomes a section of radio items;
selecting one calls back into `ControllerManager.set_mode()` via the cached id lookup.
Full rationale: [dbusmenu item model](../decisions/tray-menu-model.md).

The menu itself is defined once as plain semantic tuples (`_semantic_menu_items`) and then
rendered per host, so every host exposes identical choices and click targets. On Cinnamon
and Linux Mint - which never run a `StatusNotifierWatcher` - the daemon re-serves the same
menu as an `XApp.StatusIcon` (`org.x.StatusIcon.ctrlmgr`), displayed by the panel's
`xapp-status` applet. That fallback stays hidden while an SNI watcher is present, so a
desktop supporting both never shows two icons; the menu pops in-process as a fresh
GTK+3 `Gtk.Menu` per click (the daemon's only GTK linkage), rebuilding from the current
controller state each time. Details: [XApp fallback](../decisions/xapp-tray-fallback.md).

The tray also hosts the **per-button remapping GUI** entry ("Remap buttons..." per
controller), which hands off to a separate GTK process. The daemon stays headless; it
exposes a second D-Bus object (`/ControllerManager`, interface
`org.ctrlmgr.ControllerManager1`) that the GUI talks to:

- **Discovery / read-back:** `ListControllers`, `GetButtons` (the pad's live `EV_KEY`
  codes), `GetTargetButtons` (the output identity's codes, for the picker), `GetBindings`.
- **Editing:** `SetBinding(ident, src, dst)` applies immediately (recomposes the remap and
  re-asserts the mode under the lock) and persists to the config's `_bindings` key;
  `ResetBindings(ident)` clears a controller; `BindingsChanged` signals the GUI to
  re-read.
- **Capture:** `CaptureStart`/`CaptureCancel` plus the `CaptureResult` signal. Capturing
  releases that pad's remap grab and pauses re-asserts for it (the monitor skips a pad
  whose ident is mid-capture), waits for the next physical button-down on the real device,
  and re-asserts the mode afterward - so the recorded press goes to the pad, not to the
  virtual device the grab would otherwise forward it through. Composition and semantics:
  [button binding UI](../decisions/button-binding-ui.md).

## Lifecycle and persistence

- **Hotplug:** the monitor adds instances for new devices and stops instances for removed
  ones; stopping always restores the hidraw node so a gate never lingers after a
  disconnect.
- **Persistence:** `set_mode()` writes the chosen mode to `controller-modes.json`, keyed
  by the device's stable identity (`uniq`, else `phys:...`). Per-button bindings
  (`set_binding()`/`reset_bindings()`) live in the same file under the reserved `_bindings`
  key, keyed the same way.
- **Shutdown:** on `SIGTERM`/`SIGINT` the daemon stops every instance, releasing grabs and
  restoring all gated nodes.
