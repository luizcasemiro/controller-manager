#!/usr/bin/env python3
"""
Controller Manager - unified tray + per-controller remapping.

Supports:
  PlayStation DualSense  ->  native  |  output as Xbox 360
  Xbox controller        ->  native  (xbox->ps5 unusable in apps; see
                                       docs/decisions/output-protocol-constraints.md)
Multiple controllers simultaneously, each with its own mode.
"""

import os, sys, json, signal, threading, time, subprocess, glob, selectors
import traceback
import evdev
from evdev import UInput, ecodes as e
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# ── Constants ────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.expanduser("~/.config/controller-modes.json")

# (vendor, product) -> (display name, controller family)
# Recognise controllers by USB/BT vendor + gamepad capability rather than a
# fixed product-ID list (which keeps missing models - e.g. the BT Xbox pad
# 045e:02e0). Vendor -> controller family.
VENDOR_FAMILY = {
    0x054c: "ps5",    # Sony
    0x045e: "xbox",   # Microsoft
}

# Optional nicer display names per (vendor, product); falls back to the kernel
# device name when unknown.
#
# 0x028E is deliberately absent: xpadneo rewrites the product id of the
# Bluetooth Xbox pads it binds to 0x028E ("pretending XB1S Windows wireless
# mode"), so both an Xbox One S (0x02E0) and a newer pad (0x02FD) present as
# 0x028E once connected - and 0x028E is also the real wired Xbox 360 id. Keying
# a name on it would mislabel every Bluetooth Xbox pad as "Xbox 360", so we let
# it fall back to the kernel name ("Xbox Wireless Controller"), which is
# accurate. Two such pads then share a base name and are disambiguated by
# player number (see _inst_label). Entries below are for USB-connected pads,
# whose ids xpadneo does not rewrite.
CONTROLLER_NAMES = {
    (0x054c, 0x0ce6): "DualSense",
    (0x054c, 0x0df2): "DualSense Edge",
    (0x045e, 0x02ea): "Xbox One",
    (0x045e, 0x02fd): "Xbox One S",
    (0x045e, 0x02e0): "Xbox One S (BT)",
    (0x045e, 0x0b12): "Xbox Series X/S",
    (0x045e, 0x0b13): "Xbox Series X/S (BT)",
    (0x045e, 0x0b20): "Xbox Series X/S",
}

DEFAULT_MODES = {
    "ps5":  "ps5-native",
    "xbox": "xbox-native",
}

# Lightbar colors per ps5 mode (R, G, B 0-255).
# Only DualSense-family controllers have a lightbar; Xbox is always skipped.
MODE_LED = {
    "ps5-native": (0,   0, 255),   # blue  - native PlayStation feel
    "ps5-xbox":   (0, 128,   0),   # green - signals active Xbox emulation
}

MODE_LABELS = {
    "ps5-native":  "DualSense mode",
    "ps5-xbox":    "Emulate Xbox",
    "xbox-native": "Xbox mode",
    "xbox-ps5":    "Emulate PS5",
}

# Modes offered in the tray menu per family. "xbox-ps5" is intentionally NOT
# listed: the evdev translation is correct, but applications read PlayStation
# VID:PIDs via a HID API (hidraw) and a virtual uinput pad has no hidraw node,
# so only axes (not buttons) reach applications - unusable in practice. See
# docs/decisions/output-protocol-constraints.md. The translation infrastructure
# (VIRTUAL_PS5 / QUIRK_BUTTON_MAP / _target_for_mode) is kept so a future
# uhid-based fix can re-enable it.
MODES_FOR_FAMILY = {
    "ps5":  ["ps5-native",  "ps5-xbox"],
    "xbox": ["xbox-native"],
}

VIRTUAL_XBOX = dict(
    name="Microsoft X-Box 360 pad",
    vendor=0x045e, product=0x028e, version=0x0114,
    bustype=e.BUS_USB,
)
VIRTUAL_PS5 = dict(
    name="Sony Interactive Entertainment DualSense Wireless Controller",
    vendor=0x054c, product=0x0ce6, version=0x8100,
    bustype=e.BUS_BLUETOOTH,
)

VIRTUAL_NAMES = {VIRTUAL_XBOX["name"], VIRTUAL_PS5["name"]}

# Analog-stick Y axes inverted by the invert_y_axes config option (the values
# are mirrored via the axis' min/max before they reach uinput, so the range is
# preserved). Only these two change; X axes, triggers and buttons pass through
# untouched.
INVERT_Y_AXES = (e.ABS_Y, e.ABS_RY)

# Some controllers expose a non-standard evdev button layout via their kernel
# driver; a raw passthrough then mismatches what SDL expects for the *target*
# identity (buttons land wrong / dead). Per source (vendor, product): source
# evdev code -> standard gamepad code. Codes not listed pass through unchanged.
# Empirically mapped by position (see docs/decisions/remapping-engine.md).
QUIRK_BUTTON_MAP = {
    (0x045e, 0x02e0): {            # Xbox One S - old Bluetooth firmware
        e.BTN_C:    e.BTN_WEST,    # X
        e.BTN_WEST: e.BTN_TL,      # LB
        e.BTN_Z:    e.BTN_TR,      # RB
        e.BTN_TL:   e.BTN_SELECT,  # View
        e.BTN_TR:   e.BTN_START,   # Menu
        e.KEY_MENU: e.BTN_MODE,    # Xbox / Guide
        e.BTN_TL2:  e.BTN_THUMBL,  # L3
        e.BTN_TR2:  e.BTN_THUMBR,  # R3
        # A->BTN_A(=SOUTH), B->BTN_B(=EAST), Y->BTN_NORTH already standard.
    },
}

def compose_button_maps(quirk, user=None):
    """Combine the driver quirk map and the per-controller user bindings into
    the single dict the Remapper applies per event.

    user holds the buttons the user explicitly rebound, keyed by the PHYSICAL
    device code (as GetButtons reports) and valued at the OUTPUT identity's
    codes (as GetTargetButtons reports). A present key completely overrides the
    quirk remap for that physical button; any other code - or a whole empty
    user map - keeps the plain program mapping. None when both layers are
    empty (plain passthrough)."""
    quirk, user = quirk or {}, user or {}
    combined = {}
    for src, dst in user.items():
        combined[src] = dst                 # user bind wins outright
    for src, std in quirk.items():          # quirk fills the rest
        combined.setdefault(src, std)
    return combined or None

# User-facing names for physical buttons, per controller family. The GUI shows
# these so a user can bind "Circle" on the PS pad to "B" on the virtual Xbox pad
# without decoding raw evcodes. A code with no entry falls back to its evdev name.
PHYS_BTN_LABELS = {
    "ps5": {
        e.BTN_SOUTH:  "Cross",
        e.BTN_EAST:   "Circle",
        e.BTN_NORTH:  "Triangle",
        e.BTN_WEST:   "Square",
        e.BTN_TL:     "L1",
        e.BTN_TR:     "R1",
        e.BTN_TL2:    "L2",
        e.BTN_TR2:    "R2",
        e.BTN_SELECT: "Share",
        e.BTN_START:  "Options",
        e.BTN_MODE:   "PS",
        e.BTN_THUMBL: "L3",
        e.BTN_THUMBR: "R3",
        e.BTN_TOUCH:  "Touchpad",
        e.KEY_MENU:   "PS",
    },
    "xbox": {
        e.BTN_SOUTH:  "A",
        e.BTN_EAST:   "B",
        e.BTN_NORTH:  "X",
        e.BTN_WEST:   "Y",
        e.BTN_TL:     "LB",
        e.BTN_TR:     "RB",
        e.BTN_TL2:    "LT",
        e.BTN_TR2:    "RT",
        e.BTN_SELECT: "View",
        e.BTN_START:  "Menu",
        e.BTN_MODE:   "Guide",
        e.BTN_THUMBL: "L3",
        e.BTN_THUMBR: "R3",
    },
}

# Destination identity of the ps5-xbox remap is the virtual Xbox pad, whose
# buttons carry xpad's layout - the same codes and names as the xbox column.
TARGET_BTN_LABELS = {
    VIRTUAL_XBOX["name"]: PHYS_BTN_LABELS["xbox"],
}

def action_label(code, family=None, target_name=None):
    """Readable name of a button code for the binding GUI: the physical pad's
    family label when given, else the target identity's, else the evdev name."""
    if family is not None:
        got = PHYS_BTN_LABELS.get(family, {}).get(code)
        if got:
            return got
    if target_name is not None:
        got = TARGET_BTN_LABELS.get(target_name, {}).get(code)
        if got:
            return got
    return e.BTN.get(code) or e.KEY.get(code, f"code {code}")

# DBus names
BUS_NAME  = "org.kde.StatusNotifierItem-ctrlmgr-1"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
QUIT_ID   = 9999

# Custom API surface for the binding GUI (a separate GTK process): dedicated
# well-known bus name, object path and interface are ours.
CTRLMGR_BUS   = "org.ctrlmgr.ControllerManager1"
CTRLMGR_PATH  = "/ControllerManager"
CTRLMGR_IFACE = "org.ctrlmgr.ControllerManager1"

