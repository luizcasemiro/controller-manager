"""Unit tests for the visual controller setup flow.

The suite exercises the pure BlueZ classifier and the session state machine
with fakes; no Bluetooth adapter, GTK display, D-Bus system bus or subprocess
is touched. It skips (exit 77) when dbus-python is unavailable, mirroring the
daemon tests."""

import dataclasses
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

try:
    from controller_setup import bluez
    from controller_setup.bluez import BackendError, DeviceSnapshot
    from controller_setup.gamepad import GamepadInput
    from controller_setup.combo import ComboTracker
    from controller_setup.health import ControllerManagerHealth
    from controller_setup.session import (
        PHASE_CLOSED, PHASE_ERROR, PHASE_READY, PHASE_TIMEOUT,
        STATUS_ERROR, STATUS_READY, PairingSession,
    )
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs dbus-python")
    sys.exit(77)

fails = []


def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)


def props(name, icon="", device_class=0, paired=False, connected=False,
          trusted=False):
    return {
        "Alias": name,
        "Icon": icon,
        "Class": device_class,
        "Paired": paired,
        "Connected": connected,
        "Trusted": trusted,
    }


def device(name, address, *, icon="", device_class=0, paired=False,
           connected=False, trusted=False):
    return DeviceSnapshot(
        path=f"/org/bluez/hci0/dev_{address.replace(':', '_')}",
        address=address,
        name=name,
        icon=icon,
        device_class=device_class,
        paired=paired,
        connected=connected,
        trusted=trusted,
        properties=props(name, icon, device_class, paired, connected, trusted),
    )


class FakeBackend:
    def __init__(self, devices, fail_pair=()):
        self.devices_list = list(devices)
        self.fail_pair = set(fail_pair)
        self.begun = 0
        self.ended = 0
        self.pairs = []
        self.trusts = []
        self.connects = []
        self.in_scan = False

    def begin_scan(self):
        self.begun += 1
        self.in_scan = True

    def end_scan(self):
        self.ended += 1
        self.in_scan = False

    def devices(self):
        return list(self.devices_list)

    def find_device(self, identity):
        for item in self.devices_list:
            if item.identity == identity or item.path == identity:
                return item
        return None

    def _replace(self, item, **changes):
        new = dataclasses.replace(item, **changes)
        self.devices_list = [new if d is item else d
                             for d in self.devices_list]
        return new

    def pair(self, item):
        self.pairs.append(item.identity)
        if item.identity in self.fail_pair:
            raise BackendError("pareamento recusado")
        self._replace(item, paired=True)

    def set_trusted(self, item):
        self.trusts.append(item.identity)
        self._replace(item, trusted=True)

    def connect(self, item):
        self.connects.append(item.identity)
        current = self.find_device(item.identity) or item
        self._replace(current, connected=True)


class FakeGameMode:
    def __init__(self, ok=True, error="falha no Game Mode"):
        self.ok = ok
        self.error = error
        self.calls = 0

    def validate(self):
        return None

    def activate(self):
        self.calls += 1
        return self.ok, ("" if self.ok else self.error)


class FakeHeroic:
    def __init__(self, running=False, launch_ok=True, close_ok=True):
        self.running = running
        self.launch_ok = launch_ok
        self.close_ok = close_ok
        self.launches = 0
        self.closes = []

    def validate(self):
        return None

    def is_running(self):
        return self.running

    def close(self):
        self.closes.append(True)
        if self.close_ok:
            self.running = False
            return True, ""
        return False, "nao foi possivel encerrar"

    def launch(self):
        self.launches += 1
        if self.launch_ok:
            return True, ""
        return False, "flatpak falhou"


class FakeHealth:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def check(self, identity=None, name=None, restart=True, timeout=8.0):
        self.calls.append((identity, name, restart, timeout))
        rows = [("AA:BB", "DualSense", "ps5", "ps5-native")] if self.ok else []
        return self.ok, ("" if self.ok else "daemon indisponivel"), rows


