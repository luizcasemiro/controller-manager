#!/usr/bin/env python3
"""
Controller Manager - button binding GUI (tecla por tecla).

A standalone GTK3 app that talks to the controller-manager daemon over D-Bus
(interface CTRLMGR_IFACE on BUS_NAME/CTRLMGR_PATH). It is deliberately a
separate process: the daemon never links a GUI toolkit (see
docs/architecture/overview.md). Each connected controller has its own bind set;
a button with no bind falls through to the program's quirk remap.

To record a binding the physical button is pressed on the pad: the daemon
briefly releases its grab, captures the next button press, re-asserts the
remap, and reports the code back as a D-Bus signal.
"""

import os, subprocess, sys

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import dbus, dbus.service, dbus.mainloop.glib

# Same config path the daemon uses; kept here so the GUI can point the user at
# it without reimplementing the daemon's path resolution.
CONFIG_FILE = os.path.expanduser("~/.config/controller-modes.json")

# User systemd unit the daemon runs under, restarted through the "Restart
# service" button ("systemctl --user", no root needed for a user unit).
SERVICE_NAME = "controller-manager.service"

# Well-known identity the daemon's binding controller owns; object path and
# interface are constant across the two processes, so the GUI needs no config
# of its own.
BUS_NAME      = "org.ctrlmgr.ControllerManager1"
CTRLMGR_PATH  = "/ControllerManager"
CTRLMGR_IFACE = "org.ctrlmgr.ControllerManager1"

# Unique-name guard: a second instance of this GUI coexisting with the first
# would race it for capture windows. Taking the name at startup makes the
# second instance exit immediately; the daemon also spawns only one.
GUI_BUS_NAME  = "org.ctrlmgr.ControllerManagerGui"

CAPTURE_TIMEOUT = 8.0   # seconds the daemon waits for the physical press


