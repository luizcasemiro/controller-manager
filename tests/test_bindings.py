#!/usr/bin/env python3
"""
Per-controller button bindings: composition (quirk -> user -> target),
config persistence shape, manager set/clear/reset, capture_button reading and
the capture guard in the reconcile pass.

Plain script like the other tests/: prints OK / FAIL lines and exits non-zero
on failure, 77 when the runtime deps are missing.
"""

import importlib.util, copy, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

spec = importlib.util.spec_from_file_location("ctrlmgr_bind", MODULE)
cm = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(cm)
except Exception:           # runtime deps missing (evdev/dbus/gi)
    try:
        import evdev, dbus  # noqa: F401  (import probe only)
    except Exception:
        print("SKIP: runtime deps missing", file=sys.stderr)
        sys.exit(77)
    raise

fails = []

def check(cond, msg):
    if cond:
        print(f"  OK  {msg}")
    else:
        print(f"FAIL  {msg}")
        fails.append(msg)

from evdev import ecodes as e

# ── composition: quirk, user wins ────────────────────────────────────────────
print("Scenario A: compose_button_maps with a per-controller user layer")

quirk_simple = {e.BTN_SOUTH: e.BTN_NORTH}          # physical src -> standard

# Baseline: no user map == the plain quirk mapping.
m = cm.compose_button_maps(quirk_simple, None)
check(m[e.BTN_SOUTH] == e.BTN_NORTH,
      "no user map keeps the quirk mapping (BTN_SOUTH lands on BTN_NORTH)")
check(e.BTN_NORTH not in m,
      "codes outside the mappings pass through unchanged")

# A user bind overrides only its own physical button.
m2 = cm.compose_button_maps(quirk_simple,
                            {e.BTN_SOUTH: e.BTN_TL})
check(m2[e.BTN_SOUTH] == e.BTN_TL and e.BTN_NORTH not in m2,
      "user bind wins on its own code; neighbours keep the program map")

# User values are output-identity codes, sent verbatim (already resolved).
m3 = cm.compose_button_maps(None, {e.BTN_SOUTH: e.BTN_WEST})
check(m3[e.BTN_SOUTH] == e.BTN_WEST,
      "user value is emitted at the target identity as-is")

# Empty everywhere -> None (remapper then does plain passthrough).
check(cm.compose_button_maps(None, None) is None
      and cm.compose_button_maps({}, {}) is None,
      "all layers empty -> None (no remap at all)")

# ── config key shape round-trips through JSON ────────────────────────────────
print("Scenario B: _bindings config normalisation")

norm = cm.ControllerManager._norm_bindings({"289": "304", 305: "306", "x": "y"})
check(norm == {289: 304, 305: 306},
      "JSON round-tripped string keys normalise back to int ecodes")

# ── manager set / clear / reset with a fake instance ─────────────────────────
print("Scenario C: manager set_binding / reset_bindings persist per-controller")

saved = []
cm.save_config = lambda cfg: saved.append(copy.deepcopy(cfg))

class FakeInst:
    def __init__(self, *args, **kw):
        self.ident = kw.get("ident", args[0] if args else "unknown")
        self.mode = kw.get("mode", "ps5-xbox")
        self.path = kw.get("path")
        self.family = kw.get("family", "xbox")
        self.player = kw.get("player")
        self.bindings = dict(kw.get("bindings") or {})
        self._gone_since = None
        self.applies = 0
    def apply_mode(self, adopt=False): self.applies += 1
    def rebind(self, *a, **kw): raise AssertionError("should not rebind")
    def stop(self): pass
    def virtual_path(self): return None
    def remap_healthy(self): return True
    def refresh_led(self): pass
    def watch_holders(self, steam_game=False): pass

cm.ControllerInstance = FakeInst
cm.load_config = lambda: {}        # never depend on the live user config
mgr = cm.ControllerManager(on_change_cb=lambda: None)
mgr._gui_proc = None
mgr._instances.update({
    "pad-a": FakeInst("pad-a"),
    "pad-b": FakeInst("pad-b"),
})

check(mgr.set_binding("pad-a", e.BTN_SOUTH, e.BTN_TL) is True,
      "set_binding on a known controller returns True")
check(mgr.set_binding("pad-zzz", e.BTN_SOUTH, e.BTN_TL) is False,
      "set_binding on an unknown controller returns False")

a = mgr._instances["pad-a"]
check(a.bindings == {e.BTN_SOUTH: e.BTN_TL} and a.applies == 1,
      "instance holds the user map and the remap was re-asserted")
check(mgr.get_bindings("pad-a") == {e.BTN_SOUTH: e.BTN_TL},
      "get_bindings returns the int-keyed map")
check(mgr.get_bindings("pad-b") == {},
      "the OTHER controller keeps its own (empty) map")