def make_session(backend, game_mode=None, heroic=None, **kwargs):
    return PairingSession(
        backend,
        game_mode or FakeGameMode(),
        heroic or FakeHeroic(),
        scan_seconds=kwargs.pop("scan_seconds", 1.5),
        pair_after=0.0,
        sleep=kwargs.pop("sleep", lambda _s: None),
        **kwargs,
    )


# -- classifier --------------------------------------------------------------
print("Classifier: only controllers pass the filter")
check(bluez.is_gamepad(props("DualSense", "input-gaming")),
      "BlueZ input-gaming icon is accepted")
check(bluez.is_gamepad(props("Xbox Wireless Controller",
                             device_class=0x000508)),
      "Bluetooth class joystick/gamepad is accepted")
check(not bluez.is_gamepad(props("USB Keyboard", device_class=0x000540)),
      "keyboard class is rejected")
check(not bluez.is_gamepad(props("MX Master", "input-mouse")),
      "mouse icon is rejected")
check(not bluez.is_gamepad(props("Luiz's iPhone",
                                 device_class=0x000200)),
      "phone class is rejected")
check(not bluez.is_gamepad(props("Sony WH-1000XM5",
                                 device_class=0x000418)),
      "audio class is rejected")
check(not bluez.is_gamepad(props("Keyboard Controller",
                                 icon="input-keyboard")),
      "name cannot override a keyboard icon")
check(bluez.is_gamepad(props("8BitDo Pro 2")),
      "known controller name is accepted when class/icon are absent")
check(bluez.is_gamepad(props("Wireless Controller")),
      "PS4 wireless controller name is accepted")
check(bluez.is_gamepad(props("Xbox Wireless Controller",
                             device_class=0x000500)),
      "uncategorized peripheral class falls back to the controller name")
check(not bluez.is_gamepad(props("Random Bluetooth Thing")),
      "unknown device is not paired automatically")

# -- BlueZ object paths ------------------------------------------------------
print("BlueZ: AgentManager1 is looked up at /org/bluez, ObjectManager at /")
class FakeBus:
    def __init__(self):
        self.paths = []
    def get_object(self, service, path):
        self.paths.append((service, path))
        return path
saved_interface = bluez.dbus.Interface
bluez.dbus.Interface = lambda obj, iface: (obj, iface)
try:
    fake_bus = FakeBus()
    backend = bluez.BlueZBackend(bus=fake_bus)
    backend._ensure_manager()
    check((bluez.BLUEZ, "/") in fake_bus.paths,
          "ObjectManager is read from the root object")
    check((bluez.BLUEZ, "/org/bluez") in fake_bus.paths,
          "AgentManager1 is read from /org/bluez")
    check(backend._agent_manager == ("/org/bluez", bluez.AGENT_MANAGER_IFACE),
          "agent manager interface is bound to the correct path")
finally:
    bluez.dbus.Interface = saved_interface

# -- overlay chord -----------------------------------------------------------
print("Combo: LT + RT hold, then Home")
combo = ComboTracker(threshold=0.70, hold=0.5)
combo.set_triggers(0.69, 1.0)
combo.set_home(True)
check(not combo.update(0.0) and not combo.update(1.0),
      "left trigger below 70% does not arm the chord")
combo.set_triggers(0.90, 0.90)
check(not combo.update(2.0), "triggers arm the chord")
check(not combo.update(2.4), "still waiting at 0.4s")
check(combo.update(2.5), "Home at/after 0.5s fires")
check(not combo.update(3.0), "fires only once while held")
combo.set_home(False)
check(not combo.update(3.1), "releasing Home does not re-arm while still held")
combo.set_home(True)
check(not combo.update(3.2), "a second Home press does not repeat")
combo.set_triggers(0.0, 0.0)
check(not combo.update(3.3), "releasing the triggers resets the chord")
combo.set_triggers(0.90, 0.90)
combo.set_home(False)
check(not combo.update(3.4), "new trigger hold is armed")
combo.set_home(True)
check(combo.update(3.9), "fires after a complete new hold")

