"""LT + RT + Home chord detection for the overlay.

The daemon owns this monitor so the chord works in native mode and also when
the remapper grabs the physical pad: in that case the virtual output carries
the same trigger and Guide capabilities. Reads are non-grabbing.
"""

from __future__ import annotations

import logging
import os
import selectors
import threading
import time
from typing import Callable, Dict, Optional

try:
    import evdev
    from evdev import ecodes as e
except Exception:  # pragma: no cover - machines without python-evdev
    evdev = None
    e = None


class ComboTracker:
    """Pure state machine for one controller.

    The chord becomes true only after both triggers are above the threshold
    and Home is held continuously for ``hold`` seconds. It fires once, then
    requires a full release before it can fire again.
    """

    def __init__(self, threshold: float = 0.70, hold: float = 0.5):
        self.threshold = float(threshold)
        self.hold = float(hold)
        self.lt = 0.0
        self.rt = 0.0
        self.home = False
        self.since: Optional[float] = None
        self.fired = False

    def set_triggers(self, lt: float, rt: float) -> None:
        self.lt = max(0.0, min(1.0, float(lt)))
        self.rt = max(0.0, min(1.0, float(rt)))

    def set_home(self, pressed: bool) -> None:
        self.home = bool(pressed)

    def reset(self) -> None:
        self.since = None
        self.fired = False

    @property
    def active(self) -> bool:
        return self.lt >= self.threshold and self.rt >= self.threshold

    def update(self, now: float) -> bool:
        # The triggers are the timing element: they must stay above 70% for
        # ``hold`` seconds. Home is the deliberate confirmation, so a quick
        # press after the triggers are armed fires immediately; it does not
        # need to be held for the whole window.
        if not self.active:
            self.reset()
            return False
        if self.since is None:
            self.since = now
        if self.home and not self.fired and now - self.since >= self.hold:
            self.fired = True
            return True
        return False


class ComboMonitor:
    def __init__(self, on_trigger: Callable[[], None],
                 logger: Optional[logging.Logger] = None,
                 threshold: float = 0.70, hold: float = 0.5):
        self._on_trigger = on_trigger
        self._log = logger or logging.getLogger("controller-manager")
        self._threshold = threshold
        self._hold = hold
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._selector = None
        self._devices: Dict[str, dict] = {}
        self._last_scan = 0.0

    @property
    def available(self) -> bool:
        return evdev is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.available or self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="controller-overlay-chord", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 0.6) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    # -- listener ------------------------------------------------------------

    def _run(self) -> None:
        self._selector = selectors.DefaultSelector()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - self._last_scan >= 1.0:
                    self._scan()
                    self._last_scan = now
                if not self._devices:
                    self._stop.wait(0.2)
                    continue
                try:
                    events = self._selector.select(timeout=0.1)
                except Exception:
                    self._close_all()
                    continue
                for key, _mask in events:
                    path, device = key.data
                    self._read_device(path, device)
                now = time.monotonic()
                for state in list(self._devices.values()):
                    if state["tracker"].update(now):
                        self._fire()
        except Exception as ex:
            self._log.warning("chord monitor stopped: %s", ex)
        finally:
            self._close_all()
            if self._selector is not None:
                try:
                    self._selector.close()
                except Exception:
                    pass
                self._selector = None

    def _scan(self) -> None:
        try:
            paths = list(evdev.list_devices())
        except Exception:
            return
        for path in paths:
            if path in self._devices:
                continue
            opened = self._open(path)
            if opened is None:
                continue
            device, infos = opened
            try:
                self._selector.register(
                    device.fileno(), selectors.EVENT_READ, (path, device))
            except Exception:
                device.close()
                continue
            self._devices[path] = {
                "device": device,
                "tracker": ComboTracker(self._threshold, self._hold),
                "absinfo": infos,
                "lt_analog": 0.0,
                "rt_analog": 0.0,
                "lt_digital": 0.0,
                "rt_digital": 0.0,
            }

    def _open(self, path: str):
        try:
            device = evdev.InputDevice(path)
            caps = device.capabilities()
            keys = caps.get(e.EV_KEY, [])
            gamepad = any(code in keys for code in (
                e.BTN_GAMEPAD, e.BTN_SOUTH, e.BTN_A))
            if not gamepad:
                device.close()
                return None
            os.set_blocking(device.fileno(), False)
            infos = {}
            trigger_codes = (e.ABS_Z, e.ABS_RZ)
            for optional in ("ABS_GAS", "ABS_BRAKE"):
                code = getattr(e, optional, None)
                if code is not None:
                    trigger_codes += (code,)
            for code, info in caps.get(e.EV_ABS, []):
                if code in trigger_codes:
                    infos[code] = info
            return device, infos
        except Exception:
            return None

    def _read_device(self, path: str, device) -> None:
        try:
            for event in device.read():
                self._handle_event(path, event)
        except BlockingIOError:
            return
        except OSError:
            self._drop(path)

    def _drop(self, path: str) -> None:
        state = self._devices.pop(path, None)
        if state is None:
            return
        device = state["device"]
        try:
            self._selector.unregister(device.fileno())
        except Exception:
            pass
        try:
            device.close()
        except Exception:
            pass

    def _close_all(self) -> None:
        for path in list(self._devices):
            self._drop(path)

    # -- event translation ---------------------------------------------------

    def _fire(self) -> None:
        try:
            self._on_trigger()
        except Exception:
            self._log.exception("overlay trigger failed")

    @staticmethod
    def _normalise(value: int, info) -> float:
        span = max(1, int(info.max) - int(info.min))
        return max(0.0, min(1.0, (int(value) - int(info.min)) / span))

    def _handle_event(self, path: str, event) -> None:
        state = self._devices.get(path)
        if state is None:
            return
        if event.type == e.EV_KEY:
            if event.code in (getattr(e, "BTN_TL2", -1),
                              getattr(e, "BTN_TR2", -1)):
                if event.code == e.BTN_TL2:
                    state["lt_digital"] = 1.0 if event.value else 0.0
                else:
                    state["rt_digital"] = 1.0 if event.value else 0.0
            elif event.code in (getattr(e, "BTN_MODE", -1),
                                getattr(e, "KEY_MENU", -1)):
                state["tracker"].set_home(event.value == 1)
        elif event.type == e.EV_ABS:
            info = state["absinfo"].get(event.code)
            if info is None:
                return
            value = self._normalise(event.value, info)
            gas = getattr(e, "ABS_GAS", None)
            brake = getattr(e, "ABS_BRAKE", None)
            if event.code in (e.ABS_Z, gas):
                state["lt_analog"] = value
            elif event.code in (e.ABS_RZ, brake):
                state["rt_analog"] = value
        else:
            return
        tracker = state["tracker"]
        tracker.set_triggers(
            max(state["lt_analog"], state["lt_digital"]),
            max(state["rt_analog"], state["rt_digital"]))
        if tracker.update(time.monotonic()):
            self._fire()
