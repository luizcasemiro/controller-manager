#!/usr/bin/env python3
"""Standalone overlay opened by the LT + RT + Home chord."""

from __future__ import annotations

import fcntl
import os
import sys

from controller_setup.support import setup_logging, state_dir


def acquire_lock():
    os.makedirs(state_dir(), exist_ok=True)
    handle = open(os.path.join(state_dir(), "overlay.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> int:
    log = setup_logging()
    lock = acquire_lock()
    if lock is None:
        log.info("overlay already running")
        return 0
    try:
        import dbus.mainloop.glib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        from controller_setup.overlay import run_overlay
        return run_overlay(logger=log)
    except Exception as ex:
        log.exception("overlay failed")
        print("controller-overlay: " + str(ex), file=sys.stderr)
        return 1
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
