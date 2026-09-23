"""Regression test for the multi-controller path: four pads (2x DualSense +
2x Xbox) must be tracked as independent instances - per-pad mode, per-pad
reconnect handling, per-pad tray-menu routing, and 'DualSense 1 / 2' numbering
for identical models. Also guards the scan filter: our own virtual output pads
are recognised by python-evdev's uinput phys marker, NOT by name - a real
wired Xbox 360 pad is named exactly like VIRTUAL_XBOX by xpad and must still
be adopted.

Stubs ControllerInstance, _scan, evdev and the config I/O, so no hardware /
D-Bus / sudo / real config file is touched. Skips (exit 77) if the daemon's
runtime deps are unavailable, mirroring test_reconcile.py."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_multi", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs evdev/dbus/gi")
    # 77, not 0: validate-repo.sh must tell "never ran" from "passed".
    sys.exit(77)

# Never touch the user's real controller-modes.json. config_store feeds what
# a newly built manager loads, so a scenario can replay a 'previous session'.
import json
saved_configs = []
config_store = [{}]
cm.load_config = lambda: json.loads(json.dumps(config_store[0]))
cm.save_config = lambda cfg: saved_configs.append(json.loads(json.dumps(cfg)))

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

# Lightweight stand-in for ControllerInstance (same shape as test_reconcile.py).
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
        self.rebinds = 0; self.applies = 0; self.stopped = False
    def apply_mode(self, adopt=False): self.applies += 1
    def rebind(self, path, name, hidraw):
        self.rebinds += 1; self.path = path; self.name = name; self.hidraw = hidraw
    def stop(self): self.stopped = True
    def virtual_path(self): return None
    def remap_healthy(self): return True
    def refresh_led(self): pass
    def watch_holders(self, steam_game=False): pass
    def _apply_player_leds(self): pass

cm.ControllerInstance = FakeInst

def make_mgr(scans):
    mgr = cm.ControllerManager(on_change_cb=lambda: None)
    it = iter(scans)
    mgr._scan = lambda: next(it)
    return mgr

def ds(path, uniq):
    return {"path": path, "name": "DualSense", "vendor": 0x054c,
            "product": 0x0ce6, "family": "ps5", "uniq": uniq, "phys": "",
            "hidraw": []}

def xb(path, name, product, uniq="", phys=""):
    return {"path": path, "name": name, "vendor": 0x045e, "product": product,
            "family": "xbox", "uniq": uniq, "phys": phys, "hidraw": []}

# The verification fleet: two identical DualSense (distinct BT MACs) and two
# uniq-less USB Xbox pads of different models (distinct phys).
FLEET = [ds("event10", "AA:AA"), ds("event11", "BB:BB"),
         xb("event12", "Xbox One S", 0x02ea, phys="usb-0000:0c:00.3-2/input0"),
         xb("event13", "Xbox Series X/S", 0x0b12, phys="usb-0000:0c:00.3-4/input0")]

def by_ident(mgr):
    return dict(mgr._instances)

# ── Scenario A: four pads become four independent instances ─────────────────
print("Scenario A: 2x DualSense + 2x Xbox -> four instances, per-family defaults")
mgr = make_mgr([list(FLEET), list(FLEET)])
check(mgr._poll() is True,      "four new pads -> changed=True")
check(len(mgr._instances) == 4, "all four pads tracked as separate instances")
insts = by_ident(mgr)
check(all(i.mode == "ps5-native" for i in insts.values() if i.family == "ps5")
      and all(i.mode == "xbox-native" for i in insts.values() if i.family == "xbox"),
      "each instance starts on its family's native default")
check(mgr._poll() is False,     "steady state with four pads -> changed=False")

# ── Scenario B: one pad's reconnect leaves the other three untouched ────────
print("Scenario B: single-pad reconnect churn stays confined to that pad")
moved = [ds("event30", "AA:AA")] + FLEET[1:]
mgr = make_mgr([list(FLEET), moved])
mgr._poll()
insts = by_ident(mgr)
check(mgr._poll() is False,     "one pad on a new node -> no structural change")
check(insts["AA:AA"].rebinds == 1 and insts["AA:AA"].path == "event30",
      "reconnected pad rebound to its new node")
check(all(i.rebinds == 0 for k, i in insts.items() if k != "AA:AA"),
      "the other three pads saw no rebind")

# ── Scenario C: one pad removed, three remain ────────────────────────────────
print("Scenario C: one pad unplugged past grace -> only that instance dropped")
clock = [1000.0]
cm.time.monotonic = lambda: clock[0]
mgr = make_mgr([list(FLEET), FLEET[:3], FLEET[:3]])
mgr._poll()
gone = by_ident(mgr)[cm.ident_of(0x045e, 0x0b12, "", "usb-0000:0c:00.3-4/input0")]
mgr._poll()
clock[0] += cm.REMOVE_GRACE + 0.1
check(mgr._poll() is True,      "removal past grace -> changed=True")
check(len(mgr._instances) == 3, "three instances remain")
check(gone.stopped is True,     "only the unplugged pad was stop()'d")
check(all(not i.stopped for i in mgr._instances.values()),
      "remaining pads never stopped")

# ── Scenario D: set_mode targets exactly one pad, config keyed per ident ────
print("Scenario D: set_mode is per-pad; config persists per ident")
saved_configs.clear()
mgr = make_mgr([list(FLEET)])
mgr._poll()
insts = by_ident(mgr)
applies_before = {k: i.applies for k, i in insts.items()}
mgr.set_mode("BB:BB", "ps5-xbox")
check(insts["BB:BB"].mode == "ps5-xbox", "target pad switched to ps5-xbox")
check(insts["BB:BB"].applies == applies_before["BB:BB"] + 1,
      "target pad's mode re-applied")
check(all(insts[k].mode != "ps5-xbox" and insts[k].applies == applies_before[k]
          for k in insts if k != "BB:BB"),
      "the other three pads untouched by the switch")
check(saved_configs and saved_configs[-1].get("BB:BB") == "ps5-xbox",
      "config saved keyed by the pad's stable ident only")
check(saved_configs[-1].get("_players", {}).get("AA:AA") == 1,
      "assigned player numbers persisted under the _players config key")
check(all(getattr(i, "_player_repaint_at", None) is not None
          for i in insts.values() if i.family == "ps5"),
      "mode switch arms the player-LED re-assert on every ps5 pad")

# ── Scenario D2: player numbers follow connection order; freed one is reused ─
print("Scenario D2: stable player numbers; a freed number is reused, others keep theirs")
mgr = make_mgr([list(FLEET), FLEET[1:],
                [ds("event40", "CC:CC")] + FLEET[1:]])
mgr._poll()
players = {k: i.player for k, i in by_ident(mgr).items()}
check(players == {"AA:AA": 1, "BB:BB": 2,
                  "phys:usb-0000:0c:00.3-2/input0": 3,
                  "phys:usb-0000:0c:00.3-4/input0": 4},
      "players 1-4 assigned in adoption order across families")
clock[0] += 1.0
mgr._poll()                                  # AA absent, inside grace
clock[0] += cm.REMOVE_GRACE + 0.1
mgr._poll()                                  # AA dropped; CC adopted
insts = by_ident(mgr)
check(insts["CC:CC"].player == 1,   "next new pad takes the freed number 1")
check(insts["BB:BB"].player == 2,   "surviving pad keeps its number")

# ── Scenario D3: persisted numbers beat the restart adoption order ──────────
# A daemon restart re-adopts in evdev node order, which gate rebinds shuffle;
# the number a pad was FIRST adopted under must win, not its position today.
print("Scenario D3: persisted player numbers survive a restart in swapped order")
config_store[0] = saved_configs[-1]        # what the previous session persisted
mgr = make_mgr([[FLEET[1], FLEET[0]]])     # restart adopts DualSense 2 first
mgr._poll()
insts = by_ident(mgr)
check(insts["AA:AA"].player == 1 and insts["BB:BB"].player == 2,
      "numbers follow the persisted identity, not today's adoption order")
config_store[0] = {}

# ── Scenario E: menu numbering - duplicates numbered, unique names plain ────
print("Scenario E: 'DualSense 1 / 2' numbering for identical models only")
class Pad:
    def __init__(self, ident, name, family="ps5", mode="ps5-native", player=None):
        self.ident = ident; self.name = name
        self.family = family; self.mode = mode; self.player = player

label = cm.DbusmenuServer._inst_label   # uses no self state
two_ds = [Pad("AA:AA", "DualSense", player=1),
          Pad("BB:BB", "DualSense", player=2),
          Pad("phys:u1", "Xbox One S", "xbox", "xbox-native", player=3)]
check(label(None, two_ds[0], two_ds) == "DualSense 1"
      and label(None, two_ds[1], two_ds) == "DualSense 2",
      "two identical models numbered by their player number")
check(label(None, two_ds[2], two_ds) == "Xbox One S",
      "a unique model keeps its plain name")
reordered = [two_ds[1], two_ds[0], two_ds[2]]
check(label(None, two_ds[0], reordered) == "DualSense 1",
      "number sticks to the pad, not to its list position")

# ── Scenario F: menu click routing with four pads ────────────────────────────
print("Scenario F: radio clicks route to exactly the pad under their header")
class FakeMgr:
    def __init__(self, instances):
        self.instances = instances
        self.set_mode_calls = []
    def get_instances(self):
        return list(self.instances)
    def set_mode(self, ident, mode):
        self.set_mode_calls.append((ident, mode))

fleet_pads = [Pad("AA:AA", "DualSense", player=1),
              Pad("BB:BB", "DualSense", player=2),
              Pad("phys:u1", "Xbox One S", "xbox", "xbox-native", player=3),
              Pad("phys:u2", "Xbox Series X/S", "xbox", "xbox-native", player=4)]
fmgr = FakeMgr(fleet_pads)
menu = object.__new__(cm.DbusmenuServer)  # no bus: skip dbus Object init
menu._mgr = fmgr; menu._on_quit = lambda: None
menu._revision = 1
menu._items = []; menu._lookup = {}; menu._sig = None; menu._next_id = 1
menu.LayoutUpdated = lambda rev, parent: None
menu.ItemsPropertiesUpdated = lambda updated, removed: None
menu.notify_update()
check(sorted({i for i, _ in menu._lookup.values()})
      == ["AA:AA", "BB:BB"],
      "multi-mode pads (DualSense) have clickable radios; single-mode "
      "pads (Xbox) do not")
ds2_xbox = next(i for i, hit in menu._lookup.items()
                if hit == ("BB:BB", "ps5-xbox"))
menu.Event(ds2_xbox, "clicked", 0, 0)
check(fmgr.set_mode_calls == [("BB:BB", "ps5-xbox")],
      "click on DualSense 2's radio reaches exactly that pad")
labels = [str(p["label"]) for _, p in menu._items if "label" in p]
check("DualSense 1" in labels and "DualSense 2" in labels,
      "menu shows the numbered headers")
# A single-choice family shows its one mode as a static, disabled line rather
# than a lone always-checked radio - one per Xbox pad.
disabled = [str(p["label"]) for _, p in menu._items
            if "label" in p and not p.get("enabled", True)]
check(disabled.count(cm.MODE_LABELS["xbox-native"]) == 2,
      "each single-mode Xbox pad shows its mode as a non-interactive line")

# ── Scenario G: scan filter - phys marker excludes ours, real X360 adopted ──
print("Scenario G: virtual pads excluded by uinput phys; real X360 pad adopted")
class Info:
    def __init__(self, vendor, product):
        self.vendor = vendor; self.product = product

class FakeDev:
    def __init__(self, name, vendor, product, uniq="", phys="", gamepad=True):
        self.name = name; self.info = Info(vendor, product)
        self.uniq = uniq; self.phys = phys
        self._keys = [cm.e.BTN_SOUTH] if gamepad else [cm.e.BTN_LEFT]
    def capabilities(self):
        return {cm.e.EV_KEY: self._keys}
    def close(self): pass

devices = {
    # Our own virtual output pad: name collides with a real X360 pad, but the
    # uinput phys marker identifies it.
    "event50": FakeDev("Microsoft X-Box 360 pad", 0x045e, 0x028e,
                       phys="py-evdev-uinput"),
    # A REAL wired Xbox 360 pad: identical name, but a physical usb phys.
    "event51": FakeDev("Microsoft X-Box 360 pad", 0x045e, 0x028e,
                       phys="usb-0000:0c:00.3-2/input0"),
    # Sunshine/inputtino virtual DualSense for a stream client.
    "event52": FakeDev("DualSense (virtual)", 0x054c, 0x0ce6,
                       phys="INPUTTINO_BT_LINK"),
    # Sony vendor id but not a gamepad node (headset/touchpad sibling).
    "event53": FakeDev("DualSense Touchpad", 0x054c, 0x0ce6,
                       uniq="AA:AA", gamepad=False),
    # A normal BT DualSense.
    "event54": FakeDev("Wireless Controller", 0x054c, 0x0ce6, uniq="AA:AA",
                       phys="e8:9c:25:aa:aa:aa"),
}
cm.evdev.list_devices = lambda: sorted(devices)
cm.evdev.InputDevice = lambda path: devices[path]
cm.hidraw_for_event = lambda path: []

found = {d["path"]: d for d in cm.scan_controllers()}
check("event50" not in found, "own virtual pad (uinput phys) excluded")
check("event51" in found and found["event51"]["family"] == "xbox",
      "REAL wired Xbox 360 pad adopted despite the name collision")
check("event52" not in found, "inputtino stream-client pad excluded")
check("event53" not in found, "non-gamepad Sony node excluded")
check("event54" in found and found["event54"]["name"] == "DualSense",
      "real DualSense adopted with its display name")

# ── Scenario H: auto-compaction closes a numbering gap after COMPACT_GRACE ───
print("Scenario H: a pad left off past grace, then the gap is compacted on time")
saved_configs.clear(); config_store[0] = {}
clock[0] = 5000.0
two = [ds("event60", "AA:AA"), ds("event61", "BB:BB")]
mgr = make_mgr([list(two), [two[1]], [two[1]], [two[1]], [two[1]]])
mgr._poll()                                    # adopt AA=1, BB=2
insts = by_ident(mgr)
check(insts["AA:AA"].player == 1 and insts["BB:BB"].player == 2,
      "two pads adopted as players 1 and 2")
mgr._poll()                                    # AA absent, within grace
check(mgr._compact_due is None, "no gap while the dropped pad is still in grace")
clock[0] += cm.REMOVE_GRACE + 0.1
mgr._poll()                                    # AA dropped -> gap {2}, arm timer
check(len(mgr._instances) == 1, "AA dropped past grace")
check(by_ident(mgr)["BB:BB"].player == 2, "BB keeps its number while the timer runs")
check(mgr._compact_due is not None, "a standing gap arms the compaction timer")
clock[0] += 1.0
mgr._poll()                                    # still before due
check(by_ident(mgr)["BB:BB"].player == 2, "no compaction before COMPACT_GRACE")
clock[0] += cm.COMPACT_GRACE
mgr._poll()                                    # due -> compact
check(by_ident(mgr)["BB:BB"].player == 1, "gap compacted: BB renumbered to 1")
check(mgr._compact_due is None, "timer disarmed once the numbering is contiguous")
check(saved_configs and saved_configs[-1].get("_players", {}).get("BB:BB") == 1,
      "compacted number persisted under _players")

# ── Scenario I: a returning pad within grace reclaims its number, no renumber ─
print("Scenario I: battery swap inside the window reclaims the number, no compaction")
config_store[0] = {}
clock[0] = 5500.0
two = [ds("event62", "CC:CC"), ds("event63", "DD:DD")]
back = ds("event64", "CC:CC")                   # CC returns on a new node
mgr = make_mgr([list(two), [two[1]], [back, two[1]], [back, two[1]]])
mgr._poll()                                     # CC=1, DD=2
mgr._poll()                                     # CC absent, within grace
clock[0] += 1.0
mgr._poll()                                     # CC back before grace elapses
check(mgr._compact_due is None, "reclaim leaves the numbering contiguous, no timer")
insts = by_ident(mgr)
check(insts["CC:CC"].player == 1 and insts["DD:DD"].player == 2,
      "returning pad kept its number; nobody was renumbered")

# ── Scenario J: manual renumber compacts immediately, order-preserving ───────
print("Scenario J: 'Renumber players' compacts now, preserving order")
saved_configs.clear(); config_store[0] = {}
clock[0] = 6000.0
three = [ds("e70", "AA:AA"), ds("e71", "BB:BB"), ds("e72", "CC:CC")]
mgr = make_mgr([list(three), [three[0], three[2]], [three[0], three[2]]])
mgr._poll()                                     # AA=1, BB=2, CC=3
mgr._poll()                                     # BB absent, within grace
clock[0] += cm.REMOVE_GRACE + 0.1
mgr._poll()                                     # BB dropped -> gap {1,3}
check(by_ident(mgr)["CC:CC"].player == 3, "CC still 3 before the manual renumber")
mgr.renumber()
insts = by_ident(mgr)
check(insts["AA:AA"].player == 1 and insts["CC:CC"].player == 2,
      "manual renumber compacts to 1,2 preserving order (AA before CC)")
check(mgr._compact_due is None, "manual renumber clears any pending timer")

# ── Scenario K: the gap surfaces a 'Renumber players' entry that routes ──────
print("Scenario K: a numbering gap surfaces a Renumber players entry")
class GapMgr:
    def __init__(self, instances):
        self.instances = instances; self.renumber_calls = 0
    def get_instances(self): return list(self.instances)
    def set_mode(self, ident, mode): pass
    def renumber(self): self.renumber_calls += 1
def build_menu(mgr):
    m = object.__new__(cm.DbusmenuServer)
    m._mgr = mgr; m._on_quit = lambda: None; m._revision = 1
    m._items = []; m._lookup = {}; m._actions = {}; m._sig = None; m._next_id = 1
    m.LayoutUpdated = lambda rev, parent: None
    m.ItemsPropertiesUpdated = lambda updated, removed: None
    m.notify_update()
    return m
gap_menu = build_menu(GapMgr([Pad("AA:AA", "DualSense", player=1),
                              Pad("BB:BB", "DualSense", player=3)]))  # missing 2
action_ids = [i for i, p in gap_menu._items
              if str(p.get("label", "")) == "Renumber players"]
check(len(action_ids) == 1, "gap surfaces exactly one Renumber players entry")
gap_menu.Event(action_ids[0], "clicked", 0, 0)
check(gap_menu._mgr.renumber_calls == 1, "clicking Renumber players calls mgr.renumber")
contig_menu = build_menu(GapMgr([Pad("AA:AA", "DualSense", player=1),
                                 Pad("BB:BB", "DualSense", player=2)]))
check(not any(str(p.get("label", "")) == "Renumber players"
              for _, p in contig_menu._items),
      "contiguous numbering shows no Renumber players entry")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
