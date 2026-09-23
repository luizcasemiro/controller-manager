# Troubleshooting

Quick checks first:

```bash
systemctl --user status controller-manager.service
journalctl --user -u controller-manager.service -f
```

## The tray icon does not appear

The tray uses the **StatusNotifierItem** specification, which needs a tray host that
implements it. Cinnamon and Linux Mint have no such host, but they are handled: the daemon
serves the same menu as an `XApp.StatusIcon` instead, which Mint's panel `xapp-status`
applet displays (see [the XApp fallback decision](decisions/xapp-tray-fallback.md)). So on
Mint the tray works out of the box; on other desktops without an SNI host (some minimal
window managers) an extension may be needed.

Confirm the daemon is running (above) and that your desktop has a working
StatusNotifierItem tray. The XApp fallback only appears while the `xapp-status` applet is
present on a Mint panel.

Note for developers: the item is registered with the `StatusNotifierWatcher` using its
**object path**, not a bus name. Registering with a bus name fails silently and the icon
never appears.

## `Permission denied` on `/dev/uinput`

The daemon needs write access to `/dev/uinput` to create virtual devices. On most
distributions this is granted by group membership. Verify access:

```bash
ls -l /dev/uinput
```

If your user lacks access, add the appropriate group (distribution-dependent) and
re-log in.

## Double input / wrong glyphs in a remapped mode

The physical controller is still reaching the application through its raw HID node - the
[hidraw gate](decisions/hidraw-gate.md) is not in effect. Check:

1. **Is the gate helper installed?** Without it, gating is a silent no-op.

   ```bash
   ls -l /usr/local/bin/controller-hidraw-gate /etc/sudoers.d/controller-hidraw
   ```

2. **Is the udev rule installed and udev reloaded?** Since gate v2, pre-existing fds are
   revoked by a driver rebind and new nodes are born gated - but only with
   `/etc/udev/rules.d/72-controller-manager.rules` in place:

   ```bash
   ls -l /etc/udev/rules.d/72-controller-manager.rules
   ```

Confirm the gate state directly (replace the node with your controller's, see the
[verification runbook](../runbooks/verify-remapping.md)):

```bash
ls -l /dev/hidrawN     # remapped -> c--------- (000, no trailing '+')
                       # native   -> crw-rw----+ / crw-rw-rw-+ (open, seat ACL)
```

The exact open permissions of a native node come from the distribution's controller udev
rules (usually `0660`/`0666` plus a seat `uaccess` ACL, shown as a trailing `+`); what
matters is gated = `000` with no ACL vs. native = readable with the seat ACL.

## A fifth (phantom) controller appears when two DualSense are both emulated

Symptom: with two DualSense both in `ps5-xbox`, Steam's *Settings -> Controller* lists
**five** controllers - the two native Xbox pads, the two emulated virtual Xbox pads, and
one extra **PlayStation** pad. It appears only once the *second* pad is emulated (one
emulated = four, clean) and is always the second, whichever physical pad that is.

This is a **known, inert limitation**, not a gate failure. The hidraw gate holds on both
pads; the phantom is the second pad's **evdev** node, which Steam re-opens and lists during
the driver rebind of the second `ps5-xbox` transition. The remapper's `EVIOCGRAB` still
holds it, so the phantom produces **no input** (no double input) - it is a dead list entry.
Full analysis and why there is no lightweight fix:
[the evdev enumeration leak](decisions/evdev-enumeration-leak.md).

Confirm it is the harmless case - both hidraw nodes gated, the phantom grabbed and inert:

```bash
# both physical DualSense hidraw nodes must be 000 (gate holds on both):
for d in /sys/bus/hid/devices/*:054C:*; do
  sed -n 's/^HID_UNIQ=/uniq /p' "$d/uevent"
  for h in "$d"/hidraw/hidraw*; do ls -l "/dev/$(basename "$h")"; done
done

# the phantom's evdev node is grabbed -> no events leak to Steam:
evtest /dev/input/eventN   # prints "This device is grabbed by another process"
```

If instead the phantom *reacts* to input in a game (double input) or steals a player slot,
that is the functional case the decision doc gates its fix on - worth reporting.

