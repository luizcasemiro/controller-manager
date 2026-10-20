"""Controller overlay opened by LT + RT + Home.

The overlay is a separate GTK process. It talks to the daemon only through the
health module and reuses the installed pairing UI for "Parear novo controle".
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .gamepad import GamepadInput  # noqa: E402
from .health import ControllerManagerHealth  # noqa: E402
from .support import GameModeRunner, HeroicLauncher, setup_logging  # noqa: E402

SETUP_SCRIPT = os.path.expanduser("~/.local/bin/controller-setup.py")
if not os.path.exists(SETUP_SCRIPT):
    SETUP_SCRIPT = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "controller-setup.py")

CSS = """
#overlay-window {
    background-color: rgba(3, 8, 18, 0.96);
}
.overlay-root {
    padding: 34px;
}
.overlay-card {
    background-color: rgba(16, 27, 47, 0.98);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 26px;
    padding: 30px 38px 26px 38px;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.55);
}
.overlay-brand {
    font-size: 12px;
    font-weight: 800;
    color: #78a7ff;
}
.overlay-title {
    font-size: 36px;
    font-weight: 800;
    color: #f8fbff;
}
.overlay-sub {
    font-size: 15px;
    color: #8d9db8;
}
.status {
    font-size: 13px;
    font-weight: 700;
    color: #a8b7cd;
}
.status-ok {
    color: #52e6a1;
}
.status-warn {
    color: #ffc86b;
}
.status-error {
    color: #ff8e99;
}
button.menu,
button.danger,
button.quiet {
    border-radius: 15px;
    padding: 15px 24px;
    font-size: 18px;
    font-weight: 700;
    border: 1px solid transparent;
    background-image: none;
}
button.menu {
    background-color: rgba(255, 255, 255, 0.055);
    color: #e8f0fc;
    border: 1px solid rgba(255, 255, 255, 0.09);
}
button.menu:hover {
    background-color: rgba(255, 255, 255, 0.10);
}
button.danger {
    background-color: rgba(239, 82, 96, 0.14);
    color: #ffc4ca;
    border: 1px solid rgba(239, 82, 96, 0.32);
}
button.danger:hover {
    background-color: rgba(239, 82, 96, 0.22);
}
button.quiet {
    background-color: transparent;
    color: #8292aa;
}
button.quiet:hover {
    color: #e8f0fc;
}
button.menu:focus,
button.danger:focus,
button.quiet:focus,
button.menu.pad-focus,
button.danger.pad-focus,
button.quiet.pad-focus {
    border: 2px solid #f4f8ff;
    box-shadow: 0 0 0 4px rgba(120, 167, 255, 0.30);
}
.hint {
    font-size: 12px;
    color: #6d7f98;
}
.error {
    font-size: 13px;
    color: #ff8e99;
}
"""


class OverlayWindow(Gtk.Window):
    def __init__(self, health: ControllerManagerHealth,
                 heroic: HeroicLauncher, game_mode: GameModeRunner,
                 logger: Optional[logging.Logger] = None):
        super().__init__(title="Controller Manager")
        self._health = health
        self._heroic = heroic
        self._game_mode = game_mode
        self._log = logger or logging.getLogger("controller-setup")
        self._closing = False
        self._busy = False
        self._confirming = False
        self._manager_ok = False
        self._manager_detail = "Verificando Controller Manager..."
        self._manager_count = 0
        self._last_health = 0.0
        self._pad = GamepadInput(self._pad_action_from_thread, logger=self._log)
        self._pad_started = False
        self._buttons: List[Gtk.Button] = []
        self._focus_widget: Optional[Gtk.Button] = None

        self.set_name("overlay-window")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_default_size(1280, 720)
        self.set_opacity(0.985)
        self.connect("delete-event", self._on_close)
        self.connect("key-press-event", self._on_key)
        self._load_css()
        self._build()
        self.show_all()
        self._present()
        self._pad.start()
        self._pad_started = True
        self._focus_first()
        GLib.timeout_add(180, self._focus_first)
        self._refresh_status()
        self._refresh_health()

    # -- UI ------------------------------------------------------------------

    def _load_css(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS.encode("utf-8"))
        except Exception as ex:
            self._log.warning("overlay CSS failed: %s", ex)
            return
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _label(self, text="", css=None, xalign=0.0, wrap=False):
        label = Gtk.Label(label=text, xalign=xalign)
        if css:
            label.get_style_context().add_class(css)
        if wrap:
            label.set_line_wrap(True)
            label.set_max_width_chars(72)
        return label

    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        root.get_style_context().add_class("overlay-root")
        root.set_halign(Gtk.Align.CENTER)
        root.set_valign(Gtk.Align.CENTER)
        self.add(root)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.get_style_context().add_class("overlay-card")
        card.set_size_request(720, -1)
        root.pack_start(card, False, False, 0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.pack_start(self._label("CONTROLLER MANAGER", "overlay-brand"),
                       False, False, 0)
        self._manager_label = self._label("Controller Manager: ...", "status")
        top.pack_end(self._manager_label, False, False, 0)
        card.pack_start(top, False, False, 0)

        title = self._label("Op\u00e7\u00f5es do controle", "overlay-title")
        subtitle = self._label("Game Mode e controles", "overlay-sub")
        card.pack_start(title, False, False, 0)
        card.pack_start(subtitle, False, False, 0)

        self._game_label = self._label("Game Mode: ...", "status")
        self._count_label = self._label("Controles: ...", "status")
        card.pack_start(self._game_label, False, False, 0)
        card.pack_start(self._count_label, False, False, 0)

        self._menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=10)
        self._pair_button = self._menu_button(
            "Parear novo controle", self._on_pair)
        self._hero_button = self._menu_button(
            "Abrir Heroic", self._on_open_heroic)
        self._exit_button = self._menu_button(
            "Sair do Game Mode", self._on_exit_game_mode, "danger")
        self._close_button = self._menu_button(
            "Fechar overlay", self._on_close, "quiet")
        for button in (self._pair_button, self._hero_button,
                       self._exit_button, self._close_button):
            self._menu_box.pack_start(button, False, False, 0)
        card.pack_start(self._menu_box, False, False, 0)

        self._confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=10)
        self._confirm_label = self._label(
            "Sair do Game Mode? O monitor e o audio serao restaurados.",
            "overlay-sub", wrap=True)
        self._cancel_button = self._menu_button(
            "Cancelar", self._on_cancel_exit, "quiet")
        self._confirm_button = self._menu_button(
            "Confirmar", self._on_confirm_exit, "danger")
        self._confirm_box.pack_start(self._confirm_label, False, False, 0)
        self._confirm_box.pack_start(self._cancel_button, False, False, 0)
        self._confirm_box.pack_start(self._confirm_button, False, False, 0)
        card.pack_start(self._confirm_box, False, False, 0)
        self._confirm_box.set_no_show_all(True)
        self._confirm_box.hide()

        self._message = self._label("", "status")
        self._error = self._label("", "error", wrap=True)
        card.pack_start(self._message, False, False, 0)
        card.pack_start(self._error, False, False, 0)

        hint = self._label(
            "D-pad/Stick: navegar   \u2022   A: selecionar   \u2022   "
            "B: fechar   \u2022   Start: Heroic   \u2022   Select: parear",
            "hint", xalign=0.5)
        card.pack_start(hint, False, False, 0)

    def _menu_button(self, label, callback, css="menu"):
        button = Gtk.Button(label=label)
        button.get_style_context().add_class(css)
        button.connect("clicked", callback)
        button.connect("focus-in-event", self._on_button_focus)
        self._buttons.append(button)
        return button

    def _on_button_focus(self, button, _event):
        if button is not self._focus_widget:
            if self._focus_widget is not None:
                self._focus_widget.get_style_context().remove_class(
                    "pad-focus")
            self._focus_widget = button
            button.get_style_context().add_class("pad-focus")
        return False

    def _present(self):
        display = Gdk.Display.get_default()
        if display is not None:
            monitor = display.get_primary_monitor()
            if monitor is None and display.get_n_monitors():
                monitor = display.get_monitor(0)
            if monitor is not None:
                geometry = monitor.get_geometry()
                self.unfullscreen()
                self.move(geometry.x, geometry.y)
                self.resize(geometry.width, geometry.height)
        self.fullscreen()
        self.present()
        self._activate_window()

    def _activate_window(self):
        """Ask the WM for focus; fullscreen games often start unfocused."""
        try:
            self.set_focus_on_map(True)
            self.present_with_time(Gdk.CURRENT_TIME)
            window = self.get_window()
            if window is not None:
                window.raise_()
                window.focus(Gdk.CURRENT_TIME)
        except Exception:
            pass
        return False

    # -- status --------------------------------------------------------------

    def _refresh_status(self):
        active = self._game_mode.is_active()
        self._game_label.set_text(
            "Game Mode: " + ("ATIVO" if active else "INATIVO"))
        for cls in ("status-ok", "status-warn"):
            self._game_label.get_style_context().remove_class(cls)
        self._game_label.get_style_context().add_class(
            "status-ok" if active else "status-warn")
        checking = self._manager_detail.startswith("Verificando")
        if checking:
            manager_state = "VERIFICANDO"
        elif self._manager_ok:
            manager_state = "ATIVO"
            if self._manager_count:
                manager_state += f" ({self._manager_count})"
        else:
            manager_state = "INDISPONIVEL"
        self._manager_label.set_text("Controller Manager: " + manager_state)
        for cls in ("status-ok", "status-error", "status-warn"):
            self._manager_label.get_style_context().remove_class(cls)
        self._manager_label.get_style_context().add_class(
            "status-warn" if checking or not self._manager_ok else "status-ok")
        self._count_label.set_text(
            f"Controles gerenciados: {self._manager_count}")
        self._message.set_text(self._manager_detail)

    def _refresh_health(self):
        if self._busy:
            return
        self._error.set_text("")
        self._manager_detail = "Verificando Controller Manager..."
        self._refresh_status()
        threading.Thread(target=self._health_worker,
                         name="overlay-health", daemon=True).start()

    def _health_worker(self):
        ok, detail, rows = self._health.check(restart=True, timeout=8.0)
        GLib.idle_add(self._health_done, ok, detail, len(rows))

    def _health_done(self, ok, detail, count):
        if self._closing:
            return False
        self._manager_ok = bool(ok)
        self._manager_detail = detail or (
            "Controller Manager ativo" if ok else "Controller Manager indisponivel")
        self._manager_count = int(count)
        self._refresh_status()
        GLib.timeout_add(50, self._focus_first)
        return False

    # -- actions -------------------------------------------------------------

    def _set_busy(self, message: str):
        self._busy = True
        self._message.set_text(message)
        for button in self._buttons:
            button.set_sensitive(False)

    def _clear_busy(self):
        self._busy = False
        for button in self._buttons:
            button.set_sensitive(True)

    def _on_pair(self, *_):
        if self._busy:
            return
        if not os.path.exists(SETUP_SCRIPT):
            self._error.set_text(
                "Tela de pareamento nao encontrada: " + SETUP_SCRIPT)
            return
        self._set_busy("Abrindo o pareamento...")
        self._pad.stop()
        self.hide()
        threading.Thread(target=self._pair_worker,
                         name="overlay-pair", daemon=True).start()

    def _pair_worker(self):
        try:
            result = subprocess.run(
                [sys.executable, SETUP_SCRIPT, "--foreground", "--pair-only"],
                capture_output=True, text=True, timeout=1800, check=False)
            detail = ""
            if result.returncode != 0:
                text = ((result.stderr or "") + "\n"
                        + (result.stdout or "")).strip()
                detail = text.splitlines()[-1] if text else "pareamento falhou"
            GLib.idle_add(self._pair_done, result.returncode, detail)
        except Exception as ex:
            GLib.idle_add(self._pair_done, 1, str(ex))

    def _pair_done(self, returncode, detail):
        self.show()
        self._present()
        self._pad.start()
        self._clear_busy()
        self._refresh_health()
        if returncode != 0:
            self._error.set_text(detail)
        self._focus_first()
        return False

    def _on_open_heroic(self, *_):
        if self._busy:
            return
        self._set_busy("Preparando o Heroic...")
        self._pad.stop()
        threading.Thread(target=self._heroic_worker,
                         name="overlay-heroic", daemon=True).start()

    def _heroic_worker(self):
        ok, detail, rows = self._health.check(
            restart=True, timeout=8.0)
        if not ok or not rows:
            GLib.idle_add(
                self._action_failed,
                detail or "Nenhum controle gerenciado pelo Controller Manager")
            return
        gm_ok, gm_error = self._game_mode.activate()
        if not gm_ok:
            GLib.idle_add(self._action_failed, gm_error)
            return
        ok, error = self._heroic.launch()
        if ok:
            # Post-launch health: a dead service would leave the controller
            # unmanaged even though the game started.
            time.sleep(0.8)
            post_ok, post_detail, _rows = self._health.check(
                restart=True, timeout=4.0)
            self._log.info("overlay post-Heroic manager check: ok=%s detail=%s",
                           post_ok, post_detail)
            GLib.idle_add(self._quit)
        else:
            GLib.idle_add(self._action_failed, error)

    def _action_failed(self, detail):
        self._pad.start()
        self._clear_busy()
        self._error.set_text(detail or "Acao falhou")
        self._refresh_status()
        self._focus_first()
        return False

    def _on_exit_game_mode(self, *_):
        if self._busy:
            return
        self._confirming = True
        self._menu_box.hide()
        self._confirm_box.set_no_show_all(False)
        self._confirm_box.show_all()
        self._set_focus(self._cancel_button)

    def _on_cancel_exit(self, *_):
        self._confirming = False
        self._confirm_box.hide()
        self._menu_box.show_all()
        self._set_focus(self._exit_button)

    def _on_confirm_exit(self, *_):
        if self._busy:
            return
        self._set_busy("Saindo do Game Mode...")
        self._pad.stop()
        threading.Thread(target=self._exit_worker,
                         name="overlay-gamemode", daemon=True).start()

    def _exit_worker(self):
        ok, error = self._game_mode.disable()
        if ok:
            GLib.idle_add(self._quit)
        else:
            GLib.idle_add(self._action_failed, error)

    # -- navigation ----------------------------------------------------------

    def _focusable(self):
        if self._confirming:
            return [b for b in (self._cancel_button, self._confirm_button)
                    if b.get_visible() and b.get_sensitive()]
        return [b for b in (self._pair_button, self._hero_button,
                            self._exit_button, self._close_button)
                if b.get_visible() and b.get_sensitive()]

    def _focus_first(self):
        buttons = self._focusable()
        if buttons:
            self._set_focus(buttons[0])
        return False

    def _set_focus(self, button):
        if self._focus_widget is not None and self._focus_widget is not button:
            self._focus_widget.get_style_context().remove_class("pad-focus")
        self._focus_widget = button
        button.get_style_context().add_class("pad-focus")
        button.grab_focus()

    def _move_focus(self, step: int):
        buttons = self._focusable()
        if not buttons:
            return
        focus = self._focus_widget
        index = buttons.index(focus) if focus in buttons else (
            -1 if step > 0 else 0)
        self._set_focus(buttons[(index + step) % len(buttons)])

    def _pad_action_from_thread(self, action: str):
        GLib.idle_add(self._on_pad_action, action)

    def _on_pad_action(self, action: str):
        if self._closing or self._busy:
            return False
        self._activate_window()
        if action == GamepadInput.ACTION_PREV:
            self._move_focus(-1)
        elif action == GamepadInput.ACTION_NEXT:
            self._move_focus(1)
        elif action == GamepadInput.ACTION_BACK:
            if self._confirming:
                self._on_cancel_exit()
            else:
                self._quit()
        elif action == GamepadInput.ACTION_ACTIVATE:
            buttons = self._focusable()
            focus = self._focus_widget
            if focus in buttons:
                focus.emit("clicked")
            else:
                self._focus_first()
        elif action == GamepadInput.ACTION_OPEN:
            if not self._confirming:
                self._on_open_heroic()
        elif action == GamepadInput.ACTION_MORE:
            if not self._confirming:
                self._on_pair()
        return False

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            if self._busy:
                return True
            if self._confirming:
                self._on_cancel_exit()
            else:
                self._quit()
            return True
        return False

    def _on_close(self, *_):
        if self._busy:
            return True
        self._quit()
        return True

    def _quit(self):
        if self._closing:
            return False
        self._closing = True
        self._pad.stop()
        Gtk.main_quit()
        return False


def run_overlay(health: Optional[ControllerManagerHealth] = None,
                heroic: Optional[HeroicLauncher] = None,
                game_mode: Optional[GameModeRunner] = None,
                logger: Optional[logging.Logger] = None) -> int:
    log = logger or setup_logging()
    config = None
    if heroic is None or game_mode is None:
        from .support import load_config
        config = load_config()
    if health is None:
        health = ControllerManagerHealth(logger=log)
    if heroic is None:
        heroic = HeroicLauncher(config.flatpak_app, config.heroic_args,
                                command=config.heroic_command)
    if game_mode is None:
        game_mode = GameModeRunner(config.gamemode_script)
    OverlayWindow(health, heroic, game_mode, logger=log)
    Gtk.main()
    return 0