# Path of the GUI helper the tray launches (user-space, installed by install.sh
# next to the daemon itself). Launching it spawns a NEW process: the daemon is
# deliberately GUI-free (see docs/architecture/overview.md).
GUI_SCRIPT = os.path.expanduser("~/.local/bin/controller-gui.py")

# Config key holding per-controller user button bindings, mirroring the "_"-prefixed
# manager-controlled keys (_players): { <ident>: { src_evcode: dst_evcode } }. Only
# the buttons a user actually rebound are stored; everything else falls through to
# the program's quirk remap (see compose_button_maps).
BINDINGS_KEY = "_bindings"


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.loads(f.read())
    except Exception:
        return {}

def save_config(cfg):
    # Atomic write: a crash / OOM-kill / full disk mid-write must not leave a
    # truncated file - load_config swallows a parse error and returns {}, which
    # would silently drop every persisted mode and player number. Write a temp
    # file in the same directory (same filesystem, so os.replace is atomic),
    # fsync it, then rename over the target.
    d = os.path.dirname(CONFIG_FILE)
    os.makedirs(d, exist_ok=True)
    tmp = f"{CONFIG_FILE}.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(cfg, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


# ── hidraw gate ──────────────────────────────────────────────────────────────
#
# An evdev grab (EVIOCGRAB) only blocks the evdev node. Some applications
# read game controllers straight from /dev/hidraw*, so a remapped DualSense
# would still reach the application as a PlayStation pad - in parallel with our
# virtual Xbox pad - causing double input. We therefore chmod the physical
# controller's hidraw node to 000 for the duration of any remap. This is
# system-wide and launcher-agnostic: it hides the pad from every application
# alike. The chmod needs root, delegated to a tightly-scoped helper via a
# NOPASSWD sudoers rule. If the helper/rule is absent, this is a silent no-op.
#
# The gate operates on a SPECIFIC hidraw node (not a VID:PID), so two identical
# controllers (same VID:PID, different physical device) can be gated indepen-
# dently - only the remapped one is hidden.

GATE_BIN = "/usr/local/bin/controller-hidraw-gate"
LED_BIN  = "/usr/local/bin/controller-led"

def hidraw_for_event(ev_path):
    """Return all /dev/hidrawN nodes for the HID device behind an evdev node.
    Returns an empty list when the device exposes no hidraw (e.g. xpad) or when
    the pad's node has gone (ev_path None - _refresh_nodes drops the path when a
    pad does not re-enumerate in time, and a resting-colour apply can race that).
    Guarded like led_indicator_for_event, whose callers already tolerate None."""
    if not ev_path:
        return []
    base = f"/sys/class/input/{os.path.basename(ev_path)}/device"
    if not os.path.exists(base):
        return []
    hid_dir = os.path.realpath(os.path.join(base, "..", ".."))
    nodes = glob.glob(os.path.join(hid_dir, "hidraw", "hidraw*"))
    return sorted(f"/dev/{os.path.basename(n)}" for n in nodes)