## The DualSense lightbar shows the wrong colour after a reconnect

Symptom: the pad is correctly remapped (e.g. output as Xbox, raw HID gated) but the lightbar
stays **blue** (the firmware default) instead of the mode colour - most visibly after
toggling the controller off/on several times. The mode itself is fine; only the colour cue
is stale, which can make a mode switch *look* like it did nothing.

Cause: on a reconnect the hid-playstation `...:rgb:indicator` LED node is renumbered (its name
follows the `inputN` counter, which bumps even when the `/dev/input/eventX` path is reused),
and the controller firmware resets the lightbar to its default on power-cycle. The daemon
re-resolves the live node and re-asserts the mode colour on the next monitor tick, so it
normally corrects itself within ~2 s. Confirm the kernel LED matches the mode:

```bash
# find the DualSense indicator LED, then read its stored colour
for l in /sys/class/leds/*:rgb:indicator; do
  echo "$l -> $(cat "$l/multi_intensity")  (brightness $(cat "$l/brightness"))"
done
# ps5-native = 0 0 255 (blue) ; ps5-xbox = 0 128 0 (green)
```

If `multi_intensity` already matches the mode but the **hardware** lightbar does not
(typically completely dark), a raw-HID writer - Steam Input is the usual one - has set the
pad's lightbar-setup "light out" flag: the firmware then ignores plain colour writes until
the driver re-probes. Check who holds the pad's hidraw node and see
[Steam coexistence](decisions/steam-coexistence.md) for the required Steam setup; the
daemon rearms the pad automatically (driver rebind) once the last holder exits, which
clears the latch. If the LED value itself is wrong, check that `controller-led` is installed and
authorised (`/etc/sudoers.d/controller-hidraw`) - without it the colour write is a silent
no-op.

## A remapped controller has working sticks but dead buttons

This is the Xbox->PlayStation direction, which is intentionally **not offered** for exactly
this reason - see [output protocol constraints](decisions/output-protocol-constraints.md).
The supported directions are PlayStation->Xbox and the two native modes.

## A Bluetooth controller is paired and connected but never appears in the tray

If a pad shows as `Connected` in `bluetoothctl` (with the HID service) yet is listed by no
game and never appears in the tray, first check whether it produced an input device at all:

```bash
ls /dev/input/js*                          # is there a js node for it?
for d in /sys/class/input/js*; do echo "$d $(cat $d/device/name)"; done
```

No node means no driver bound. A common cause on some Xbox pads is a **malformed HID report
descriptor** the kernel refuses to parse:

```bash
sudo dmesg | grep -i 'unbalanced collection'
# playstation/xpadneo 0005:045E:02FD.*: unbalanced collection at end of report description
# ... probe with driver <x> failed with error -22
```

For the specific Bluetooth Xbox pad reporting product `0x02FD` this is a firmware bug fixed
by the bundled HID-BPF descriptor fixup - make sure it installed (`install.sh` skips it with
a warning if the build toolchain is missing):

```bash
udev-hid-bpf list-devices | grep -i xbox   # should show the pad matched to the fixup
```

See [the Xbox 0x02FD HID-BPF decision](decisions/xbox-02fd-hid-bpf.md) for the full story
and manual build/install steps.

## Buttons are mapped to the wrong positions

Some controllers expose a non-standard evdev button layout. The remapper carries a
per-source quirk table that translates such layouts to standard codes. A new controller
model with an unusual layout may need an entry added to that table.

## Another remapper is fighting for the device

If a separate userspace remapper grabs controllers system-wide (translating them into a
virtual device of its own, or into keyboard/mouse events), applications will see mixed or
duplicated input. Disable other system-wide controller remappers so this daemon has sole
control of the physical devices.

## An orphaned virtual controller lingers

A correctly running daemon removes its virtual device as soon as a controller returns to
native mode. If you find a stale virtual pad, restart the service - it clears any leftover
state:

```bash
systemctl --user restart controller-manager.service
```

If you can reproduce a lingering virtual device during normal mode switching, that is a
bug worth reporting; see [the remapping engine decision](decisions/remapping-engine.md)
for the lifecycle that should prevent it.
