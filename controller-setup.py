#!/usr/bin/env python3
"""KDE Connect entry point: Game Mode + visual Bluetooth controller pairing.

One command prepares the TV/audio environment, scans for Bluetooth gamepads,
pairs/trusts/connects them and finally offers a button to start Heroic in
console mode. The tray daemon is not touched; this process runs only for the
duration of the setup session.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys

from controller_setup.support import (GameModeRunner, HeroicLauncher,
                                      load_config, setup_logging, state_dir)


def acquire_lock():
    os.makedirs(state_dir(), exist_ok=True)
    handle = open(os.path.join(state_dir(), "session.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def fatal(message: str, log) -> int:
    log.error("%s", message)
    print("controller-setup: " + message, file=sys.stderr)
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Controller Manager",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
    except Exception:
        pass
    return 1


def detached_launch() -> int:
    """Hand the UI to a detached child and return to KDE Connect immediately.

    The parent takes the single-instance lock first and passes its descriptor
    to the child. Closing the parent's copy does not release the flock because
    the child still holds the same open file description, so two commands
    cannot start two sessions.
    """
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        import dbus.mainloop.glib  # noqa: F401
    except Exception as ex:
        print("controller-setup: missing graphical dependency: " + str(ex),
              file=sys.stderr)
        return 1
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("controller-setup: no graphical session (DISPLAY)", file=sys.stderr)
        return 1
    lock = acquire_lock()
    if lock is None:
        print("controller-setup: already running", file=sys.stderr)
        return 0
    env = os.environ.copy()
    env["CONTROLLER_SETUP_DETACHED"] = "1"
    env["CONTROLLER_SETUP_LOCK_FD"] = str(lock.fileno())
    command = [sys.executable, os.path.realpath(__file__), "--foreground"]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(lock.fileno(),),
            start_new_session=True,
            env=env,
        )
    except Exception as ex:
        print("controller-setup: cannot start UI: " + str(ex),
              file=sys.stderr)
        return 1
    finally:
        lock.close()
    return 0


def main() -> int:
    foreground = "--foreground" in sys.argv[1:]
    pair_only = "--pair-only" in sys.argv[1:]
    if not foreground:
        return detached_launch()

    detached = os.environ.get("CONTROLLER_SETUP_DETACHED") == "1"
    lock = None
    if not detached:
        lock = acquire_lock()
        if lock is None:
            print("controller-setup: already running", file=sys.stderr)
            return 0
    else:
        # Refresh the PID written by the parent; the inherited flock stays
        # held for the lifetime of this process.
        try:
            fd = int(os.environ.get("CONTROLLER_SETUP_LOCK_FD", "-1"))
            if fd >= 0:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, str(os.getpid()).encode())
        except Exception:
            pass

    log = setup_logging()
    try:
        # Register the GLib-backed D-Bus main loop before the BlueZ agent is
        # created. The agent's incoming method calls then dispatch while the
        # worker thread blocks in Pair().
        import dbus.mainloop.glib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        from controller_setup.bluez import BlueZBackend
        from controller_setup.health import ControllerManagerHealth
        from controller_setup.session import PairingSession
        from controller_setup.ui import SetupWindow
        from gi.repository import Gtk

        config = load_config()
        backend = BlueZBackend()
        game_mode = GameModeRunner(config.gamemode_script)
        heroic = HeroicLauncher(config.flatpak_app, config.heroic_args,
                                command=config.heroic_command)
        session = PairingSession(
            backend, game_mode, heroic, logger=log,
            manager_health=ControllerManagerHealth(logger=log),
            pair_only=pair_only)

        window = SetupWindow(session)
        if not session.start():
            window.destroy()
            return 1
        log.info("controller-setup UI started")
        Gtk.main()
        log.info("controller-setup UI closed")
        return 0
    except Exception as ex:
        log.exception("controller-setup failed")
        return fatal(
            "Nao foi possivel iniciar a tela de pareamento: " + str(ex), log)
    finally:
        try:
            if lock is not None:
                lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