def hidraw_gate(action, uniq, nodes):
    """action: 'block' | 'restore' | 'rearm', keyed by the pad's stable HID
    identity (uniq: BT MAC / serial). The v2 helper actually revokes access -
    a plain chmod cannot: a process that opened the node before the gate
    (Steam Input grabs every pad on connect) keeps a working fd. The helper
    rebinds the kernel driver on a state transition, which kills every open
    fd and re-probes the pad (resetting the DualSense lightbar latch); the
    device nodes RENUMBER when that happens, so callers must re-resolve them
    afterwards (ControllerInstance._refresh_nodes). Pads without a uniq fall
    back to the v1 per-node chmod (best effort, no revocation). No-op when
    the helper/sudo rule is missing."""
    if not os.path.exists(GATE_BIN):
        return
    if uniq:
        cmds = [["sudo", "-n", GATE_BIN, action, uniq]]
    else:
        legacy = {"block": "block-node", "restore": "restore-node"}.get(action)
        if not legacy or not nodes:
            return
        cmds = [["sudo", "-n", GATE_BIN, legacy, node] for node in nodes]
    for cmd in cmds:
        try:
            # timeout 10: a driver rebind (unbind + re-probe) is slower than
            # the old chmod; still far below anything a user would notice as
            # a hang, and generous enough for a busy USB/BT stack.
            subprocess.run(
                cmd, timeout=10, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as ex:
            print(f"hidraw_gate: {' '.join(cmd[2:])} failed: {ex}",
                  file=sys.stderr)


def led_indicator_for_event(ev_path):
    """Return the DualSense RGB-indicator LED name (e.g. 'input38:rgb:indicator')
    for the HID device behind an evdev node, or None. The hid-playstation driver
    exposes the lightbar as a multicolor LED; driving it there lets the kernel
    do the USB/BT output-report framing, so the colour survives BT reconnects -
    unlike raw hidraw writes, which race the driver and get dropped over BT."""
    if not ev_path:      # instance mid-rebind: node not re-resolved yet
        return None
    base = f"/sys/class/input/{os.path.basename(ev_path)}/device"
    if not os.path.exists(base):
        return None
    hid_dir = os.path.realpath(os.path.join(base, "..", ".."))
    nodes = glob.glob(os.path.join(hid_dir, "leds", "*:rgb:indicator"))
    return os.path.basename(nodes[0]) if nodes else None

def led_set_raw(hidraw_nodes, rgb):
    """Set the DualSense lightbar via a raw HID output report (root helper).
    Unlike a kernel LED-class write, a raw colour report also clears the pad's
    firmware 'light out' latch - the state Steam Input leaves behind while it
    holds the pad - so the colour actually reaches the hardware instead of only
    updating the sysfs LED node (which stays a physical no-op once latched).
    Tries the pad's hidraw node(s) until the helper accepts one (a DualSense
    exposes a single hidraw). No-op without a node or the helper/sudo rule."""
    if not hidraw_nodes or not os.path.exists(LED_BIN):
        return
    r, g, b = rgb
    for node in hidraw_nodes:
        try:
            res = subprocess.run(
                ["sudo", "-n", LED_BIN, "lightbar-raw", node,
                 str(r), str(g), str(b)],
                timeout=5, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if res.returncode == 0:
                return
        except Exception as ex:
            print(f"led_set_raw: {node} failed: {ex}", file=sys.stderr)


def led_set_player(prefix, player):
    """Light the pad's white player LEDs (hid-playstation ...:white:player-N)
    to the daemon's stable player number via the root helper. The pattern is
    PS5-authentic: the lit-LED count equals the player number. No-op without
    a helper, a prefix or a representable number (patterns cover 1-4)."""
    if not prefix or not player or player > 4 or not os.path.exists(LED_BIN):
        return
    try:
        subprocess.run(
            ["sudo", "-n", LED_BIN, "player", prefix, str(player)],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as ex:
        print(f"led_set_player: {prefix} failed: {ex}", file=sys.stderr)


def is_steam_client(pid):
    """True when the pid belongs to the Steam client itself (the main
    client binary, comm 'steam', is the process that opens every pad's
    hidraw for identification and Steam Input; the webhelper never does
    but is included for completeness). Games - Steam-launched or not -
    run under their own comms and count as foreign."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() in ("steam", "steamwebhelper")
    except OSError:
        return False


def steam_game_running():
    """True while any Steam-launched title is alive. Every Steam launch -
    native or Proton alike - goes through the client's wrapper chain with
    'SteamLaunch AppId=<id>' on the command line (steam-launch-wrapper and
    reaper both carry the marker and live exactly as long as the game), so
    one /proc cmdline sweep answers 'is a game running' without any Steam
    IPC. Needed because a game under Steam Input never opens the pad
    itself - the client's permanent hold is all the holder scan sees."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"SteamLaunch AppId=" in f.read():
                    return True
        except OSError:              # process exited / not ours
            continue
    return False


def hidraw_holders(nodes):
    """PIDs of other processes holding an open fd on any of the given
    /dev/hidrawN nodes. Only same-user processes are visible without
    privileges - which is exactly the population that matters here (Steam,
    games, launchers all run as the desktop user)."""
    holders = set()
    if not nodes:
        return holders
    want = set(nodes)
    me = str(os.getpid())
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and p != me]
    except OSError:
        return holders
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") in want:
                        holders.add(int(pid))
                        break
                except OSError:      # fd closed while we looked
                    continue
        except OSError:              # process exited / not ours
            continue
    return holders


# ── Controller discovery ─────────────────────────────────────────────────────

def ident_of(vendor, product, uniq, phys=""):
    """Stable identity of a physical pad across reconnects and node churn:
    the BT MAC / serial when the pad reports one; else the physical
    attachment point (distinguishes two IDENTICAL uniq-less pads, e.g. two
    USB Xbox pads on xpad - stable within a session, though not across a
    port change); else vendor:product as the last resort."""
    if uniq:
        return uniq
    if phys:
        return f"phys:{phys}"
    return f"{vendor:04x}:{product:04x}"


def scan_controllers(exclude_paths=frozenset()):
    """All connected real controllers as list of dicts. Shared by the
    manager's poll and by a single instance re-resolving its nodes after a
    gate transition, so both apply the exact same notion of 'a controller'."""
    result = []
    for path in evdev.list_devices():
        if path in exclude_paths:
            continue
        try:
            dev = evdev.InputDevice(path)
        except Exception:
            continue
        try:
            phys = dev.phys or ""
            # Our own virtual output pads: python-evdev stamps every uinput
            # device it creates with its phys marker, so that - not the name -
            # is the reliable tell. The name may only exclude when phys is
            # empty: xpad names a REAL wired Xbox 360 pad exactly like
            # VIRTUAL_XBOX, but a physical pad always carries its usb/bt
            # attachment in phys, so it stays adoptable.
            if phys == "py-evdev-uinput" or (not phys and dev.name in VIRTUAL_NAMES):
                continue
            # Software-emulated pads: Sunshine (inputtino) creates virtual
            # DualSense/Xbox pads over uhid with a REAL Sony/Microsoft VID for
            # its stream clients. Adopting one would grab and remap a stream
            # client's own input. They are recognisable by their synthetic
            # phys and their '(virtual)' kernel device name.
            if phys == "INPUTTINO_BT_LINK" or "(virtual)" in dev.name:
                continue
            family = VENDOR_FAMILY.get(dev.info.vendor)
            if not family:
                continue
            # Must be an actual gamepad - excludes headset / motion-sensor /
            # touchpad / consumer-control nodes that share the same vendor id.
            keys = dev.capabilities().get(e.EV_KEY, [])
            if e.BTN_GAMEPAD not in keys and e.BTN_SOUTH not in keys:
                continue
            name = CONTROLLER_NAMES.get(
                (dev.info.vendor, dev.info.product), dev.name)
            result.append({
                "path":    path,
                "name":    name,
                "vendor":  dev.info.vendor,
                "product": dev.info.product,
                "family":  family,
                "uniq":    dev.uniq or "",
                "phys":    phys,
                "hidraw":  hidraw_for_event(path),
            })
        finally:
            dev.close()
    return result


# ── Remapper thread ──────────────────────────────────────────────────────────

def capture_button(dev, timeout, cancel=None):
    """Block until the next EV_KEY down-press (value 1) on `dev` (an evdev
    InputDevice, or a stand-in exposing .fd/.read), up to `timeout` seconds.
    Reads in 0.25 s slices so a cancel/timeout is observed promptly. Returns
    the key code, or 0 on timeout/cancel; key releases are ignored."""
    if cancel is None:
        cancel = threading.Event()
    deadline = time.monotonic() + float(timeout)
    sel = selectors.DefaultSelector()
    try:
        sel.register(dev.fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancel.is_set():
                return 0
            if not sel.select(min(remaining, 0.25)):
                continue
            try:
                events = dev.read()
            except (BlockingIOError, OSError):
                continue
            for ev in events:
                if ev.type == e.EV_KEY and ev.value == 1:
                    return ev.code
    finally:
        sel.close()

class Remapper(threading.Thread):
    """Grabs a source controller and emits a virtual target device."""

    def __init__(self, src_path, target_spec, button_map=None, invert_y=False):
        super().__init__(daemon=True)
        self._src_path   = src_path
        self._target     = target_spec
        self._button_map = button_map or {}
        self._invert_y   = invert_y
        self._stop_event = threading.Event()
        self._ui         = None
        self._virtual_path = None
        # Self-pipe to wake the read loop deterministically on stop(). Closing
        # the source fd from another thread does NOT reliably interrupt a read()
        # blocked on an idle device (Linux), which previously leaked the thread
        # and its virtual uinput device. select() on this pipe avoids that.
        self._wake_r, self._wake_w = os.pipe()

    @property
    def virtual_path(self):
        return self._virtual_path

    def stop(self):
        self._stop_event.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def run(self):
        # Everything the loop allocates (source fd, virtual uinput, selector)
        # is tracked here so the single finally can release it on ANY exit -
        # including the early error returns below. The self-pipe fds are opened
        # in __init__, so they must be closed on every path too; leaking them
        # (as an earlier version did when grab() failed) exhausts the daemon's
        # fd budget under a pad whose grab keeps racing.
        src = ui = sel = None
        try:
            try:
                src = evdev.InputDevice(self._src_path)
            except Exception as ex:
                print(f"remapper: cannot open {self._src_path}: {ex}", file=sys.stderr)
                return

            caps = src.capabilities()
            caps.pop(e.EV_SYN, None)
            caps.pop(e.EV_FF,  None)

            # Advertise the translated (standard) key codes, else SDL maps the
            # target identity against the source's non-standard codes.
            if self._button_map and e.EV_KEY in caps:
                caps[e.EV_KEY] = list(dict.fromkeys(
                    self._button_map.get(c, c) for c in caps[e.EV_KEY]))

            # Source absinfo per axis, for range-preserving value mirroring.
            abs_info = {code: info for code, info in
                        caps.get(e.EV_ABS, [])}

            try:
                ui = UInput(events=caps, **self._target)
            except Exception as ex:
                print(f"remapper: cannot create uinput: {ex}", file=sys.stderr)
                return

            self._ui           = ui
            # The uinput node's /sys entry can lag (or be hidden by a container
            # /sys, where the /dev node still works). Poll briefly, then proceed
            # with None rather than crash the whole remap loop.
            self._virtual_path = None
            for _ in range(10):
                try:
                    if ui.device is not None:
                        self._virtual_path = ui.device.path
                except Exception:
                    pass
                if self._virtual_path:
                    break
                time.sleep(0.1)

            try:
                src.grab()
            except Exception as ex:
                print(f"remapper: grab failed for {self._src_path}: {ex}", file=sys.stderr)
                return

            print(f"remapper: {src.name} -> {self._target['name']} "
                  f"({self._virtual_path})", file=sys.stderr)

            sel = selectors.DefaultSelector()
            sel.register(src.fileno(), selectors.EVENT_READ)
            sel.register(self._wake_r,  selectors.EVENT_READ)
            try:
                while not self._stop_event.is_set():
                    for key, _ in sel.select():
                        if key.fd == self._wake_r:        # stop() signalled
                            self._stop_event.set()
                            break
                        for event in src.read():          # all currently queued events
                            if event.type in (e.EV_KEY, e.EV_ABS, e.EV_REL):
                                code = event.code
                                if event.type == e.EV_KEY and self._button_map:
                                    code = self._button_map.get(code, code)
                                value = event.value
                                if (event.type == e.EV_ABS and self._invert_y
                                        and code in INVERT_Y_AXES):
                                    info = abs_info.get(code)
                                    if info is not None:
                                        # Mirror within the axis range so
                                        # min/max are preserved.
                                        value = info.min + info.max - value
                                ui.write(event.type, code, value)
                                ui.syn()
            except OSError:
                pass
        finally:
            if sel is not None:
                sel.close()
            if src is not None:
                try: src.ungrab()
                except Exception: pass
                try: src.close()
                except Exception: pass
            if ui is not None:
                try: ui.close()
                except Exception: pass
            try: os.close(self._wake_r)
            except Exception: pass
            try: os.close(self._wake_w)
            except Exception: pass
            self._virtual_path = None
            print(f"remapper: stopped for {self._src_path}", file=sys.stderr)


# ── Controller instance ──────────────────────────────────────────────────────

class ControllerInstance:
    def __init__(self, path, name, vendor, product, family, mode, uniq, phys,
                 hidraw, invert_y=False, bindings=None):
        self.path    = path
        self.name    = name
        self.vendor  = vendor
        self.product = product
        self.family  = family
        self.mode    = mode
        self.invert_y = invert_y  # mirror ABS_Y/ABS_RY on the virtual output
        # Per-controller user button remap { src ecode: dst ecode }; keys the
        # user explicitly rebound only. Empty means "program remap as-is".
        self.bindings = dict(bindings or {})
        self.uniq    = uniq       # stable per-device id (BT MAC / serial)
        self.phys    = phys       # physical attachment (USB port / BT adapter)
        # Stable key across reconnects: a reconnect changes the evdev path but
        # not this, so we can recognise the same physical pad and rebind it
        # instead of churning the tray menu.
        self.ident   = ident_of(vendor, product, uniq, phys)
        self.hidraw  = hidraw     # list of /dev/hidrawN (may be empty)
        # DualSense lightbar via the kernel LED class (...:rgb:indicator); None for
        # Xbox pads or when the node can't be resolved. Re-resolved per instance,
        # so it tracks the node renumbering that happens on every BT reconnect.
        self.led     = led_indicator_for_event(path) if family == "ps5" else None
        # Stable player number in overall connection order (first-connected
        # pad = 1), assigned by the manager at adoption and freed on removal.
        # Shown on the DualSense white player LEDs and in the tray label -
        # deliberately OURS, not the kernel's: the kernel re-allocates its id
        # on every gate rebind, and Steam renumbers its slots on every
        # (re)enumeration, so both drift under mode switches.
        self.player  = None
        self._remap  = None
        self._gone_since = None   # monotonic time first seen absent, for grace
        # Resting-colour bookkeeping (ps5-native only, see watch_holders):
        self._holders      = set()   # hidraw holder pids last tick
        self._suppressed   = False   # a game owned the lightbar last tick
        self._latch_risk   = False   # a raw fd closed; firmware may be latched
        self._assert_at    = 0.0     # next slow resting-colour re-assert
        self._repaint_at   = None    # scheduled one-shot resting repaint
        self._player_repaint_at = None   # one-shot player-LED-only re-assert

    def display_name(self):
        return self.name

    # ── LED lightbar ─────────────────────────────────────────────────────────

    def _apply_led(self):
        """Set the lightbar colour for the current mode via a raw HID output
        report (hidraw), which also clears the DualSense firmware 'light out'
        latch - a kernel LED-class write does not, so a latched pad would stay
        physically dark. Re-resolves the live nodes from the evdev path on every
        call and never trusts a cached name: both the hidraw and the
        ...:rgb:indicator node renumber on every (BT) reconnect. No-op for Xbox
        pads or when nothing resolves."""
        if self.family != "ps5":
            return
        # Every apply (whatever triggered it) pushes the next slow re-assert
        # out by one full period - the assert is a backstop, not a metronome.
        self._assert_at = time.monotonic() + 15.0
        # Track the rgb-indicator node name so refresh_led can spot a renumber.
        self.led = led_indicator_for_event(self.path)
        led_set_raw(hidraw_for_event(self.path), MODE_LED.get(self.mode, (0, 0, 0)))
        self._apply_player_leds()

    def _apply_player_leds(self):
        """Re-assert this pad's player number on its white player LEDs. Same
        churn problem as the lightbar: the kernel re-allocates its player id
        on every gate rebind and Steam rewrites its own slot count raw on
        every (re)enumeration - the daemon's adoption-ordered number is the
        one that stays put. The LED names share the pad's live inputN prefix
        with the rgb indicator, so resolve it from there."""
        if self.family != "ps5" or not self.player:
            return
        led = led_indicator_for_event(self.path)
        if led:
            led_set_player(led.split(":", 1)[0], self.player)

    def refresh_led(self):
        """Repaint the lightbar when its LED node has renumbered under us. The
        ...:rgb:indicator node is named after the inputN instance, which bumps on
        every (BT) reconnect *even when the /dev/input/eventX path is reused* - so
        the path-keyed rebind() never sees the change and the daemon would keep
        painting the old, now-dead node (a mode switch then changes no colour).
        Called each monitor tick: when the live node differs from the one we last
        painted, repaint it. Idempotent while the node is stable."""
        if self.family != "ps5":
            return
        led = led_indicator_for_event(self.path)
        if led and led != self.led:
            self._apply_led()

    def watch_holders(self, steam_game=False):
        """Resting-colour policy for ps5-native (remap modes need none of
        this - there the gate guarantees exclusive LED ownership). The
        question each tick is: does the lightbar belong to a GAME right
        now? Two signals answer it (the Steam client holds every pad
        permanently with PlayStation support enabled, so the holder set
        alone no longer can):

        * steam_game - a Steam-launched title is alive (manager-wide
          cmdline sweep, see steam_game_running). Games under Steam Input
          never open the pad themselves, the client does it for them;
        * a FOREIGN holder - any raw-HID holder that is not the Steam
          client. Direct-access titles (Steam Input disabled per game,
          Lutris/Wine, emulators) open the pad in their own name.

        While either is true the lightbar is legitimately the game's:
        hands off, cancel pending repaints. Otherwise the resting colour
        is OURS - the idle client paints its slot colours only on discrete
        events (startup, adopt, game exit) and stays quiet in between, so
        the daemon re-asserts the mode colour after each of them
        (rewriting an unchanged colour is visually a no-op - this is not
        the paint war the native mode must avoid):

        * a RELEASER closed a raw fd (game exited, Steam quit): it may
          leave the lightbar firmware-latched dark ('light out' setup flag
          - kernel LED writes then change nothing until the driver
          re-probes), so rearm (driver rebind -> fresh lightbar setup) and
          repaint. Deferred while a game is still active: the rebind
          would yank the running game's fd;
        * suppression ended with no fd ever closing (a Steam Input game
          exited): the client restores its slot colour on the way out -
          one repaint shortly after gets the last word, no rearm needed;
        * the client (re)opened the pad (enumeration after one of our
          rebinds, or a client start): writes its defaults once and goes
          quiet - one delayed repaint outlasts it (field-verified: Steam
          reopened ~6 s after a rebind and stomped the fresh colour);
        * backstop: a slow periodic re-assert (_assert_at, one period
          after whatever painted last) recovers the mode colour from
          anything unforeseen - so the pad shows its mode at a glance at
          all times outside a running game.

        The scheduled one-shot repaint at the end fires in remap modes too:
        apply_mode arms it after every gate transition, because the write
        right after a driver rebind races the BT re-probe and can be dropped.
        Called from the monitor tick, outside the manager lock (shells out)."""
        if self.family != "ps5":
            return
        now = time.monotonic()
        if self._target_for_mode() is None:
            holders = hidraw_holders(self.hidraw)
            foreign = any(not is_steam_client(pid) for pid in holders)
            if self._holders - holders:
                self._latch_risk = True    # a raw fd closed since last heal
            changed = holders != self._holders
            self._holders = holders
            if steam_game or foreign:
                self._suppressed = True
                self._repaint_at = None            # lightbar is the game's
                self._assert_at  = now + 15.0      # no overdue write at exit
                                                   # racing Steam's restore
            else:
                if self._latch_risk:
                    self._latch_risk = False
                    self._suppressed = False
                    hidraw_gate("rearm", self.uniq, self.hidraw)  # kills fds too
                    self._holders = set()          # we just revoked the rest
                    self._refresh_nodes()
                    self._apply_led()
                    self._repaint_at = now + 6.0
                elif self._suppressed:
                    self._suppressed = False
                    self._repaint_at = now + 3.0   # outlast the exit restore
                elif changed:
                    self._repaint_at = now + 6.0   # outlast the (re)open write
                if (now >= self._assert_at
                        and not (self._repaint_at is not None
                                 and now >= self._repaint_at)):
                    # Backstop paint - unless a one-shot fires this very
                    # tick anyway (below); _apply_led reschedules _assert_at.
                    self._apply_led()
        if self._repaint_at is not None and now >= self._repaint_at:
            self._repaint_at = None
            self._apply_led()
        # Player-LED-only re-assert, armed by the manager when ANOTHER pad's
        # gate churn made Steam recount and rewrite its slots on every pad it
        # holds. Deliberately does not touch the lightbar: outside the settle
        # window that may legitimately belong to a game.
        if (self._player_repaint_at is not None
                and now >= self._player_repaint_at):
            self._player_repaint_at = None
            self._apply_player_leds()

    # ── mode / lifecycle ──────────────────────────────────────────────────────

    def _target_for_mode(self):
        if self.mode == "ps5-xbox":
            return VIRTUAL_XBOX
        if self.mode == "xbox-ps5":
            return VIRTUAL_PS5
        return None

    def apply_mode(self, adopt=False):
        """Stop existing remapper, transition the hidraw gate, start a new
        remapper if needed, update LED. A gate state transition rebinds the
        kernel driver (to revoke foreign fds and reset the lightbar latch),
        which renumbers this pad's device nodes - so re-resolve them before
        grabbing, and never grab before gating (the rebind would kill the
        grab again)."""
        if self._remap:
            old = self._remap
            old.stop()
            old.join(timeout=1.0)   # wait for ungrab before the new grab attempt
            self._remap = None

        target = self._target_for_mode()
        # Hide this pad's raw HID path from applications before the remapper
        # takes over (the evdev grab alone does not cover hidraw) - or reopen
        # it when returning to native.
        hidraw_gate("block" if target else "restore", self.uniq, self.hidraw)
        self._refresh_nodes()
        if adopt and not target and hidraw_holders(self.hidraw):
            # Adoption found the pad already opened by someone else: that fd
            # predates us (Steam Input grabs every pad the moment it connects)
            # and may have firmware-latched the lightbar dark - a state no
            # later kernel LED write can clear. Gating relies on marker
            # TRANSITIONS to rebind, and a freshly paired identity has no
            # marker, so the restore above never rebound - force it here.
            # Holders that arrive THROUGH a restore rebind never hit this:
            # their nodes are renumbered and reopen only seconds later.
            hidraw_gate("rearm", self.uniq, self.hidraw)
            self._refresh_nodes()

        if target and self.path:
            bmap = compose_button_maps(
                QUIRK_BUTTON_MAP.get((self.vendor, self.product)),
                self.bindings)
            r = Remapper(self.path, target, bmap, invert_y=self.invert_y)
            r.start()
            self._remap = r

        # The gate transition above may have rebound the driver: every old
        # holder fd is dead, and enumerators (Steam) will REOPEN the reborn
        # nodes in a few seconds and write their defaults once -
        # watch_holders outlasts that with a fresh delayed repaint. The
        # repaint here is scheduled unconditionally: the immediate write
        # below races the BT re-probe after a rebind and can be dropped by
        # the pad - the delayed re-assert is what makes the colour stick
        # (in remap modes too, where the holder policy itself is inactive).
        self._holders    = set()
        self._repaint_at = time.monotonic() + 6.0

        self._apply_led()

    def _refresh_nodes(self, timeout=3.0):
        """Re-resolve path/hidraw/LED bindings after a gate transition tore
        the pad's kernel nodes down and recreated them renumbered. Polls
        briefly because the re-probe takes a moment; when nothing was rebound
        the first scan simply confirms the existing binding. If the pad does
        not come back in time the stale path is dropped - the monitor's
        reconcile rebinds when it reappears."""
        deadline = time.monotonic() + timeout
        while True:
            for d in scan_controllers():
                if ident_of(d["vendor"], d["product"],
                            d["uniq"], d["phys"]) == self.ident:
                    self.path   = d["path"]
                    self.name   = d["name"]
                    self.hidraw = d["hidraw"]
                    self.led    = (led_indicator_for_event(self.path)
                                   if self.family == "ps5" else None)
                    return
            if time.monotonic() >= deadline:
                self.path = None
                return
            time.sleep(0.1)

    def rebind(self, path, name, hidraw):
        """Same physical pad reappeared on a new evdev/hidraw node after a (BT)
        reconnect. Refresh the node bindings and restart the remapper in place -
        the instance (and thus its tray-menu entry) is kept, so the host sees no
        structural change. Deliberately NO gate 'restore' here: the gate is
        keyed by the pad's stable identity, so staying gated across a reconnect
        is exactly right - the reborn nodes were already born gated (udev
        rule), and apply_mode re-asserts the gate idempotently."""
        self.path   = path
        self.name   = name
        self.hidraw = hidraw
        self.led    = led_indicator_for_event(path) if self.family == "ps5" else None
        self.apply_mode()                     # re-grab, re-gate, re-apply LED

    def virtual_path(self):
        return self._remap.virtual_path if self._remap else None

    def remap_healthy(self):
        """False when the current mode calls for a remapper but none is
        running (thread died: lost grab after a sub-poll BT blip, or the
        grab failed outright). Native modes are trivially healthy."""
        if self._target_for_mode() is None:
            return True
        return self._remap is not None and self._remap.is_alive()

    def stop(self):
        if self._remap:
            self._remap.stop()
            self._remap = None
        # Ungate whenever we stop managing this pad (disconnect or shutdown),
        # so a gated pad never lingers invisible to every application.
        hidraw_gate("restore", self.uniq, self.hidraw)


# ── Controller Manager ───────────────────────────────────────────────────────

# Grace period before a vanished controller is really dropped. A Bluetooth pad
# re-registers on a *new* evdev/hidraw node after every reconnect (e.g. when it
# is un/re-paired for console use); without this window the brief gap would tear
# the instance down and rebuild it, churning the tray menu into stuck-disabled
# items. Instances are keyed by stable identity (BT MAC / serial), so a pad that
# returns within the window is rebound in place instead of removed and re-added.
REMOVE_GRACE = 5.0   # seconds; ~2x the 2 s poll interval

# Grace before a GAP in the player numbering (a pad switched off long enough to
# be dropped) is compacted away, closing the hole so the remaining players are
# renumbered to a contiguous 1..N. Deliberately long: a controller whose
# battery/accu dies and is swapped returns within this window, reclaims its old
# number (see _poll's adoption path) and never triggers a renumber - only a pad
# that stays off past it gives up its slot. Auto-compaction lives in _poll; the
# tray 'Renumber players' entry forces it immediately.
COMPACT_GRACE = 300.0   # seconds (5 minutes)

def _numbering_gap(players):
    """True when a set of player numbers is not a contiguous 1..N - i.e. a
    removed pad left a hole compaction can close. Falsy numbers are ignored."""
    nums = sorted(p for p in players if p)
    return nums != list(range(1, len(nums) + 1))

class ControllerManager:
    def __init__(self, on_change_cb):
        self._lock       = threading.Lock()
        self._instances  = {}      # ident -> ControllerInstance
        self._config     = load_config()
        self._invert_y   = bool(self._config.get("invert_y_axes", False))
        self._on_change  = on_change_cb   # called (from thread) when list changes
        self._monitor_th = threading.Thread(target=self._monitor, daemon=True)
        # Monotonic deadline at which a standing numbering gap is compacted, or
        # None when the numbers are contiguous. Armed/cleared each _poll pass.
        self._compact_due = None
        # Button-capture state (binding GUI): ident currently being captured, or
        # None. One capture at a time; the _poll reconcile must not re-grab a pad
        # mid-capture (it would eat the user's press).
        self._capturing_ident = None
        self._capture_worker  = None
        self._on_capture      = None   # callback(ident, code) called on completion
        self._capture_cancel  = threading.Event()
        self._gui_proc        = None   # running binding-GUI subprocess (if any)

    def start(self):
        # Populate instances synchronously so the first menu we publish already
        # reflects connected controllers. Registering an empty menu and mutating
        # it afterwards makes the host recycle item ids across a structural change
        # (separator->radio, header relabel), which leaves entries stuck disabled.
        # Then run the periodic monitor for hotplug.
        self._poll()
        self._monitor_th.start()

    def _virtual_paths(self):
        paths = set()
        for inst in self._instances.values():
            vp = inst.virtual_path()
            if vp:
                paths.add(vp)
        return paths

    def _scan(self):
        """Connected real controllers, minus our own virtual output devices."""
        return scan_controllers(exclude_paths=self._virtual_paths())

    @staticmethod
    def _ident(d):
        """Stable identity of a scanned controller - matches ControllerInstance.ident."""
        return ident_of(d["vendor"], d["product"], d["uniq"], d["phys"])

    def _poll(self):
        """One scan/reconcile pass; returns True if the *set* of controllers
        changed. A reconnect that only moves a pad to a new node does not count -
        it is rebound in place, so the tray menu is left untouched."""
        found = {}
        for d in self._scan():
            found[self._ident(d)] = d   # identical padless pads collapse; acceptable
        changed = False
        churned = False       # any gate/driver churn Steam re-enumerates on
        config_dirty = False  # a player number was assigned or changed
        now = time.monotonic()
        with self._lock:
            # Reconcile known instances: rebind on reconnect, drop after grace.
            for ident, inst in list(self._instances.items()):
                d = found.get(ident)
                if d is None:                       # absent this pass
                    if inst._gone_since is None:
                        inst._gone_since = now
                    elif now - inst._gone_since >= REMOVE_GRACE:
                        inst.stop()
                        del self._instances[ident]
                        changed = True
                    continue
                was_gone = inst._gone_since is not None
                inst._gone_since = None             # present again
                if d["path"] != inst.path:          # reconnected on a new node
                    inst.rebind(d["path"], d["name"], d["hidraw"])
                    churned = True
                elif was_gone or not inst.remap_healthy():
                    # Two ways a pad can look unchanged yet be broken underneath:
                    #  * reconnected on the *reused* evdev path (BT re-pair /
                    #    power-cycle): same eventX number, fresh device below -
                    #    the old grab died and the firmware reset the lightbar;
                    #  * a BT blip shorter than one poll killed the remapper's
                    #    grab without the pad ever appearing absent (the thread
                    #    is dead but the mode still wants a remap).
                    # Both: re-assert the whole mode - restart the remapper,
                    # re-gate the hidraw node, repaint the lightbar. Skipped while
                    # the binding GUI is capturing this pad: a re-grab would eat
                    # the press the user is about to record.
                    if ident != self._capturing_ident:
                        inst.rebind(d["path"], d["name"], d["hidraw"])
                        churned = True
            # Add genuinely new controllers.
            for ident, d in found.items():
                if ident in self._instances:
                    continue
                default = DEFAULT_MODES.get(d["family"], "ps5-native")
                mode = self._config.get(ident, default)
                # Drop modes no longer offered for this family (e.g. a stored
                # "xbox-ps5" from before it was removed): fall back to the native
                # default instead of silently applying an unselectable mode.
                if mode not in MODES_FOR_FAMILY.get(d["family"], []):
                    mode = default
                inst = ControllerInstance(
                    d["path"], d["name"], d["vendor"], d["product"],
                    d["family"], mode, d["uniq"], d["phys"], d["hidraw"],
                    invert_y=self._invert_y,
                    bindings=self._config.get(BINDINGS_KEY, {}).get(ident))
                # Overall connection order: a pad keeps the number it was
                # FIRST adopted under ("_players" in the config, keyed like
                # the modes by stable ident) - daemon restarts re-adopt in
                # evdev node order, which the gate rebinds shuffle, so the
                # session order alone would swap numbers between restarts.
                # Fresh pads (or a persisted number currently worn by another
                # connected pad) take the lowest free number.
                used = {i.player for i in self._instances.values()}
                player = self._config.get("_players", {}).get(ident)
                if not player or player in used:
                    player = next(p for p in range(1, len(used) + 2)
                                  if p not in used)
                inst.player = player
                if self._config.get("_players", {}).get(ident) != player:
                    self._config.setdefault("_players", {})[ident] = player
                    config_dirty = True
                inst.apply_mode(adopt=True)
                self._instances[ident] = inst
                changed = True
                churned = True
            if churned:
                self._arm_player_reassert()
            # Auto-compaction: once a dropped pad leaves the connected numbers
            # non-contiguous, close the gap after COMPACT_GRACE - long enough
            # for a battery/accu swap to return and reclaim the number first,
            # so a brief power-off never renumbers the remaining players. A gap
            # that heals on its own (pad came back) disarms the timer.
            if self._gap_present():
                if self._compact_due is None:
                    self._compact_due = now + COMPACT_GRACE
                elif now >= self._compact_due:
                    if self._renumber_locked():
                        self._arm_player_reassert()
                        changed = True
                        config_dirty = True
                    self._compact_due = None
            else:
                self._compact_due = None
        if config_dirty:
            save_config(self._config)
        if changed:
            GLib.idle_add(self._on_change)
        # LED self-heal + resting-colour watch, outside the lock (both may
        # shell out to sudo, and we must not stall set_mode/get_instances
        # behind that): a pad rebound while its :rgb:indicator node still
        # lagged gets its colour applied on a later tick, and a ps5-native
        # pad reclaims its resting colour whenever no game owns the lightbar.
        # The Steam-game sweep is one /proc pass for all pads, and skipped
        # entirely when no pad is in a native ps5 mode (nobody would use it).
        instances = self.get_instances()
        steam_game = (any(i.mode == "ps5-native" for i in instances)
                      and steam_game_running())
        for inst in instances:
            inst.refresh_led()
            inst.watch_holders(steam_game)
        return changed

    def _monitor(self):
        # A single unhandled exception in _poll would otherwise kill this daemon
        # thread for good: hotplug adoption, removal and the resting-colour watch
        # all stop silently while the D-Bus main loop keeps answering menu clicks,
        # so the tray looks alive but never updates again (a stale pad path once
        # crashed hidraw_for_event here and froze the whole daemon). Log and carry
        # on; the next tick retries from a fresh scan.
        while True:
            time.sleep(2)
            try:
                self._poll()
            except Exception:
                traceback.print_exc()

    def get_instances(self):
        with self._lock:
            return list(self._instances.values())

    def _arm_player_reassert(self):
        """A gate/driver rebind makes enumerators (Steam) recount and rewrite
        THEIR player slots raw on every pad they hold - not just the rebound
        one. Schedule a one-shot re-assert of our stable numbers on all ps5
        pads once that write has passed. Called under the manager lock."""
        for inst in self._instances.values():
            if inst.family == "ps5":
                inst._player_repaint_at = time.monotonic() + 6.0

    def _gap_present(self):
        """True when the connected pads' player numbers are not a contiguous
        1..N. Caller holds the lock."""
        return _numbering_gap(i.player for i in self._instances.values())

    def _renumber_locked(self):
        """Compact the connected pads' player numbers to a contiguous 1..N,
        preserving their relative order: the numbers shift, but two continuously
        connected pads never swap places and nobody overtakes anybody. The
        stale reservations of absent pads in _players are left untouched - a
        returning pad finds its old number now worn by a lower-numbered peer,
        so it takes the next free one instead of reopening the closed gap.
        Returns True if any number changed. Caller holds the lock and is
        responsible for persisting the config and notifying afterwards."""
        changed = False
        ordered = sorted((i for i in self._instances.values() if i.player),
                         key=lambda i: i.player)
        for new_num, inst in enumerate(ordered, start=1):
            if inst.player != new_num:
                inst.player = new_num
                self._config.setdefault("_players", {})[inst.ident] = new_num
                changed = True
        return changed

    def renumber(self):
        """Compact player numbers now (order-preserving), bypassing the
        auto-compaction timer. Driven by the tray 'Renumber players' entry."""
        with self._lock:
            changed = self._renumber_locked()
            if changed:
                self._arm_player_reassert()
            self._compact_due = None
            instances = list(self._instances.values())
        if changed:
            save_config(self._config)
            # Immediate feedback on the white player LEDs; the armed re-assert
            # above outlasts Steam's slot rewrite a few seconds later.
            for inst in instances:
                inst._apply_player_leds()
            GLib.idle_add(self._on_change)

    def set_mode(self, ident, mode):
        with self._lock:
            inst = self._instances.get(ident)
            if not inst:
                return
            inst.mode = mode
            inst.apply_mode()
            self._config[inst.ident] = mode
            self._arm_player_reassert()
        save_config(self._config)
        GLib.idle_add(self._on_change)

    def stop_all(self):
        with self._lock:
            for inst in self._instances.values():
                inst.stop()

    # ── button bindings (per-controller user remap) ───────────────────────────

    @staticmethod
    def _norm_bindings(entry):
        """JSON round-trips object keys as strings, but evdev codes are ints.
        Normalise a configured {src: dst} map (or None) to {int src: int dst}."""
        out = {}
        for k, v in (entry or {}).items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def get_bindings(self, ident):
        """The controller's configured {src ec: dst ec} map (int keys)."""
        with self._lock:
            return self._norm_bindings(
                self._config.get(BINDINGS_KEY, {}).get(ident))

    def get_buttons(self, ident):
        """(code, user-facing label) for every button the controller exposes,
        for the binding GUI's source list."""
        with self._lock:
            inst = self._instances.get(ident)
            if not inst or not inst.path:
                return []
        try:
            dev = evdev.InputDevice(inst.path)
        except Exception:
            return []
        try:
            keys = dev.capabilities().get(e.EV_KEY, [])
        finally:
            dev.close()
        codes = sorted({c for c in keys if isinstance(c, int)})
        return [(c, action_label(c, family=inst.family)) for c in codes]

    def set_binding(self, ident, src, dst):
        """Map button `src` on this controller to button `dst`; dst < 0 removes
        the mapping, falling back to the program's quirk remap."""
        with self._lock:
            inst = self._instances.get(ident)
            if not inst:
                return False
            src, dst = int(src), int(dst)
            per = self._config.setdefault(BINDINGS_KEY, {})
            entry = per.setdefault(ident, {})
            key = str(src)
            if dst < 0:
                entry.pop(key, None)
                if not entry:
                    per.pop(ident, None)
                    if not per:
                        self._config.pop(BINDINGS_KEY, None)
            else:
                entry[key] = dst
            inst.bindings = self._norm_bindings(per.get(ident))
            inst.apply_mode()
        save_config(self._config)
        GLib.idle_add(self._on_change)
        return True

    def reset_bindings(self, ident):
        """Remove every user binding on this controller (program remap only)."""
        with self._lock:
            inst = self._instances.get(ident)
            if not inst:
                return False
            per = self._config.get(BINDINGS_KEY, {})
            per.pop(ident, None)
            if not per:
                self._config.pop(BINDINGS_KEY, None)
            inst.bindings = {}
            inst.apply_mode()
        save_config(self._config)
        GLib.idle_add(self._on_change)
        return True

    def open_gui(self, ident=None):
        """Launch the binding GUI as a SEPARATE process - the daemon never links
        a GUI toolkit (docs/architecture/overview.md). A GUI already running is
        reused, never respawned; the optional ident pre-selects a controller."""
        if not os.path.exists(GUI_SCRIPT):
            return
        if self._gui_proc is not None and self._gui_proc.poll() is None:
            return
        cmd = [sys.executable, GUI_SCRIPT]
        if ident:
            cmd.append(str(ident))
        self._gui_proc = subprocess.Popen(cmd)

    # ── button capture (binding GUI records a physical press) ─────────────────

    def start_capture(self, ident, timeout, on_result):
        """Begin recording one physical button press on `ident`. The remapper's
        grab is briefly released (gate state kept, so the pad stays hidden in a
        remap mode), the next EV_KEY down-press - or the timeout - is captured,
        the remap is re-asserted, then on_result(ident, code) fires (code 0 on
        timeout/cancel). One capture at a time; False when busy or unknown."""
        with self._lock:
            if self._capturing_ident is not None:
                return False
            inst = self._instances.get(ident)
            if not inst or not inst.path:
                return False
            self._capturing_ident = ident
            self._capture_cancel = threading.Event()
            self._on_capture = on_result
        threading.Thread(
            target=self._capture_run, args=(ident, float(timeout)),
            daemon=True).start()
        return True

    def cancel_capture(self, ident):
        with self._lock:
            if self._capturing_ident != ident:
                return False
            self._capture_cancel.set()
        return True

    def _capture_run(self, ident, timeout):
        dev = None
        code = 0
        try:
            inst = next((i for i in self.get_instances() if i.ident == ident), None)
            if inst is not None and inst._remap is not None:
                old = inst._remap
                old.stop()
                old.join(timeout=1.0)   # wait for the ungrab before opening
                inst._remap = None
            if inst is not None and inst.path:
                try:
                    dev = evdev.InputDevice(inst.path)
                    code = capture_button(dev, timeout, self._capture_cancel)
                except Exception as ex:
                    print(f"capture: {ident}: {ex}", file=sys.stderr)
        except Exception as ex:
            print(f"capture: {ident}: {ex}", file=sys.stderr)
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
            inst = next((i for i in self.get_instances() if i.ident == ident), None)
            if inst is not None:
                try:
                    inst.apply_mode()   # re-grab, re-gate (no-op), repaint
                except Exception as ex:
                    print(f"capture: re-assert failed for {ident}: {ex}",
                          file=sys.stderr)
            with self._lock:
                self._capturing_ident = None
            cb, self._on_capture = self._on_capture, None
            if cb is not None:
                GLib.idle_add(cb, ident, code)


# ── dbusmenu ────────────────────────────────────────────────────────────────

class DbusmenuServer(dbus.service.Object):

    def __init__(self, bus, path, manager, on_quit):
        dbus.service.Object.__init__(self, bus, path)
        self._mgr      = manager
        self._on_quit  = on_quit
        self._revision = 1
        # Cached menu model. Every dbusmenu call (GetLayout, GetGroupProperties,
        # GetProperty, Event) answers from this one snapshot, so within a
        # revision a host can never observe two different id->item mappings -
        # rebuilding per call let the instance list shift between a host's
        # GetLayout and its Event, misrouting the click.
        self._items    = []    # ordered [(id, props-dict)], Quit last
        self._lookup   = {}    # id -> (controller ident, mode) for radio items
        self._actions  = {}    # id -> action name for plain command items
        self._sig      = None  # structural signature of the cached model
        # Item ids come from this counter and are NEVER reused for a different
        # item. The GNOME appindicator host caches item properties per id and
        # does not refresh them when a layout change reuses an id for a
        # structurally different item (separator->radio after a controller
        # connects), leaving entries stuck disabled. Fresh ids per structural
        # change force the host to treat them as new items and fetch fresh
        # properties. Display-only changes keep their ids and are pushed as
        # ItemsPropertiesUpdated deltas instead.
        self._next_id  = 1

    # ── menu building ────────────────────────────────────────────────────────

    def _make_item(self, id_, props):
        return dbus.Struct(
            [dbus.Int32(id_),
             dbus.Dictionary(props, signature="sv"),
             dbus.Array([], signature="v")],
            signature="(ia{sv}av)"
        )

    def _inst_label(self, inst, instances):
        """'DualSense' for a unique model; 'DualSense <player>' when duplicates
        exist. The player number is the same one shown on the pad's white
        player LEDs, so menu and hardware always agree - a positional index
        would flip after a drop-and-readopt while the LEDs kept the number."""
        peers = [i for i in instances if i.name == inst.name]
        if len(peers) == 1:
            return inst.name
        if inst.player:
            return f"{inst.name} {inst.player}"
        return f"{inst.name} {peers.index(inst) + 1}"

    def _semantic_items(self):
        """The menu as plain (kind, ...) tuples, before ids and dbus types:
        ('info', label) | ('header', ident, label)
        | ('radio', ident, mode, label, checked) | ('sep',)."""
        instances = self._mgr.get_instances()
        sem = []
        if not instances:
            sem.append(("info", "No controller connected"))
        else:
            for inst in instances:
                sem.append(("header", inst.ident,
                            self._inst_label(inst, instances)))
                modes = MODES_FOR_FAMILY.get(inst.family, [])
                if len(modes) == 1:
                    # A single-choice family (e.g. Xbox: only native) has
                    # nothing to switch between. Show the mode as a static,
                    # non-interactive line rather than a lone radio that is
                    # always checked and does nothing when clicked. If a second
                    # mode is ever enabled for the family this reverts to radios
                    # automatically.
                    sem.append(("info", MODE_LABELS[modes[0]]))
                else:
                    for mode in modes:
                        sem.append(("radio", inst.ident, mode,
                                    MODE_LABELS[mode], inst.mode == mode))
                if inst is not instances[-1]:
                    sem.append(("sep",))
                # Per-controller entry into the binding GUI (separate process);
                # the ident is carried so the GUI pre-selects this pad.
                sem.append(("action", ("remap", inst.ident),
                            "Remap buttons..."))
                if inst is not instances[-1]:
                    sem.append(("sep",))
        # Offer a manual compaction only while a gap actually exists - with a
        # contiguous numbering the entry would be a no-op and just clutter.
        if _numbering_gap(getattr(i, "player", None) for i in instances):
            sem.append(("sep",))
            sem.append(("action", "renumber", "Renumber players"))
        sem.append(("sep",))   # final separator before Quit
        return sem

    @staticmethod
    def _props_for(entry):
        kind = entry[0]
        if kind == "info":
            return {"label":   dbus.String(entry[1]),
                    "enabled": dbus.Boolean(False)}
        if kind == "header":
            return {"label":   dbus.String(entry[2]),
                    "enabled": dbus.Boolean(False)}
        if kind == "radio":
            return {"label":        dbus.String(entry[3]),
                    "enabled":      dbus.Boolean(True),
                    "toggle-type":  dbus.String("radio"),
                    "toggle-state": dbus.Int32(1 if entry[4] else 0)}
        if kind == "action":
            return {"label":   dbus.String(entry[2]),
                    "enabled": dbus.Boolean(True)}
        return {"type": dbus.String("separator")}

    @staticmethod
    def _structure_sig(sem):
        """What defines an item's KIND and click target. Labels and
        toggle-state are volatile display state - excluded here, so they
        update in place via ItemsPropertiesUpdated without an id change."""
        sig = []
        for entry in sem:
            if entry[0] == "radio":
                sig.append(("radio", entry[1], entry[2]))
            elif entry[0] == "header":
                sig.append(("header", entry[1]))
            elif entry[0] == "action":
                sig.append(("action", entry[1]))
            else:
                sig.append((entry[0],))
        return tuple(sig)

    def _rebuild(self):
        """Sync the cached model to the current controller state and emit the
        matching dbusmenu signal (LayoutUpdated on structural change,
        ItemsPropertiesUpdated for display-state deltas). Main loop only."""
        sem = self._semantic_items()
        sig = self._structure_sig(sem)

        if sig != self._sig:
            items, lookup, actions = [], {}, {}
            for entry in sem:
                if self._next_id == QUIT_ID:   # never hand out the Quit id
                    self._next_id += 1
                id_ = self._next_id
                self._next_id += 1
                items.append((id_, self._props_for(entry)))
                if entry[0] == "radio":
                    lookup[id_] = (entry[1], entry[2])
                elif entry[0] == "action":
                    actions[id_] = entry[1]
            # Quit is the one constant item: same kind, label and props
            # forever, so its well-known id is safe to keep.
            items.append((QUIT_ID, {"label":   dbus.String("Quit"),
                                    "enabled": dbus.Boolean(True)}))
            self._items, self._lookup, self._sig = items, lookup, sig
            self._actions = actions
            self._revision += 1
            self.LayoutUpdated(dbus.UInt32(self._revision), dbus.Int32(0))
            return

        # Same structure: positions align pairwise with the cached items
        # (Quit, cached last, has no semantic entry - zip stops before it).
        updated = []
        for (id_, props), entry in zip(self._items, sem):
            new = self._props_for(entry)
            delta = {k: v for k, v in new.items() if props.get(k) != v}
            if delta:
                props.update(delta)
                updated.append((id_, delta))
        if updated:
            self.ItemsPropertiesUpdated(
                dbus.Array(
                    [dbus.Struct(
                        [dbus.Int32(i), dbus.Dictionary(p, signature="sv")],
                        signature="(ia{sv})") for i, p in updated],
                    signature="(ia{sv})"),
                dbus.Array([], signature="(ias)"))

    def _layout(self):
        if self._sig is None:      # first host call before any state change
            self._rebuild()
        return dbus.Struct(
            [dbus.Int32(0),
             dbus.Dictionary({}, signature="sv"),
             dbus.Array(
                 [dbus.Struct(self._make_item(id_, props),
                              signature="(ia{sv}av)")
                  for id_, props in self._items],
                 signature="v")],
            signature="(ia{sv}av)"
        )

    def notify_update(self):
        self._rebuild()

    # ── dbusmenu methods ─────────────────────────────────────────────────────

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parentId, recursionDepth, propertyNames):
        return dbus.UInt32(self._revision), self._layout()

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, propertyNames):
        if self._sig is None:
            self._rebuild()
        want  = {int(i) for i in ids}            # empty -> all items, per spec
        names = {str(n) for n in propertyNames}  # empty -> all properties
        result = []
        for id_, props in self._items:
            if want and id_ not in want:
                continue
            if names:
                props = {k: v for k, v in props.items() if k in names}
            result.append(dbus.Struct(
                [dbus.Int32(id_), dbus.Dictionary(props, signature="sv")],
                signature="(ia{sv})"))
        return dbus.Array(result, signature="(ia{sv})")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="is", out_signature="v")
    def GetProperty(self, id_, name):
        if self._sig is None:
            self._rebuild()
        for iid, props in self._items:
            if iid == int(id_) and str(name) in props:
                return props[str(name)]
        return dbus.String("")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="isvu", out_signature="")
    def Event(self, id_, eventId, data, timestamp):
        if eventId != "clicked":
            return
        if id_ == QUIT_ID:
            GLib.idle_add(lambda: (self._on_quit(), False)[1])
            return
        # Route via the cached model, so the click lands on exactly the item
        # the host displayed. An id from a stale revision (structure changed
        # since the host fetched it) is simply absent here and ignored.
        hit = self._lookup.get(int(id_))
        if hit:
            ctrl_ident, mode = hit
            self._mgr.set_mode(ctrl_ident, mode)
            return
        action = self._actions.get(int(id_))
        if action == "renumber":
            self._mgr.renumber()
        elif isinstance(action, tuple) and action[0] == "remap":
            self._mgr.open_gui(action[1])

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for id_, eventId, data, ts in events:
            self.Event(id_, eventId, data, ts)
        return dbus.Array([], signature="i")

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="i", out_signature="b")
    def AboutToShow(self, id_):
        return False

    @dbus.service.method("com.canonical.dbusmenu",
                         in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], "i"), dbus.Array([], "i")

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return {"Version": dbus.UInt32(3), "TextDirection": dbus.String("ltr"),
                "Status":  dbus.String("normal"),
                "IconThemePath": dbus.Array([], "s")}.get(prop, dbus.String(""))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return {"Version": dbus.UInt32(3), "TextDirection": dbus.String("ltr"),
                "Status":  dbus.String("normal"),
                "IconThemePath": dbus.Array([], "s")}

    @dbus.service.signal("com.canonical.dbusmenu", signature="ui")
    def LayoutUpdated(self, revision, parent): pass

    @dbus.service.signal("com.canonical.dbusmenu", signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed): pass

    @dbus.service.signal("com.canonical.dbusmenu", signature="iu")
    def ItemActivationRequested(self, id_, timestamp): pass