# -- service health ----------------------------------------------------------
print("Health: active service + daemon listing the controller")
class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

class HealthRunner:
    def __init__(self, active=True):
        self.active = active
        self.restarts = 0
    def __call__(self, command, **kwargs):
        if "is-active" in command:
            return Result(0 if self.active else 3)
        if "restart" in command:
            self.restarts += 1
            self.active = True
            return Result(0)
        return Result(0)

runner = HealthRunner(active=True)
controllers = [("AA:BB", "DualSense", "ps5", "ps5-native")]
health = ControllerManagerHealth(
    runner=runner, controller_provider=lambda: controllers,
    sleep=lambda _s: None)
ok, detail, rows = health.check(identity="aa:bb", timeout=1.0)
check(ok and not detail, "active service and listed controller pass")
check(runner.restarts == 0, "no restart is needed when healthy")
check(len(rows) == 1, "controller rows are returned")
check(ControllerManagerHealth._matches(
    [("phys:usb-1", "DualSense", "ps5", "ps5-native")],
    "AC:36:1B:70:70:E8", "DualSense Wireless Controller"),
    "name substring bridges BlueZ alias differences")
check(ControllerManagerHealth._matches(
    [("AC_36_1B_70_70_E8", "Wireless Controller", "ps5", "ps5-native")],
    "ac:36:1b:70:70:e8", ""),
    "normalised MAC separators are accepted")

runner = HealthRunner(active=False)
health = ControllerManagerHealth(
    runner=runner, controller_provider=lambda: controllers,
    sleep=lambda _s: None)
ok, detail, rows = health.check(identity="AA:BB", timeout=2.0)
check(ok, "inactive service is restarted and then passes")
check(runner.restarts == 1, "the service is restarted exactly once")

runner = HealthRunner(active=True)
health = ControllerManagerHealth(
    runner=runner,
    controller_provider=lambda: (_ for _ in ()).throw(
        RuntimeError("dbus down")),
    sleep=lambda _s: None)
ok, detail, rows = health.check(timeout=0.2)
check(not ok and "D-Bus" in detail, "unavailable D-Bus is reported")
check(runner.restarts == 1, "D-Bus failure triggers one restart")

runner = HealthRunner(active=True)
health = ControllerManagerHealth(
    runner=runner,
    controller_provider=lambda: [("CC:DD", "Other", "xbox", "xbox-native")],
    sleep=lambda _s: None)
ok, detail, rows = health.check(identity="AA:BB", timeout=0.05)
check(not ok, "a controller not listed by the daemon fails the check")
check(runner.restarts == 0, "waiting for adoption does not restart a live daemon")

# -- gamepad action mapping --------------------------------------------------
print("Gamepad: D-pad/stick and face buttons map to UI actions")
import types

class FakeCodes:
    EV_KEY = 1
    EV_ABS = 3
    BTN_DPAD_UP = 544
    BTN_DPAD_LEFT = 546
    BTN_DPAD_DOWN = 545
    BTN_DPAD_RIGHT = 547
    BTN_SOUTH = 304
    BTN_A = 304
    BTN_EAST = 305
    BTN_B = 305
    BTN_START = 315
    BTN_SELECT = 314
    BTN_MODE = 316
    ABS_HAT0Y = 17
    ABS_HAT0X = 16
    ABS_Y = 1

class FakeDevice:
    def __init__(self, low=-32768, high=32767):
        self.low, self.high = low, high
    def absinfo(self, code):
        return types.SimpleNamespace(min=self.low, max=self.high)

class FakeEvent:
    def __init__(self, type_, code, value):
        self.type, self.code, self.value = type_, code, value

