"""Regression test for the lightbar firmware-latch handling around adoption
and gate rebinds. Field bug (second DualSense, 2026-07-06): Steam Input holds
a pad's hidraw fd from the moment it CONNECTS and firmware-latches the
lightbar dark; for a freshly paired identity the adoption 'restore' is not a
marker transition, so nothing rebound the driver and every later kernel LED
write was a firmware no-op - tray and modes worked, the pad just never showed
a colour. Guards:

  * adoption of a native pad with pre-existing foreign holders forces a
    'rearm' (driver rebind resets the latch);
  * adoption without holders, and plain mode switches, must NOT rearm (a
    rearm yanks a running game's fd);
  * after every apply_mode a one-shot delayed repaint is armed - the write
    right after a rebind races the BT re-probe and can be dropped - and it
    fires in remap modes too, where the holder policy itself is inactive.

Stubs the gate, holder scan, LED helpers and controller scan; the real
ControllerInstance runs. Skips (exit 77) when the daemon's runtime deps are
unavailable, mirroring test_reconcile.py."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_latch", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs evdev/dbus/gi")
    # 77, not 0: validate-repo.sh must tell "never ran" from "passed".
    sys.exit(77)

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

# Controllable clock; no real waits.
clock = [1000.0]
cm.time.monotonic = lambda: clock[0]
cm.time.sleep = lambda s: None

UNIQ = "48:18:8d:53:37:6e"
gate_calls = []
holders = [set()]                  # what hidraw_holders reports
led_calls = []

player_calls = []

cm.hidraw_gate = lambda action, uniq, nodes: gate_calls.append(action)
cm.hidraw_holders = lambda nodes: set(holders[0])
cm.led_set_raw = lambda hidraw, rgb: led_calls.append(rgb)
cm.led_set_player = lambda prefix, player: player_calls.append((prefix, player))
cm.led_indicator_for_event = lambda path: "input132:rgb:indicator" if path else None
cm.hidraw_for_event = lambda path: ["/dev/hidraw1"] if path else []
cm.scan_controllers = lambda exclude_paths=frozenset(): [
    {"path": "event17", "name": "DualSense", "vendor": 0x054c,
     "product": 0x0ce6, "family": "ps5", "uniq": UNIQ, "phys": "",
     "hidraw": ["/dev/hidraw1"]}]

class FakeRemapper:
    """No uinput/evdev in the test; apply_mode only needs the lifecycle."""
    def __init__(self, src_path, target_spec, button_map=None, invert_y=False):
        self.alive = True
    def start(self): pass
    def stop(self): self.alive = False
    def join(self, timeout=None): pass
    def is_alive(self): return self.alive
    @property
    def virtual_path(self): return None

cm.Remapper = FakeRemapper

def new_inst(mode):
    return cm.ControllerInstance("event17", "DualSense", 0x054c, 0x0ce6,
                                 "ps5", mode, UNIQ, "", ["/dev/hidraw1"])

def reset():
    gate_calls.clear(); led_calls.clear(); holders[0] = set()

# ── Scenario A: adoption with a pre-existing holder -> rearm + latch reset ───
print("Scenario A: native adoption with Steam already holding -> rearm")
reset()
holders[0] = {12055}                       # Steam grabbed the pad at connect
inst = new_inst("ps5-native")
inst.apply_mode(adopt=True)
check(gate_calls == ["restore", "rearm"],
      "restore (no-op for a fresh identity), then a forced rearm")
check(led_calls == [(0, 0, 255)],
      "resting blue written (raw colour write clears the latch)")

# ── Scenario B: the delayed repaint re-asserts the colour ────────────────────
print("Scenario B: one-shot repaint fires ~6 s later (post-rebind BT drop)")
holders[0] = set()                         # rebind killed Steam's fd
clock[0] += 2.0
inst.watch_holders()
check(len(led_calls) == 1,   "repaint not yet due at +2 s")
clock[0] += 4.5
inst.watch_holders()
check(led_calls == [(0, 0, 255), (0, 0, 255)],
      "delayed re-assert repainted the resting blue once")
inst.watch_holders()
check(len(led_calls) == 2,   "one-shot: no further repaint spam")

# ── Scenario C: adoption without holders -> no rearm ─────────────────────────
print("Scenario C: clean adoption never rebinds")
reset()
inst = new_inst("ps5-native")
inst.apply_mode(adopt=True)
check(gate_calls == ["restore"], "no holders -> restore only, no rearm")

# ── Scenario D: plain mode switch with a game holding -> no rearm ────────────
print("Scenario D: non-adoption apply_mode leaves a running game's fd alone")
reset()
inst = new_inst("ps5-native")
inst.apply_mode(adopt=True)                # clean adoption
holders[0] = {23456}                       # a game opened the pad
gate_calls.clear()
inst.apply_mode()                          # e.g. tray re-click of native
check(gate_calls == ["restore"],
      "no rearm outside adoption - the game keeps its fd")

# ── Scenario E: remap adoption -> gate rebind suffices, repaint still armed ──
print("Scenario E: ps5-xbox adoption relies on block; repaint fires in remap mode")
reset()
holders[0] = {12055}
inst = new_inst("ps5-xbox")
inst.apply_mode(adopt=True)
check(gate_calls == ["block"],
      "block already rebinds - no extra rearm in a remap mode")
check(led_calls == [(0, 128, 0)], "green written after the gate")
holders[0] = set()
clock[0] += 6.5
inst.watch_holders()
check(led_calls == [(0, 128, 0), (0, 128, 0)],
      "delayed re-assert fires in remap mode too (was: never repainted)")

# ── Scenario F: player LEDs ride every colour apply + the armed one-shot ────
print("Scenario F: player number asserted with the colour and on the cross-pad one-shot")
reset(); player_calls.clear()
inst = new_inst("ps5-native")
inst.player = 2
inst.apply_mode(adopt=True)
check(player_calls == [("input132", 2)],
      "player pattern written alongside the colour apply")
player_calls.clear()
inst._repaint_at = None                    # isolate the player-only one-shot
inst._player_repaint_at = clock[0] + 6.0   # armed by another pad's churn
clock[0] += 2.0
inst.watch_holders()
check(not player_calls,  "player re-assert not yet due")
clock[0] += 4.5
inst.watch_holders()
check(player_calls == [("input132", 2)],
      "armed one-shot re-asserts the player number (lightbar untouched)")
check(not led_calls[1:], "player-only one-shot never repaints the lightbar")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