# ── Controller binding API (separate GTK GUI) ─────────────────────────────────

class CtlrMgrServer(dbus.service.Object):
    """D-Bus surface for the binding GUI (controller-gui.py, a separate GTK
    process - the daemon itself links no toolkit). Runs on the daemon's own
    main loop; captures happen on a worker thread and their result arrives as
    a signal, since the GUI must never be blocked behind a press wait."""

    def __init__(self, bus, manager):
        self._bus_name = dbus.service.BusName(CTRLMGR_BUS, bus)
        dbus.service.Object.__init__(self, self._bus_name, CTRLMGR_PATH)
        self._mgr = manager

    @dbus.service.method(CTRLMGR_IFACE, out_signature="a(ssss)")
    def ListControllers(self):
        return dbus.Array(
            [dbus.Struct((inst.ident, inst.name, inst.family, inst.mode),
                         signature="(ssss)")
             for inst in self._mgr.get_instances()],
            signature="(ssss)")

    @dbus.service.method(CTRLMGR_IFACE, in_signature="s", out_signature="a(is)")
    def GetButtons(self, ident):
        return dbus.Array(
            [dbus.Struct((code, label), signature="(is)")
             for code, label in self._mgr.get_buttons(str(ident))],
            signature="(is)")

    @dbus.service.method(CTRLMGR_IFACE, out_signature="a(is)")
    def GetTargetButtons(self):
        labels = TARGET_BTN_LABELS[VIRTUAL_XBOX["name"]]
        return dbus.Array(
            [dbus.Struct((code, label), signature="(is)")
             for code, label in sorted(labels.items())],
            signature="(is)")

    @dbus.service.method(CTRLMGR_IFACE, in_signature="s", out_signature="a{ii}")
    def GetBindings(self, ident):
        bindings = self._mgr.get_bindings(str(ident))
        return dbus.Dictionary(
            {dbus.Int32(k): dbus.Int32(v) for k, v in bindings.items()},
            signature="ii")

    @dbus.service.method(CTRLMGR_IFACE, in_signature="sii", out_signature="b")
    def SetBinding(self, ident, src, dst):
        ok = self._mgr.set_binding(str(ident), int(src), int(dst))
        if ok:
            self.BindingsChanged(str(ident))
        return bool(ok)

    @dbus.service.method(CTRLMGR_IFACE, in_signature="s", out_signature="b")
    def ResetBindings(self, ident):
        ok = self._mgr.reset_bindings(str(ident))
        if ok:
            self.BindingsChanged(str(ident))
        return bool(ok)

    @dbus.service.method(CTRLMGR_IFACE, in_signature="sd", out_signature="b")
    def CaptureStart(self, ident, timeout):
        return bool(
            self._mgr.start_capture(str(ident), float(timeout),
                                    self._capture_done))

    @dbus.service.method(CTRLMGR_IFACE, in_signature="s", out_signature="b")
    def CaptureCancel(self, ident):
        return bool(self._mgr.cancel_capture(str(ident)))

    @dbus.service.signal(CTRLMGR_IFACE, signature="si")
    def CaptureResult(self, ident, code):
        pass

    @dbus.service.signal(CTRLMGR_IFACE, signature="s")
    def BindingsChanged(self, ident):
        pass

    def _capture_done(self, ident, code):
        # Runs on the main loop (scheduled via GLib.idle_add by the capture
        # worker), so emitting the dbus signal from here is safe.
        self.CaptureResult(ident, int(code))