check(len(saved) == 1 and cm.BINDINGS_KEY in saved[0]
      and saved[0][cm.BINDINGS_KEY]["pad-a"] == {str(e.BTN_SOUTH): e.BTN_TL},
      "config persisted under _bindings keyed per-ident")

# Clear one binding -> falls back (pad-a map empties out entirely here).
mgr.set_binding("pad-a", e.BTN_SOUTH, -1)
check(a.bindings == {} and cm.BINDINGS_KEY not in saved[1],
      "clearing the only bind drops the whole _bindings key")

mgr.set_binding("pad-a", e.BTN_SOUTH, e.BTN_TL)
mgr.set_binding("pad-a", e.BTN_EAST, e.BTN_WEST)
mgr.reset_bindings("pad-a")
check(a.bindings == {} and a.applies >= 3,
      "reset_bindings empties the map and re-asserts")
check(cm.BINDINGS_KEY not in saved[-1],
      "reset removes the per-controller entry from the saved config")

# The other controller is unaffected by pad-a's edits.
b = mgr._instances["pad-b"]
check(b.bindings == {} and b.applies == 0,
      "pad-b never re-asserted - each controller has its own bind set")

# Capture guard: busy or unknown controllers refuse to start a capture.
mgr._capturing_ident = "pad-a"
check(mgr.start_capture("pad-b", 1.0, None) is False,
      "a second concurrent capture is refused")
mgr._capturing_ident = None
check(mgr.start_capture("pad-zzz", 1.0, None) is False,
      "capture on an unknown controller is refused")

# ── capture_button reads the next physical press ─────────────────────────────
print("Scenario D: capture_button honours press/repeat/release/timeout/cancel")

class FakePad:
    def __init__(self, wfd, events):
        self.fd = wfd          # real fd so select() works
        self._events = list(events)
    def read(self):
        return list(self._events)

def mk_events(pairs):
    return [cm.evdev.InputEvent(0, 0, t, c, v) for t, c, v in pairs]

def open_pair():
    r, w = os.pipe()
    return r, w

r, w = open_pair()
os.write(w, b"x")
pad = FakePad(r, mk_events([(e.EV_KEY, e.BTN_SOUTH, 1)]))
check(cm.capture_button(pad, 5.0) == e.BTN_SOUTH,
      "first EV_KEY down-press is captured")
os.close(r); os.close(w)

r, w = open_pair()
os.write(w, b"x")
pad = FakePad(r, mk_events([(e.EV_KEY, e.BTN_SOUTH, 0),   # a release first
                            (e.EV_KEY, e.BTN_SOUTH, 2),   # auto-repeat
                            (e.EV_KEY, e.BTN_EAST, 1)]))  # the real press
check(cm.capture_button(pad, 5.0) == e.BTN_EAST,
      "releases and auto-repeat are skipped")
os.close(r); os.close(w)

r, w = open_pair()
pad = FakePad(r, [])
t0 = time.monotonic()
check(cm.capture_button(pad, 0.1) == 0 and time.monotonic() - t0 < 1.0,
      "idle pad times out with 0")
os.close(r); os.close(w)

cancel = cm.threading.Event()
cancel.set()
r, w = open_pair()
pad = FakePad(r, [])
check(cm.capture_button(pad, 30.0, cancel) == 0,
      "cancel event aborts a pending capture immediately")
os.close(r); os.close(w)

# ── reconcile leaves a mid-capture pad alone ─────────────────────────────────
print("Scenario E: _poll does not re-grab while a pad is being captured")

class RebindCounter(FakeInst):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rebinds = 0
        self.healthy = False          # looks dead -> normally re-asserted
    def remap_healthy(self):
        return self.healthy
    def rebind(self, *a, **kw):
        self.rebinds += 1

mgr._instances.clear()
ident = cm.ident_of(0x045e, 0x028e, "u", "")
inst = RebindCounter(ident=ident, player=1, path="/dev/input/event9")
mgr._instances[ident] = inst

def dev():
    return {"path": "/dev/input/event9", "name": "X",
            "vendor": 0x045e, "product": 0x028e, "family": "xbox",
            "uniq": "u", "phys": "", "hidraw": []}

scans = iter([[dev()], [dev()], [dev()]])
def one_scan():
    return next(scans, [])
mgr._scan = one_scan

mgr._capturing_ident = ident
check(mgr._poll() is False, "reconcile pass runs while capturing")
check(inst.rebinds == 0,     "no re-assert fired into the active capture")
mgr._capturing_ident = None
mgr._poll()
check(inst.rebinds == 1,     "once capture ends the normal re-assert resumes")

print("")
if fails:
    print(f"RESULT: {len(fails)} FAILURE(S)")
    sys.exit(1)
print("RESULT: ALL PASS")