import controller_setup.gamepad as gamepad_mod
saved_codes = gamepad_mod.e
gamepad_mod.e = FakeCodes
try:
    actions = []
    pad = GamepadInput(actions.append)
    def emit(code, value):
        pad._last_action = 0.0
        pad._handle_event("/dev/input/event-test", FakeDevice(),
                          FakeEvent(FakeCodes.EV_KEY, code, value))
    emit(FakeCodes.BTN_DPAD_DOWN, 1)
    emit(FakeCodes.BTN_SOUTH, 1)
    emit(FakeCodes.BTN_EAST, 1)
    emit(FakeCodes.BTN_START, 1)
    emit(FakeCodes.BTN_SELECT, 1)
    check(actions == ["next", "activate", "back", "open", "more"],
          "buttons map to semantic actions")
    actions.clear()
    pad._axis_state.clear()
    pad._last_action = 0.0
    pad._handle_event("/dev/input/event-test", FakeDevice(),
                      FakeEvent(FakeCodes.EV_ABS, FakeCodes.ABS_Y, -22000))
    check(actions == ["prev"], "left stick up maps to previous")
    pad._handle_event("/dev/input/event-test", FakeDevice(),
                      FakeEvent(FakeCodes.EV_ABS, FakeCodes.ABS_Y, -22000))
    check(actions == ["prev"], "held stick does not repeat every event")
    pad._last_action = 0.0
    pad._handle_event("/dev/input/event-test", FakeDevice(),
                      FakeEvent(FakeCodes.EV_ABS, FakeCodes.ABS_Y, 22000))
    check(actions == ["prev", "next"], "left stick down maps to next")
finally:
    gamepad_mod.e = saved_codes

# -- full flow: unpaired controller -----------------------------------------
print("Session A: unpaired controller is paired, trusted, connected, then scan stops")
backend = FakeBackend([device("DualSense", "AA:BB", icon="input-gaming")])
gm = FakeGameMode()
heroic = FakeHeroic()
s = make_session(backend, gm, heroic)
check(s.start(), "session starts")
s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_READY, "phase becomes ready")
check(gm.calls == 1, "Game Mode activated exactly once")
check(backend.pairs == ["AA:BB"], "pair called")
check(backend.trusts == ["AA:BB"], "trust called")
check(backend.connects == ["AA:BB"], "connect called")
check(backend.begun == 1 and backend.ended == 1,
      "discovery started and stopped exactly once")
check(snap.heroic_ready, "Heroic button is enabled")
check(snap.devices[0].status == STATUS_READY, "device card is ready")
check(snap.can_scan_more, "more-controllers action is available")

# -- manager health ----------------------------------------------------------
print("Session A2: controller adoption is verified through the daemon D-Bus")
backend = FakeBackend([device("DualSense", "AA:BB", icon="input-gaming")])
health = FakeHealth(ok=True)
s = make_session(backend, manager_health=health)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.manager_ok, "healthy daemon is reported")
check(snap.manager_count == 1, "managed controller count is exposed")
check(health.calls and health.calls[0][0] == "AA:BB",
      "health check asks for the paired controller identity")
check(health.calls[0][2] is True, "pairing health check allows one restart")

print("Session A3: daemon unavailability blocks Heroic and offers retry")
backend = FakeBackend([device("DualSense", "AA:BB", icon="input-gaming")])
health = FakeHealth(ok=False)
s = make_session(backend, manager_health=health)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(not snap.manager_ok, "unhealthy daemon is reported")
check(snap.heroic_ready, "the controller itself is still ready")
check(snap.can_retry, "manager retry is offered")
ok, error = s.open_heroic()
check(not ok and "daemon" in error, "Heroic is refused while the manager is down")

print("Session A4: pair-only mode skips Game Mode and Heroic")
backend = FakeBackend([device("DualSense", "AA:BB", icon="input-gaming")])
gm = FakeGameMode()
heroic = FakeHeroic()
s = make_session(backend, gm, heroic, pair_only=True,
                 manager_health=FakeHealth(ok=True))
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_READY, "pair-only reaches ready")
check(not snap.game_mode_active, "Game Mode is not activated")
check(snap.pair_only, "snapshot identifies pair-only mode")
check(gm.calls == 0, "Game Mode script is never called")
check(heroic.closes == [] and heroic.launches == 0,
      "Heroic is neither closed nor launched")
ok, error = s.open_heroic()
check(not ok, "pair-only has no Heroic action")