class BindingGui:
    def __init__(self, bus, preselect=None):
        self._bus = bus
        try:
            self._obj = bus.get_object(BUS_NAME, CTRLMGR_PATH)
        except Exception as ex:
            self._fatal(f"cannot reach the daemon on {BUS_NAME}: {ex}")
        self._iface = dbus.Interface(self._obj, CTRLMGR_IFACE)
        self._capture_ident = None   # ident the daemon is capturing right now

        builder = None
        self._build()
        self._window.connect("destroy", Gtk.main_quit)

        # Incoming daemon events land on the GLib main loop, so emitting the
        # Gtk updates straight from the handler is safe.
        bus.add_signal_receiver(
            self._on_capture_result, "CaptureResult", CTRLMGR_IFACE,
            BUS_NAME, CTRLMGR_PATH)
        bus.add_signal_receiver(
            self._on_bindings_changed, "BindingsChanged", CTRLMGR_IFACE,
            BUS_NAME, CTRLMGR_PATH)

        self._window.show_all()
        self._refresh_controllers(preselect)

    def _fatal(self, msg):
        print(f"controller-gui: {msg}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------ UI ----

    def _build(self):
        win = Gtk.Window(title="Controller bindings")
        win.set_default_size(920, 480)
        self._window = win

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        win.add(vbox)

        top = Gtk.Box(spacing=6)
        top.set_margin_start(8); top.set_margin_top(8); top.set_margin_end(8)
        lab = Gtk.Label(label="Controller:", xalign=0)
        self._combo = Gtk.ComboBoxText()
        self._combo.connect("changed", self._on_controller_changed)
        self._mode_lbl = Gtk.Label(label="", xalign=0)
        top.pack_start(lab, False, False, 0)
        top.pack_start(self._combo, False, False, 0)
        top.pack_start(self._mode_lbl, True, True, 0)
        vbox.pack_start(top, False, False, 0)

        self._store = Gtk.ListStore(int, str, str, bool)
        tree = Gtk.TreeView(model=self._store)
        col = Gtk.TreeViewColumn("Controller button", Gtk.CellRendererText(),
                                 text=1)
        col_b = Gtk.TreeViewColumn("Forwards as", Gtk.CellRendererText(),
                                   text=2)
        tree.append_column(col)
        tree.append_column(col_b)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(tree)
        vbox.pack_start(scroll, True, True, 0)

        self._status = Gtk.Statusbar()
        self._status_ctx = self._status.get_context_id("bind")
        vbox.pack_start(self._status, False, False, 0)

        actions = Gtk.Box(spacing=6)
        actions.set_margin_start(8); actions.set_margin_bottom(8)
        actions.set_margin_end(8)
        self._bind_btn = Gtk.Button(label="Remap a button...")
        self._bind_btn.connect("clicked", self._on_remap_clicked)
        reset = Gtk.Button(label="Reset this controller's binds")
        reset.connect("clicked", self._on_reset_clicked)
        config = Gtk.Button(label="Config folder...")
        config.connect("clicked", self._on_open_config_clicked)
        restart = Gtk.Button(label="Restart service")
        restart.connect("clicked", self._on_restart_clicked)
        close = Gtk.Button(label="Close")
        close.connect("clicked", Gtk.main_quit)
        actions.pack_start(self._bind_btn, False, False, 0)
        actions.pack_start(reset, False, False, 0)
        actions.pack_start(config, False, False, 0)
        actions.pack_end(close, False, False, 0)
        actions.pack_end(restart, False, False, 0)
        vbox.pack_start(actions, False, False, 0)

    def _status_set(self, text):
        self._status.pop(self._status_ctx)
        self._status.push(self._status_ctx, text)

    # --------------------------------------------------------- D-Bus data ----

    def _list_controllers(self):
        rows = []
        for ident, name, family, mode in self._iface.ListControllers():
            rows.append((str(ident), str(name), str(family), str(mode)))
        return rows

    def _refresh_controllers(self, preselect=None):
        try:
            rows = self._list_controllers()
        except dbus.DBusException as ex:
            self._status_set(f"daemon error: {ex}")
            return
        self._combo.handler_block_by_func(self._on_controller_changed)
        self._combo.remove_all()
        for ident, name, family, mode in rows:
            label = f"{name}  [{mode}]"
            self._combo.append(ident, label)
        if rows:
            if preselect and any(r[0] == preselect for r in rows):
                self._combo.set_active_id(preselect)
            else:
                self._combo.set_active(0)
        self._combo.handler_unblock_by_func(self._on_controller_changed)
        self._on_controller_changed()

    def _current_ident(self):
        return self._combo.get_active_id()

    def _on_controller_changed(self, *_):
        ident = self._current_ident()
        if not ident:
            self._store.clear()
            self._mode_lbl.set_text("")
            self._bind_btn.set_sensitive(False)
            self._status_set("no controller connected")
            return
        self._bind_btn.set_sensitive(True)
        self._refresh_rows(ident)

    def _refresh_rows(self, ident):
        self._store.clear()
        try:
            buttons = list(self._iface.GetButtons(ident))
            binds = {
                int(k): int(v)
                for k, v in self._iface.GetBindings(ident).items()
            }
        except dbus.DBusException as ex:
            self._status_set(f"daemon error: {ex}")
            return
        for code, label in buttons:
            dst = binds.get(int(code))
            if dst is None:
                shown = "program default"
            else:
                shown = self._dst_label(int(dst)) or f"code {int(dst)}"
            self._store.append([int(code), str(label), shown, dst is not None])

    def _dst_label(self, code):
        try:
            for c, label in self._iface.GetTargetButtons():
                if int(c) == code:
                    return str(label)
        except dbus.DBusException:
            pass
        return None

    # ----------------------------------------------------------- actions ----

    def _on_remap_clicked(self, *_):
        ident = self._current_ident()
        if not ident:
            self._status_set("connect a controller first")
            return
        try:
            ok = bool(self._iface.CaptureStart(ident, CAPTURE_TIMEOUT))
        except dbus.DBusException as ex:
            self._status_set(f"daemon error: {ex}")
            return
        if not ok:
            self._status_set("another capture is already running")
            return
        self._capture_ident = ident
        self._bind_btn.set_sensitive(False)
        self._status_set("press the button on the controller you want to bind")

    def _on_reset_clicked(self, *_):
        ident = self._current_ident()
        if not ident:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self._window, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Reset every button binding on this controller?")
        dialog.format_secondary_text(
            "Buttons without a binding keep the program's default remap.")
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            try:
                self._iface.ResetBindings(ident)
            except dbus.DBusException as ex:
                self._status_set(f"daemon error: {ex}")
                return
            self._refresh_rows(ident)
            self._status_set("bindings reset to the program default")

    def _on_open_config_clicked(self, *_):
        folder = os.path.dirname(CONFIG_FILE)
        try:
            subprocess.Popen(["xdg-open", folder])
        except Exception as ex:
            self._status_set(f"cannot open config folder: {ex}")

    def _on_restart_clicked(self, *_):
        dialog = Gtk.MessageDialog(
            transient_for=self._window, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Restart the {SERVICE_NAME} service?")
        dialog.format_secondary_text(
            "Controllers will reconnect and keep their modes and bindings.")
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.YES:
            return
        try:
            ret = subprocess.call(
                ["systemctl", "--user", "restart", SERVICE_NAME])
        except Exception as ex:
            self._status_set(f"cannot restart service: {ex}")
            return
        if ret != 0:
            self._status_set(f"service restart failed (exit {ret})")
            return
        self._refresh_controllers()
        self._status_set("service restarted")

    # ------------------------------------------------------ daemon signals ----

    def _on_capture_result(self, ident, code):
        ident = str(ident)
        code = int(code)
        if ident != self._capture_ident:
            return
        self._capture_ident = None
        self._bind_btn.set_sensitive(True)
        # The user may have switched controllers while capturing; the bind must
        # land on the controller that was captured, so select it back.
        if self._current_ident() != ident:
            self._combo.set_active_id(ident)
            self._refresh_controllers()
        if code == 0:
            self._status_set("no press recorded (timeout or cancelled)")
            return
        self._offer_target(ident, code)

    def _on_bindings_changed(self, ident):
        if not self._current_ident():
            return
        self._refresh_rows(self._current_ident())

    # -------------------------------------------------------- target picker ----

    def _offer_target(self, ident, src):
        try:
            targets = list(self._iface.GetTargetButtons())
        except dbus.DBusException as ex:
            self._status_set(f"daemon error: {ex}")
            return
        src_label = self._label_for(ident, src)
        dialog = Gtk.Dialog(
            title=f"Bind {src_label} to...", transient_for=self._window,
            modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Bind", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.add(Gtk.Label(
            label=f"Button {src_label} will be sent as:", xalign=0))
        combo = Gtk.ComboBoxText()
        combo.append("-1", "program default (keep the controller's own remap)")
        for c, label in targets:
            combo.append(str(int(c)), str(label))
        combo.set_active(0)
        area.add(combo)
        dialog.show_all()
        resp = dialog.run()
        dst = int(combo.get_active_id())
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            self._status_set("bind cancelled")
            return
        try:
            self._iface.SetBinding(ident, src, dst)
        except dbus.DBusException as ex:
            self._status_set(f"daemon error: {ex}")
            return
        self._refresh_rows(ident)
        self._status_set(f"{src_label} now sends as "
                         f"{self._dst_label(dst) or 'program default'}")

    def _label_for(self, ident, code):
        try:
            for c, label in self._iface.GetButtons(ident):
                if int(c) == code:
                    return str(label)
        except dbus.DBusException:
            pass
        return f"code {code}"


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    # Single instance guard (see GUI_BUS_NAME above).
    try:
        dbus.service.BusName(GUI_BUS_NAME, bus, do_not_queue=True)
    except dbus.exceptions.NameExistsException:
        sys.exit(0)

    preselect = sys.argv[1] if len(sys.argv) > 1 else None
    BindingGui(bus, preselect)
    Gtk.main()


if __name__ == "__main__":
    main()