# ── StatusNotifierItem (tray) ────────────────────────────────────────────────

class TrayIcon(dbus.service.Object):

    def __init__(self, bus, manager, menu):
        dbus.service.Object.__init__(self, bus, ITEM_PATH)
        self._mgr  = manager
        self._menu = menu

    def _props(self):
        instances = self._mgr.get_instances()
        active_remaps = [i for i in instances
                         if i.mode not in ("ps5-native", "xbox-native")]
        if active_remaps:
            icon  = "input-gaming"
        else:
            icon  = "input-gaming-symbolic"
        title = "Controller Manager"
        if instances:
            tip = "\n".join(
                f"{inst.name} - {MODE_LABELS[inst.mode]}" for inst in instances)
        else:
            tip = "No controller connected"

        return {
            "Category":      dbus.String("Hardware"),
            "Id":            dbus.String("controller-manager"),
            "Status":        dbus.String("Active"),
            "Title":         dbus.String(title),
            "IconName":      dbus.String(icon),
            "IconThemePath": dbus.String(""),
            "Menu":          dbus.ObjectPath(MENU_PATH),
            "ItemIsMenu":    dbus.Boolean(True),
            "ToolTip":       dbus.Struct(
                [dbus.String(icon), dbus.Array([], "(iiay)"),
                 dbus.String(title), dbus.String(tip)],
                signature="(sa(iiay)ss)"
            ),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self._props().get(prop, dbus.String(""))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self._props()

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def Activate(self, x, y): pass

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def ContextMenu(self, x, y): pass

    @dbus.service.method("org.kde.StatusNotifierItem", in_signature="ii")
    def SecondaryActivate(self, x, y): pass

    def refresh(self):
        self._menu.notify_update()
        self.NewIcon()
        self.NewTitle()
        self.NewToolTip()

    @dbus.service.signal("org.kde.StatusNotifierItem", signature="s")
    def NewStatus(self, s): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewIcon(self): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewTitle(self): pass

    @dbus.service.signal("org.kde.StatusNotifierItem")
    def NewToolTip(self): pass


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    loop = GLib.MainLoop()
    bus  = dbus.SessionBus()
    # Own the conventional StatusNotifierItem well-known name, held for the
    # process lifetime (an unreferenced BusName would be garbage-collected and
    # the name released). Registration itself goes by object path rather than
    # this name (see register_with_watcher); owning it just gives a stable,
    # spec-conventional identity for hosts that look items up by name.
    bus_name = dbus.service.BusName(BUS_NAME, bus)

    tray_ref = [None]

    def on_change():
        if tray_ref[0]:
            tray_ref[0].refresh()

    mgr  = ControllerManager(on_change)

    def _shutdown_and_quit():
        mgr.stop_all()
        loop.quit()

    menu = DbusmenuServer(bus, MENU_PATH, mgr, _shutdown_and_quit)
    tray = TrayIcon(bus, mgr, menu)
    tray_ref[0] = tray
    # Binding-gui API (separate process); shares the daemon's bus and well-known
    # name, so the GUI resolves everything from BUS_NAME alone.
    ctlr = CtlrMgrServer(bus, mgr)

    watcher_name = "org.kde.StatusNotifierWatcher"

    def register_with_watcher():
        try:
            watcher = bus.get_object(watcher_name, "/StatusNotifierWatcher")
            # Register by object path, not by our well-known bus name. The host
            # keys the item on the registration argument: a well-known name is
            # stable across restarts, so a fresh process collides with the dying
            # previous indicator (which the host only tears down ~500ms after our
            # old connection drops) and the registration is silently swallowed.
            # The path form keys on our *unique* connection name instead, so each
            # run is distinct and the stale indicator cleans itself up.
            watcher.RegisterStatusNotifierItem(
                ITEM_PATH, dbus_interface=watcher_name)
        except Exception as ex:
            print(f"controller-manager: watcher registration failed: {ex}",
                  file=sys.stderr)

    # Populate controllers BEFORE announcing ourselves, so the first menu the
    # host reads is already complete (see ControllerManager.start).
    mgr.start()

    # Register exactly once per watcher lifetime, driven by watch_name_owner.
    # Two points matter:
    #   * It must run inside the main loop (watch_name_owner delivers the current
    #     owner asynchronously, i.e. once loop.run() is servicing requests), so we
    #     can answer the watcher's verification call-back - registering before the
    #     loop races and the entry silently never appears.
    #   * It must NOT register twice for the same owner: a duplicate
    #     RegisterStatusNotifierItem makes the host reset the indicator, which
    #     briefly drops its name owner and destroys it ~500ms later (icon flaps in
    #     then vanishes). The guard re-registers only after a real disappear /
    #     reappear of the watcher (login race, shell replacement).
    watcher_registered = [False]

    def _on_watcher_owner(owner):
        if owner and not watcher_registered[0]:
            watcher_registered[0] = True
            register_with_watcher()
        elif not owner:
            watcher_registered[0] = False

    bus.watch_name_owner(watcher_name, _on_watcher_owner)

    def _shutdown(*_):
        mgr.stop_all()
        loop.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    loop.run()


if __name__ == "__main__":
    main()
