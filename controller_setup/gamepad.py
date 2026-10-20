"""Gamepad navigation for the pairing UI.

Once a controller is connected, the dashboard must be usable from the couch.
The listener opens every evdev gamepad without grabbing it, so the normal
daemon/Steam path is unaffected. It only emits semantic actions; the GTK layer
applies them on its own main loop.
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
except Exception:  # pragma: no cover - exercised only on a machine without evdev
    evdev = None
    e = None


class GamepadInput:
    ACTION_PREV = "prev"
    ACTION_NEXT = "next"
    ACTION_ACTIVATE = "activate"
    ACTION_BACK = "back"
    ACTION_OPEN = "open"
    ACTION_MORE = "more"

    def __init__(self, on_action: Callable[[str], None],
                 logger: Optional[logging.Logger] = None):
        self._on_action = on_action
        self._log = logger or logging.getLogger("controller-setup")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._selector = None
        self._devices: Dict[str, object] = {}
        self._axis_state: Dict[tuple, int] = {}
        self._last_scan = 0.0
        self._last_action = 0.0

    @property
    def available(self) -> bool:
        return evdev is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.available:
            return False
        if self.running:
            return True
        self._stop.clear()
        self._axis_state.clear()
        self._last_scan = 0.0
        self._thread = threading.Thread(
            target=self._run, name="controller-setup-pad", daemon=True)
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
                    self._stop.wait(0.25)
                    continue
                try:
                    events = self._selector.select(timeout=0.25)
                except Exception:
                    self._close_all()
                    continue
                for key, _mask in events:
                    path, device = key.data
                    self._read_device(path, device)
        except Exception as ex:
            self._log.warning("gamepad listener stopped: %s", ex)
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
            device = self._open(path)
            if device is not None:
                try:
                    self._selector.register(
                        device.fileno(), selectors.EVENT_READ,
                        (path, device))
                except Exception:
                    device.close()
                    continue
                self._devices[path] = device

    def _open(self, path: str):
        try:
            device = evdev.InputDevice(path)
            caps = device.capabilities().get(e.EV_KEY, [])
            gamepad = any(code in caps for code in (
                e.BTN_GAMEPAD, e.BTN_SOUTH, e.BTN_A))
            if not gamepad:
                device.close()
                return None
            # Some python-evdev builds lack InputDevice.set_blocking; set the
            # fd directly so selector-driven reads stay non-blocking.
            os.set_blocking(device.fileno(), False)
            return device
        except Exception:
            return None

    def _read_device(self, path: str, device) -> None:
        try:
            for event in device.read():
                self._handle_event(path, device, event)
        except BlockingIOError:
            return
        except OSError:
            self._drop(path)

    def _drop(self, path: str) -> None:
        device = self._devices.pop(path, None)
        if device is None:
            return
        try:
            self._selector.unregister(device.fileno())
        except Exception:
            pass
        try:
            device.close()
        except Exception:
            pass
        for key in [k for k in self._axis_state if k[0] == path]:
            self._axis_state.pop(key, None)

    def _close_all(self) -> None:
        for path in list(self._devices):
            self._drop(path)

    # -- event translation ---------------------------------------------------

    def _emit(self, action: str) -> None:
        now = time.monotonic()
        if now - self._last_action < 0.12:
            return
        self._last_action = now
        try:
            self._on_action(action)
        except Exception:
            self._log.exception("gamepad action failed")

    def _handle_event(self, path: str, device, event) -> None:
        if event.type == e.EV_KEY and event.value == 1:
            action = {
                e.BTN_DPAD_UP: self.ACTION_PREV,
                e.BTN_DPAD_LEFT: self.ACTION_PREV,
                e.BTN_DPAD_DOWN: self.ACTION_NEXT,
                e.BTN_DPAD_RIGHT: self.ACTION_NEXT,
                e.BTN_SOUTH: self.ACTION_ACTIVATE,
                e.BTN_A: self.ACTION_ACTIVATE,
                e.BTN_EAST: self.ACTION_BACK,
                e.BTN_B: self.ACTION_BACK,
                e.BTN_START: self.ACTION_OPEN,
                e.BTN_SELECT: self.ACTION_MORE,
                e.BTN_MODE: self.ACTION_OPEN,
            }.get(event.code)
            if action:
                self._emit(action)
            return

        if event.type != e.EV_ABS:
            return
        if event.code not in (e.ABS_HAT0Y, e.ABS_HAT0X, e.ABS_Y):
            return
        try:
            info = device.absinfo(event.code)
            if not (info.min < 0 < info.max):
                return
            span = max(1, info.max - info.min)
            normalised = (event.value - info.min) / span
        except Exception:
            return
        direction = 0
        if normalised <= 0.32:
            direction = -1
        elif normalised >= 0.68:
            direction = 1
        key = (path, event.code)
        previous = self._axis_state.get(key, 0)
        self._axis_state[key] = direction
        if direction != 0 and direction != previous:
            self._emit(self.ACTION_PREV if direction < 0 else self.ACTION_NEXT)