# -- paired but disconnected: trust + connect only --------------------------
print("Session B: paired controller skips Pair")
backend = FakeBackend([device("Xbox Wireless Controller", "CC:DD",
                              device_class=0x000508, paired=True)])
s = make_session(backend)
s.start(); s.wait(2.5)
check(s.snapshot().phase == PHASE_READY, "paired controller becomes ready")
check(backend.pairs == [], "Pair is not called for an already paired pad")
check(backend.trusts == ["CC:DD"] and backend.connects == ["CC:DD"],
      "trust and connect are still asserted")

# -- already connected at session start -------------------------------------
print("Session C: an already connected pad is ready but does not stop the scan")
backend = FakeBackend([device("DualSense", "EE:FF", icon="input-gaming",
                              paired=True, connected=True)])
s = make_session(backend, scan_seconds=0.15)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_TIMEOUT, "scan runs to its timeout")
check(snap.heroic_ready, "existing controller keeps Heroic enabled")
check(backend.pairs == [] and backend.connects == [],
      "no needless pair/connect calls")
check(snap.can_scan_more, "more action is available after timeout")

# -- only non-controllers ---------------------------------------------------
print("Session D: keyboard/mouse discovery is ignored")
backend = FakeBackend([
    device("Keyboard", "11:11", device_class=0x000540),
    device("Mouse", "22:22", icon="input-mouse"),
])
s = make_session(backend, scan_seconds=0.15)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_TIMEOUT, "no controller -> timeout")
check(not snap.devices, "non-controller devices are not shown as candidates")
check(backend.pairs == [] and backend.trusts == [],
      "non-controllers are never paired or trusted")

# -- Heroic already open ----------------------------------------------------
print("Session E: Heroic already running is force-closed before Game Mode")
backend = FakeBackend([])
gm = FakeGameMode()
heroic = FakeHeroic(running=True)
s = make_session(backend, gm, heroic, scan_seconds=0.15)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(heroic.closes == [True], "close is requested")
check(heroic.running is False, "Heroic is no longer running")
check(gm.calls == 1, "Game Mode is activated after closing Heroic")
check(snap.game_mode_active, "badge becomes active")
check(snap.phase == PHASE_TIMEOUT, "the normal scan follows the close")

print("Session E2: a Heroic that will not close aborts the flow")
backend = FakeBackend([])
gm = FakeGameMode()
s = make_session(backend, gm, FakeHeroic(running=True, close_ok=False))
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_ERROR, "close failure is an error")
check("fechar o heroic" in (snap.message + " " + snap.detail).lower(),
      "error mentions Heroic")
check(gm.calls == 0, "Game Mode is not activated after a close failure")

# -- Game Mode failure ------------------------------------------------------
print("Session F: Game Mode failure is surfaced without scanning")
backend = FakeBackend([])
s = make_session(backend, FakeGameMode(ok=False))
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_ERROR, "phase is error")
check("Game Mode" in snap.detail or "Game Mode" in snap.message,
      "error mentions Game Mode")
check(backend.begun == 0, "discovery never starts after the failure")

# -- pair failure stays visible and does not abort the session --------------
print("Session G: one failed pair keeps the session alive and shows the card")
backend = FakeBackend([device("DualSense", "AB:CD", icon="input-gaming")],
                      fail_pair={"AB:CD"})
s = make_session(backend, scan_seconds=0.25)
s.start(); s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_TIMEOUT, "session ends with timeout, not a crash")
check(len(snap.devices) == 1 and snap.devices[0].status == STATUS_ERROR,
      "failed device stays visible with an error card")
check(backend.begun == backend.ended == 1, "discovery is cleaned up")

