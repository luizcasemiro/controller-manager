"""Regression test for ControllerManager._poll reconcile logic: identity keying,
rebind-on-reconnect and the debounce grace window. These guard the property that
a Bluetooth pad re-appearing on a *new* evdev node after a reconnect is rebound
in place (no tray-menu churn) rather than removed and re-added.

Stubs ControllerInstance and _scan, so no hardware / D-Bus / sudo is touched.
Skips (exit 77) if the daemon's runtime deps are unavailable, mirroring how
validate-repo.sh skips shellcheck when it is not installed."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs evdev/dbus/gi")
    # 77, not 0: validate-repo.sh must tell "never ran" from "passed".
    sys.exit(77)

# Never touch the user's real controller-modes.json: adoption persists the
# assigned player numbers through save_config since the player-LED feature.
cm.load_config = lambda: {}
cm.save_config = lambda cfg: None

# Keep a handle on the real ControllerInstance before the reconcile scenarios
# swap in a lightweight fake; Scenario E exercises the real led self-heal.
RealInst = cm.ControllerInstance

# Controllable monotonic clock so the grace window is tested without real waits.
clock = [1000.0]
cm.time.monotonic = lambda: clock[0]

# Lightweight stand-in for ControllerInstance: records what the reconcile did.
class FakeInst:
    def __init__(self, path, name, vendor, product, family, mode, uniq, phys,
                 hidraw, invert_y=False, bindings=None):
        self.path = path; self.name = name; self.vendor = vendor
        self.product = product; self.family = family; self.mode = mode
        self.invert_y = invert_y; self.bindings = dict(bindings or {})
        self.uniq = uniq; self.phys = phys; self.hidraw = hidraw
        self.ident = cm.ident_of(vendor, product, uniq, phys)
        self._gone_since = None
        self.player = None
        self.rebinds = 0; self.stopped = False
    def apply_mode(self, adopt=False): pass
    def rebind(self, path, name, hidraw):
        self.rebinds += 1; self.path = path; self.name = name; self.hidraw = hidraw
    def stop(self): self.stopped = True
    def virtual_path(self): return None
    def remap_healthy(self): return True  # liveness re-assert tested in Scenario H
    def refresh_led(self): pass   # led self-heal is exercised separately (Scenario E)
    def watch_holders(self, steam_game=False): pass  # resting-colour policy needs /proc + sudo - not here

cm.ControllerInstance = FakeInst

def make_mgr(scans):
    mgr = cm.ControllerManager(on_change_cb=lambda: None)
    it = iter(scans)
    mgr._scan = lambda: next(it)
    return mgr

def dev(path, uniq="ac:36:1b:70:70:e8", phys=""):
    return {"path": path, "name": "DualSense", "vendor": 0x054c,
            "product": 0x0ce6, "family": "ps5", "uniq": uniq, "phys": phys,
            "hidraw": []}

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

# ── Scenario A: connect -> steady -> RECONNECT on a new node ──────────────────
print("Scenario A: reconnect must rebind in place (no menu change)")
mgr = make_mgr([[dev("event25")], [dev("event25")], [dev("event30")]])
c1 = mgr._poll()
inst = next(iter(mgr._instances.values()))
check(c1 is True,               "connect -> changed=True (menu rebuild)")
check(len(mgr._instances) == 1, "one instance after connect")
check(mgr._poll() is False,     "steady state -> changed=False")
check(mgr._poll() is False,     "reconnect on new node -> changed=False (NO menu churn)")
check(inst.rebinds == 1,        "reconnect -> rebind() called exactly once")
check(inst.path == "event30",   "instance now bound to the new node")
check(len(mgr._instances) == 1 and inst.stopped is False,
      "same instance kept (never stopped/recreated)")

# ── Scenario B: brief disconnect inside grace, then back ────────────────────
print("Scenario B: blip shorter than grace keeps the instance")
mgr = make_mgr([[dev("event25")], [], [dev("event30")]])
mgr._poll()
inst = next(iter(mgr._instances.values()))
clock[0] += 2.0
check(mgr._poll() is False,     "absent <grace -> changed=False (kept)")
check(len(mgr._instances) == 1, "instance retained during blip")
clock[0] += 1.0
check(mgr._poll() is False,     "return within grace -> changed=False")
check(inst.rebinds == 1,        "return -> rebind() once")

# ── Scenario C: real disconnect longer than grace -> removed ────────────────
print("Scenario C: absence beyond grace removes the instance")
mgr = make_mgr([[dev("event25")], [], []])
mgr._poll()
inst = next(iter(mgr._instances.values()))
clock[0] += 1.0
mgr._poll()
clock[0] += cm.REMOVE_GRACE + 0.1
check(mgr._poll() is True,      "absent >grace -> changed=True (menu rebuild)")
check(len(mgr._instances) == 0, "instance removed")
check(inst.stopped is True,     "removed instance was stop()'d")

# ── Scenario D: a genuinely different controller is a real change ──────────
print("Scenario D: a different pad (new identity) is a structural change")
mgr = make_mgr([[dev("event25", uniq="AA:AA")],
                [dev("event25", uniq="AA:AA"), dev("event40", uniq="BB:BB")]])
mgr._poll()
check(mgr._poll() is True,      "new distinct controller -> changed=True")
check(len(mgr._instances) == 2, "both controllers tracked")

# ── Scenario G: reconnect on the *reused* evdev path (same eventX number) ────
# The pad vanishes and returns on the *same* /dev/input/eventX. rebind() must
# still fire so the mode is re-asserted (remapper restarted, hidraw re-gated,
# lightbar repainted) - without it the pad comes back un-remapped and wrongly
# coloured until a manual switch. It is not a structural change (no menu churn).
print("Scenario G: reconnect on a reused evdev path must re-assert the mode")
mgr = make_mgr([[dev("event25")], [], [dev("event25")]])
mgr._poll()
inst = next(iter(mgr._instances.values()))
clock[0] += 1.0
check(mgr._poll() is False,  "absent <grace -> kept, no menu change")
check(inst.rebinds == 0,     "no rebind while absent")
clock[0] += 1.0
check(mgr._poll() is False,  "return on same path -> changed=False (no menu churn)")
check(inst.rebinds == 1,     "reconnect on reused path -> rebind() (mode re-asserted)")

# ── Scenario E: lightbar node lags evdev on reconnect -> self-heal ───────────
print("Scenario E: :rgb:indicator node absent at bind time -> applied once it appears")
led_calls = []
cm.hidraw_gate = lambda *a, **k: None          # no sudo/hidraw in the test
cm.led_set_raw = lambda hidraw, rgb: led_calls.append(rgb)
resolver = [None]                              # what led_indicator_for_event returns
cm.led_indicator_for_event = lambda path: resolver[0]
# The hidraw node the lightbar is written to renumbers together with the
# rgb-indicator node, so track the same resolver (non-empty only when present).
cm.hidraw_for_event = lambda path: [resolver[0]] if resolver[0] else []

# Pad (re)binds while the driver has not created the LED node yet.
inst = RealInst("event30", "DualSense", 0x054c, 0x0ce6,
                "ps5", "ps5-native", "ac:36:1b:70:70:e8", "", [])
check(inst.led is None,        "led unresolved while node absent at bind")
inst.refresh_led()
check(inst.led is None and not led_calls,
      "tick with node still absent -> no-op (no colour written)")

resolver[0] = "input30:rgb:indicator"          # driver has now created the node
inst.refresh_led()
check(inst.led == "input30:rgb:indicator",     "node appeared -> led resolved")
check(led_calls == [(0, 0, 255)],
      "colour applied exactly once (blue = ps5-native)")

inst.refresh_led()                             # further ticks must not re-write
check(len(led_calls) == 1,     "already bound -> subsequent ticks are no-ops")

# ── Scenario F: LED node renumbers while the evdev path is reused ────────────
# The real regression: a reconnect bumps the inputN-derived :rgb:indicator name
# (input37 -> input44) but the /dev/input/eventX path is reused, so the
# path-keyed rebind() never fires. The daemon must still repaint the NEW node.
print("Scenario F: node renumbers on reconnect (evdev path reused) -> repaint new node")
led_calls.clear()
resolver[0] = "input37:rgb:indicator"          # node present at bind
inst = RealInst("event25", "DualSense", 0x054c, 0x0ce6,
                "ps5", "ps5-xbox", "ac:36:1b:70:70:e8", "", [])
check(inst.led == "input37:rgb:indicator", "led bound to the node present at construct")
check(not led_calls,                       "construct itself does not paint")

resolver[0] = "input44:rgb:indicator"          # reconnect renumbered the node
inst.refresh_led()
check(inst.led == "input44:rgb:indicator", "renumbered node picked up on next tick")
check(led_calls == [(0, 128, 0)],
      "NEW node repainted exactly once (green = ps5-xbox)")

inst.refresh_led()                             # node stable now
check(len(led_calls) == 1,                 "stable node -> no repaint spam")

# ── Scenario H: dead remapper thread on a steady pad -> mode re-asserted ─────
# A BT blip shorter than one poll interval kills the remapper's grab without
# the pad ever appearing absent. The reconcile must notice the dead thread
# (remap_healthy() False) and re-assert the mode, without any menu churn.
print("Scenario H: dead remapper on a present pad -> re-assert, no menu churn")
class SickInst(FakeInst):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.healthy = True
    def remap_healthy(self):
        return self.healthy

cm.ControllerInstance = SickInst
mgr = make_mgr([[dev("event25")], [dev("event25")], [dev("event25")]])
mgr._poll()
inst = next(iter(mgr._instances.values()))
check(mgr._poll() is False,  "healthy steady state -> no rebind trigger")
check(inst.rebinds == 0,     "healthy -> rebind not called")
inst.healthy = False                        # grab died between polls
check(mgr._poll() is False,  "re-assert is not a structural change (no menu churn)")
check(inst.rebinds == 1,     "dead remapper -> rebind() re-asserts the mode")
cm.ControllerInstance = FakeInst

# ── Scenario I: two identical uniq-less pads stay two instances ─────────────
# USB pads on xpad report no serial (uniq empty); with a vendor:product
# fallback alone, two identical pads would collapse into one instance. The
# physical attachment point keeps them apart.
print("Scenario I: identical pads without uniq are kept apart by phys")
mgr = make_mgr([[dev("event10", uniq="", phys="usb-0000:0c:00.3-2/input0"),
                 dev("event11", uniq="", phys="usb-0000:0c:00.3-4/input0")]])
check(mgr._poll() is True,      "two new pads -> changed=True")
check(len(mgr._instances) == 2, "identical uniq-less pads tracked separately")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
