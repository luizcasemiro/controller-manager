#!/usr/bin/env python3
"""
Live-device integration test for the Remapper: a synthetic uinput source is
grabbed by a real Remapper (the same class the daemon uses) and the emitted
virtual device is read back to verify:
  * per-event EV_KEY translation through compose_button_maps (user bindings,
    the 0x133<->0x134 target swap, passthrough for unmapped codes),
  * EV_ABS Y-axis mirroring under invert_y.
Requires a writable /dev/uinput (same prerequisite as the daemon); exits 77
otherwise, like the other skippable tests.
"""

import importlib.util, os, select, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    import evdev
    from evdev import UInput, InputDevice, AbsInfo
    from evdev import ecodes as e
except Exception:
    print("SKIP: evdev missing", file=sys.stderr)
    sys.exit(77)

if not os.access("/dev/uinput", os.W_OK):
    print("SKIP: /dev/uinput not writable", file=sys.stderr)
    sys.exit(77)

# The python-evdev UInput path auto-resolution reads /sys/devices/virtual/input.
# Inside sandboxed/truncated mount namespaces that tree is barely visible, so
# ui.device resolves to None while uinput itself still works there. Probe with
# find_by_name (defined below); skip only if uinput is unusable.

spec = importlib.util.spec_from_file_location("ctrlmgr_remap", MODULE)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

fails = []

def check(cond, msg):
    if cond:
        print(f"  OK  {msg}")
    else:
        print(f"FAIL  {msg}")
        fails.append(msg)


def read_dev(dev, timeout=2.0):
    """Read one batch of forwarded events, filtering to the forwarded types."""
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([dev.fd], [], [], 0.2)
        if r:
            out += [ev for ev in dev.read()
                    if ev.type in (e.EV_KEY, e.EV_ABS, e.EV_REL)]
            if out:
                break
    return [(ev.type, ev.code, ev.value) for ev in out]


Y = (e.ABS_Y, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0))
RY = (e.ABS_RY, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0))

def find_by_name(name, timeout=5.0):
    """Locate the /dev/input node of a freshly created uinput device by its
    name (the api's own .device resolution can be unavailable in sandboxes)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in evdev.list_devices():
            try:
                d = InputDevice(p)
                ok = d.name == name
                d.close()
            except Exception:
                ok = False
            if ok:
                return p
        time.sleep(0.05)
    return None


probe = UInput(events={e.EV_KEY: [e.BTN_SOUTH]}, name="CM Probe Device")
probe_ok = find_by_name("CM Probe Device", timeout=2.0) is not None
probe.close()
if not probe_ok:
    print("SKIP: /dev/uinput not usable here (no node appears for our probe)",
          file=sys.stderr)
    sys.exit(77)


src = UInput(events={
    e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_TR, e.BTN_WEST],
    e.EV_ABS: [Y, RY],
}, name="CM Fake Source", vendor=0x1234, product=0x0001, version=1)
src_path = find_by_name("CM Fake Source")
if src_path is None:
    print("FAIL  source uinput never became visible", file=sys.stderr)
    src.close()
    sys.exit(1)

BIND = {e.BTN_SOUTH: e.BTN_TR}                      # user: Cross -> RB
MAP = cm.compose_button_maps(None, BIND)

def run_case(name, button_map, invert_y):
    rr = cm.Remapper(src_path, cm.VIRTUAL_XBOX, button_map, invert_y=invert_y)
    rr.start()
    t0 = time.time()
    while rr._virtual_path is None and time.time() - t0 < 5.0:
        time.sleep(0.05)
    if rr._virtual_path is None:
        print(f"FAIL  {name}: remapper produced no virtual device")
        fails.append(name)
        return None
    out = None
    for _ in range(50):
        try:
            out = InputDevice(rr._virtual_path)
            break
        except Exception:
            time.sleep(0.05)
    if out is None:
        print(f"FAIL  {name}: cannot open {rr._virtual_path}")
        fails.append(name)
        return None
    return rr, out


try:
    res = run_case("case A", MAP, invert_y=True)
    if res:
        rr, out = res

        print("Scenario A: translated caps on the virtual pad")
        caps = {c for c in out.capabilities().get(e.EV_KEY, [])
                if isinstance(c, int)}
        check(caps == {e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TR},
              "adverts translated codes only (Cross gone, RB present)")

        print("Scenario B: per-event EV_KEY translation")
        src.write(e.EV_KEY, e.BTN_SOUTH, 1); src.syn()
        check(read_dev(out)[:1] == [(e.EV_KEY, e.BTN_TR, 1)],
              "user bind: physical Cross is emitted as RB")
        src.write(e.EV_KEY, e.BTN_SOUTH, 0); src.syn()
        check(read_dev(out)[:1] == [(e.EV_KEY, e.BTN_TR, 0)],
              "release follows the same translation")

        src.write(e.EV_KEY, e.BTN_EAST, 1); src.syn()
        check(read_dev(out)[:1] == [(e.EV_KEY, e.BTN_EAST, 1)],
              "unmapped code passes through unchanged")
        src.write(e.EV_KEY, e.BTN_EAST, 0); src.syn()
        read_dev(out)

        src.write(e.EV_KEY, e.BTN_NORTH, 1); src.syn()
        check(read_dev(out)[:1] == [(e.EV_KEY, e.BTN_NORTH, 1)],
              "unbound codes pass through unchanged (no swap layer)")
        src.write(e.EV_KEY, e.BTN_NORTH, 0); src.syn()
        read_dev(out)

        print("Scenario C: EV_ABS Y mirroring (invert_y)")
        src.write(e.EV_ABS, e.ABS_Y, 10); src.syn()
        check(read_dev(out)[:1] == [(e.EV_ABS, e.ABS_Y, 245)],
              "ABS_Y 10 -> 245 within the 0..255 range")
        src.write(e.EV_ABS, e.ABS_RY, 200); src.syn()
        check(read_dev(out)[:1] == [(e.EV_ABS, e.ABS_RY, 55)],
              "ABS_RY 200 -> 55")
        rr.stop()
        out.close()

    print("Scenario D: no bind layers -> plain passthrough")
    res = run_case("case D", None, invert_y=False)
    if res:
        rr, out = res
        src.write(e.EV_KEY, e.BTN_SOUTH, 1); src.syn()
        check(read_dev(out)[:1] == [(e.EV_KEY, e.BTN_SOUTH, 1)],
              "identical codes fly through untouched")
        src.write(e.EV_KEY, e.BTN_SOUTH, 0); src.syn()
        read_dev(out)
        src.write(e.EV_ABS, e.ABS_Y, 120); src.syn()
        check(read_dev(out)[:1] == [(e.EV_ABS, e.ABS_Y, 120)],
              "axis passes unchanged without invert_y")
        rr.stop()
        out.close()

finally:
    try:
        src.close()
    except Exception:
        pass

print("")
if fails:
    print(f"RESULT: {len(fails)} FAILURE(S)")
    sys.exit(1)
print("RESULT: ALL PASS")