# -- connect a second controller -------------------------------------------
print("Session H: 'connect more' finds a second controller and stops again")
first = device("DualSense 1", "10:10", icon="input-gaming")
backend = FakeBackend([first])
s = make_session(backend)
s.start(); s.wait(2.5)
check(s.snapshot().phase == PHASE_READY, "first controller ready")
second = device("DualSense 2", "20:20", icon="input-gaming")
backend.devices_list.append(second)
check(s.scan_more(), "additional scan starts")
s.wait(2.5)
snap = s.snapshot()
check(snap.phase == PHASE_READY, "second controller ready")
check({d.identity for d in snap.devices if d.status == STATUS_READY}
      == {"10:10", "20:20"}, "both controllers are ready")
check(backend.pairs == ["10:10", "20:20"], "each new pad is paired once")
check(backend.begun == 2 and backend.ended == 2,
      "each discovery window is opened and closed")

# -- Heroic launch ----------------------------------------------------------
print("Session I: opening Heroic cancels discovery and is available only when ready")
backend = FakeBackend([])
heroic = FakeHeroic()
s = make_session(backend, heroic=heroic)
s.start(); s.wait(2.5)
ok, error = s.open_heroic()
check(not ok and "Conecte" in error, "launch is refused without a controller")
check(heroic.launches == 0, "Heroic not launched without ready pad")

backend = FakeBackend([device("DualSense", "30:30", icon="input-gaming")])
heroic = FakeHeroic()
s = make_session(backend, heroic=heroic)
s.start(); s.wait(2.5)
ok, error = s.open_heroic()
check(ok and not error, "Heroic launch succeeds after pairing")
check(heroic.launches == 1, "Heroic was launched")
check(s.snapshot().phase == PHASE_CLOSED, "session closes after launch")

# -- shutdown ---------------------------------------------------------------
print("Session J: closing the UI stops discovery")
backend = FakeBackend([])
s = make_session(backend, scan_seconds=10.0)
s.start()
time.sleep(0.07)
s.shutdown()
check(s.snapshot().phase == PHASE_CLOSED, "closed phase")
check(backend.ended >= 1, "end_scan ran during shutdown")
check(not backend.in_scan, "discovery is no longer active")

# -- process adapters -------------------------------------------------------
print("Support: Game Mode and Heroic commands are injectable")
import tempfile
from controller_setup.support import (GameModeRunner, HeroicLauncher,
                                      is_heroic_cmdline)

print("Support: Heroic process detection ignores game runtimes")
check(is_heroic_cmdline(
    "/usr/bin/flatpak run com.heroicgameslauncher.hgl --console"),
    "flatpak run entry point is detected")
check(is_heroic_cmdline("/app/bin/heroic --type=renderer"),
    "flatpak Heroic children are detected")
check(is_heroic_cmdline("/usr/bin/heroic"), "native Heroic is detected")
check(not is_heroic_cmdline(
    "python3 /home/luiz/.var/app/com.heroicgameslauncher.hgl/config/"
    "heroic/tools/runtimes/umu/umu_run.py /games/setup.exe"),
    "game runtimes under the Heroic data path are ignored")
check(not is_heroic_cmdline("grep com.heroicgameslauncher.hgl"),
    "unrelated commands mentioning the app id are ignored")

class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

gm_script = tempfile.NamedTemporaryFile("w", delete=False)
gm_script.write("#!/bin/sh\n")
gm_script.close()
os.chmod(gm_script.name, 0o755)
calls = []
def gm_run(cmd, **kwargs):
    calls.append(cmd)
    return FakeRun()
runner = GameModeRunner(gm_script.name, runner=gm_run)
check(runner.validate() is None, "executable Game Mode script validates")
ok, err = runner.activate()
check(ok and not err, "Game Mode script exit 0 is success")
check(calls == [[gm_script.name, "1"]], "Game Mode is called with argument 1")
calls.clear()
ok, err = runner.disable()
check(ok and calls == [[gm_script.name, "0"]],
      "Game Mode off is called with argument 0")
os.unlink(gm_script.name)

class FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs

started = []
def fake_popen(command, **kwargs):
    proc = FakePopen(command, **kwargs)
    started.append(proc)
    return proc

def heroic_run(command, **kwargs):
    return FakeRun(returncode=0)

launcher = HeroicLauncher(flatpak="/usr/bin/flatpak", popen=fake_popen,
                          runner=heroic_run)
