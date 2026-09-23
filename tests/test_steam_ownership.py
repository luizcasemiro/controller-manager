"""Regression test for the lightbar ownership policy with the Steam client
as a PERMANENT holder (PlayStation controller support enabled). The old
holder-delta model could not tell the idle client from a running game - the
policy now classifies instead of counting:

  * steam_game (manager-wide 'SteamLaunch AppId=' cmdline sweep) marks Steam
    Input titles, which never open the pad themselves;
  * a FOREIGN holder (any raw-HID holder that is not the Steam client) marks
    direct-access titles (Steam Input disabled per game, Lutris, emulators).

While either is true the lightbar is the game's (hands off); otherwise the
daemon re-asserts the mode colour: delayed repaint after client (re)opens,
repaint shortly after a Steam Input game exits (no rearm - nothing closed),
rearm + repaint after any raw fd closed (latch risk), deferred while a game
is still active, plus a slow periodic backstop so the colour is visible at
a glance at all times outside a running game.

Stubs the gate, holder scan, LED helpers, client classification and
controller scan; the real ControllerInstance runs. The real is_steam_client
and steam_game_running are smoke-tested against live /proc first. Skips
(exit 77) when the daemon's runtime deps are unavailable, mirroring
test_reconcile.py."""
import importlib.util, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_own", MODULE)
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

# ── Real /proc smoke tests, before anything is stubbed ───────────────────────
print("Scenario 0: real is_steam_client / steam_game_running against /proc")
check(cm.is_steam_client(os.getpid()) is False,
      "this test process is not the Steam client")
marker = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)",
     "SteamLaunch AppId=999999"],   # inert argv, but visible in /proc cmdline
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    check(cm.steam_game_running() is True,
          "a live process with the SteamLaunch marker is detected")
finally:
    marker.terminate(); marker.wait()
check(isinstance(cm.steam_game_running(), bool),
      "sweep returns a bool (a real game may legitimately be running)")

# ── Stubbed environment (same pattern as test_lightbar_latch.py) ─────────────
clock = [1000.0]
cm.time.monotonic = lambda: clock[0]
cm.time.sleep = lambda s: None

UNIQ = "ac:36:1b:70:70:e8"
STEAM_PIDS = {501}
gate_calls = []
holders = [set()]
led_calls = []

cm.hidraw_gate = lambda action, uniq, nodes: gate_calls.append(action)
cm.hidraw_holders = lambda nodes: set(holders[0])
cm.is_steam_client = lambda pid: pid in STEAM_PIDS
cm.led_set_raw = lambda hidraw, rgb: led_calls.append(rgb)
cm.led_set_player = lambda prefix, player: None
cm.led_indicator_for_event = lambda path: "input21:rgb:indicator" if path else None
cm.hidraw_for_event = lambda path: ["/dev/hidraw0"] if path else []
cm.scan_controllers = lambda exclude_paths=frozenset(): [
    {"path": "event5", "name": "DualSense", "vendor": 0x054c,
     "product": 0x0ce6, "family": "ps5", "uniq": UNIQ, "phys": "",
     "hidraw": ["/dev/hidraw0"]}]

class FakeRemapper:
    def __init__(self, src_path, target_spec, button_map=None, invert_y=False): self.alive = True
    def start(self): pass
    def stop(self): self.alive = False
    def join(self, timeout=None): pass
    def is_alive(self): return self.alive
    @property
    def virtual_path(self): return None

cm.Remapper = FakeRemapper

inst = cm.ControllerInstance("event5", "DualSense", 0x054c, 0x0ce6,
                             "ps5", "ps5-native", UNIQ, "", ["/dev/hidraw0"])
inst.apply_mode(adopt=True)          # clean adoption: paint #1, repaint armed
clock[0] += 6.5
inst.watch_holders()                 # armed one-shot fires: paint #2
base = len(led_calls)
check(base == 2 and gate_calls == ["restore"],
      "baseline: adoption paint + one-shot re-assert, no rearm")

# ── A: the idle client (re)opens the pad -> one delayed repaint wins ─────────
print("Scenario A: Steam client opens the pad idle -> colour re-asserted")
holders[0] = {501}
clock[0] += 2.0
inst.watch_holders()
check(len(led_calls) == base, "no instant write on the client's open")
clock[0] += 6.5
inst.watch_holders()
check(len(led_calls) == base + 1 and gate_calls == ["restore"],
      "delayed repaint outlasted the client's slot-colour write, no rearm")
base += 1

# ── B: Steam Input game running -> hands off, backstop paused ────────────────
print("Scenario B: Steam Input game running -> no repaint, no backstop")
inst._repaint_at = clock[0] + 6.0    # pretend something armed a repaint
clock[0] += 30.0                     # far past any backstop period
inst.watch_holders(steam_game=True)
check(inst._repaint_at is None, "pending repaint cancelled at game start")
check(len(led_calls) == base, "no backstop write while the game owns the pad")

# ── C: Steam Input game exits (no fd ever closed) -> repaint, NO rearm ───────
print("Scenario C: Steam Input game exits -> outlast the exit restore")
clock[0] += 2.0
inst.watch_holders()                 # first quiet tick after the game
check(len(led_calls) == base, "no write racing Steam's exit restore")
clock[0] += 3.5
inst.watch_holders()
check(len(led_calls) == base + 1, "repaint shortly after the exit")
check(gate_calls == ["restore"], "no rearm - no raw fd was closed")
base += 1

# ── D: direct-access title (foreign holder) -> suppress; exit -> rearm ────────
print("Scenario D: foreign holder suppresses; its exit rearms (latch risk)")
holders[0] = {501, 777}              # Lutris/Wine title opened the pad itself
clock[0] += 2.0
inst.watch_holders()
clock[0] += 20.0                     # long session, backstop must stay quiet
inst.watch_holders()
check(len(led_calls) == base, "foreign holder -> hands off throughout")
holders[0] = {501}                   # title closed its fd
clock[0] += 2.0
inst.watch_holders()
check(gate_calls == ["restore", "rearm"],
      "fd closed -> rearm heals the possible 'light out' latch")
check(len(led_calls) == base + 1, "immediate repaint after the rearm")
base += 1
clock[0] += 6.5
inst.watch_holders()                 # armed one-shot after the rearm
base = len(led_calls)

# ── E: fd closes while a Steam game still runs -> rearm deferred ─────────────
print("Scenario E: release during an active game defers the rearm")
holders[0] = {501, 888}
clock[0] += 2.0
inst.watch_holders(steam_game=True)
holders[0] = {501}                   # game's own fd gone, game still alive
clock[0] += 2.0
inst.watch_holders(steam_game=True)
check(gate_calls == ["restore", "rearm"],
      "no rearm while the game is alive (it would yank running fds)")
clock[0] += 2.0
inst.watch_holders()                 # game ended
check(gate_calls == ["restore", "rearm", "rearm"],
      "deferred rearm fires once the game is gone")
clock[0] += 6.5
inst.watch_holders()
base = len(led_calls)

# ── F: slow periodic backstop in the quiet steady state ─────────────────────
print("Scenario F: backstop re-asserts once per period, then stays quiet")
clock[0] += 16.0
inst.watch_holders()
check(len(led_calls) == base + 1, "one backstop write after a full period")
inst.watch_holders()
check(len(led_calls) == base + 1, "no spam on the next tick")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