launcher.is_running = lambda: False
check(launcher.validate() is None, "installed Flatpak Heroic validates")
ok, err = launcher.launch()
check(ok and not err, "Heroic launch succeeds")
check(started and started[0].command == [
    "/usr/bin/flatpak", "run", "com.heroicgameslauncher.hgl",
    "--console", "--fullscreen"], "Heroic runs in console fullscreen")
check(started[0].kwargs.get("start_new_session") is True,
      "Heroic is detached from the KDE Connect process")
launcher.is_running = lambda: True
closed = []
launcher.close = lambda: (closed.append(1) or True, "")
ok, err = launcher.launch()
check(ok and not err, "an open Heroic is closed before launch")
check(closed == [1], "close is called exactly once")
launcher.is_running = lambda: False
launcher._runner = lambda command, **kwargs: FakeRun(returncode=1)
check(launcher.validate() is not None, "missing Flatpak app is reported")

# -- forced close -----------------------------------------------------------
print("Support: Heroic close escalates from SIGTERM to SIGKILL")
import signal

state = [["111"]]
kills = []
def provider(_app):
    return list(state[0])
def killer(pid, sig):
    kills.append((pid, sig))
    if sig == signal.SIGTERM:
        state[0] = []
close_launcher = HeroicLauncher(
    flatpak="/usr/bin/flatpak", process_provider=provider, killer=killer,
    sleep=lambda _s: None)
ok, err = close_launcher.close(grace=0.5)
check(ok and not err, "SIGTERM closes a cooperative Heroic")
check(kills == [(111, signal.SIGTERM)], "SIGTERM is sent once")

state = [["222"]]
kills = []
def killer_stubborn(pid, sig):
    kills.append((pid, sig))
close_launcher = HeroicLauncher(
    flatpak="/usr/bin/flatpak", process_provider=provider,
    killer=killer_stubborn, clock=lambda: 0.0, sleep=lambda _s: None)
ok, err = close_launcher.close(grace=0.0)
check(not ok, "a stubborn process reports failure")
check([s for _p, s in kills] == [signal.SIGTERM, signal.SIGKILL],
      "SIGKILL is used after the grace period")
check("PIDs" in err, "the remaining PID is reported")

# -- KDE Connect config helper ----------------------------------------------
print("KDE Connect helper: edits only commands= and is idempotent")
import importlib.util
HELPER = os.path.join(REPO, "scripts", "kdeconnect-install-command.py")
spec = importlib.util.spec_from_file_location("kde_helper", HELPER)
kde = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kde)

sample = ('[General]\n'
          'commands="@ByteArray({\\"_a_\\":{\\"command\\":\\"/bin/true\\",'
          '\\"name\\":\\"Existing\\"}})"\n')
with tempfile.TemporaryDirectory() as tmp:
    device = os.path.join(tmp, "device")
    os.makedirs(os.path.join(device, "kdeconnect_runcommand"))
    path = os.path.join(device, "kdeconnect_runcommand", "config")
    with open(path, "w", encoding="utf-8") as f:
        f.write(sample)
    changed, detail = kde.update_config_file(
        path, "/home/u/.local/bin/controller-setup.py", "Pair")
    check(changed, "first run adds the command")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    check("[General]" in text and "/bin/true" in text,
          "other sections and existing commands are preserved")
    parsed = kde.parse_commands(
        text.split("commands=", 1)[1].strip())
    check(any(e.get("name") == "Pair" for e in parsed.values()),
          "new command is present")
    check(kde.parse_commands(
        '@ByteArray({"b":{"command":"/bin/false"}})'),
        "unquoted @ByteArray values are accepted too")
    backups = [p for p in os.listdir(os.path.dirname(path))
               if p.startswith("config.bak-")]
    check(len(backups) == 1, "a timestamped backup is created")
    changed2, detail2 = kde.update_config_file(
        path, "/home/u/.local/bin/controller-setup.py", "Pair")
    check(not changed2, "second run is idempotent")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)

