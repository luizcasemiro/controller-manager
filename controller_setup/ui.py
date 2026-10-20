"""Fullscreen GTK3 pairing UI.

The visual language is a console dashboard rather than a settings dialog: a
large radar, one card per controller and two clear actions. The daemon stays
headless; this process is spawned by the KDE Connect command and owns its
window for the duration of the setup session.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    import cairo  # noqa: E402
except Exception:  # pragma: no cover - GTK ships pycairo in practice
    cairo = None

from .gamepad import GamepadInput  # noqa: E402
from .session import (  # noqa: E402
    PHASE_ACTIVATING,
    PHASE_CHECKING,
    PHASE_CLOSED,
    PHASE_CLOSING_HEROIC,
    PHASE_ERROR,
    PHASE_LAUNCHING,
    PHASE_READY,
    PHASE_SCANNING,
    PHASE_TIMEOUT,
    PHASE_VERIFYING_MANAGER,
    STATUS_CONNECTING,
    STATUS_ERROR,
    STATUS_PAIRING,
    STATUS_READY,
    STATUS_TRUSTING,
    PairingSession,
    UiSnapshot,
)

CSS = """
#setup-window {
    background-image: linear-gradient(to bottom, #060b16, #0a1426 55%, #151126);
    color: #f4f8ff;
}
.root {
    padding: 20px 48px 22px 48px;
}
.brand {
    font-size: 13px;
    font-weight: 800;
    color: #78a7ff;
}
.brand-sub {
    font-size: 12px;
    color: #6f809b;
}
.badge {
    border-radius: 999px;
    padding: 7px 15px;
    background-color: rgba(255, 255, 255, 0.055);
    border: 1px solid rgba(255, 255, 255, 0.09);
}
.badge-on {
    background-color: rgba(39, 202, 130, 0.14);
    border: 1px solid rgba(39, 202, 130, 0.35);
}
.badge-label {
    font-size: 12px;
    font-weight: 700;
    color: #a8b7cd;
}
.badge-on .badge-label {
    color: #52e6a1;
}
.badge-warn {
    background-color: rgba(255, 176, 76, 0.12);
    border: 1px solid rgba(255, 176, 76, 0.35);
}
.badge-warn .badge-label {
    color: #ffc86b;
}
.badge-error {
    background-color: rgba(239, 82, 96, 0.12);
    border: 1px solid rgba(239, 82, 96, 0.35);
}
.badge-error .badge-label {
    color: #ff8e99;
}
.hero-title {
    font-size: 40px;
    font-weight: 800;
    color: #f8fbff;
}
.hero-sub {
    font-size: 16px;
    color: #8d9db8;
}
.radar {
    background-color: transparent;
    border: none;
}
.countdown {
    font-size: 30px;
    font-weight: 700;
    color: #b9c9e2;
}
.countdown-active {
    color: #8fb7ff;
}
.section-label {
    font-size: 15px;
    font-weight: 700;
    color: #c7d5e9;
}
.empty-note {
    font-size: 14px;
    color: #71819a;
    padding: 20px;
}
.card {
    background-color: rgba(255, 255, 255, 0.045);
    border: 1px solid rgba(255, 255, 255, 0.075);
    border-radius: 18px;
    padding: 15px 20px;
}
.card-ready {
    background-color: rgba(36, 201, 126, 0.09);
    border: 1px solid rgba(36, 201, 126, 0.35);
}
.card-error {
    background-color: rgba(239, 82, 96, 0.075);
    border: 1px solid rgba(239, 82, 96, 0.32);
}
.icon-holder {
    border-radius: 14px;
    background-color: rgba(105, 145, 255, 0.13);
    padding: 10px;
}
.icon-holder-ready {
    background-color: rgba(36, 201, 126, 0.16);
}
.icon-holder-error {
    background-color: rgba(239, 82, 96, 0.13);
}
.device-name {
    font-size: 19px;
    font-weight: 700;
    color: #f4f8ff;
}
.device-detail {
    font-size: 13px;
    color: #91a1b9;
}
.device-error {
    font-size: 12px;
    color: #ff8e99;
}
.step {
    font-size: 12px;
    font-weight: 700;
    color: #65758e;
}
.step-done {
    color: #52e6a1;
}
.step-current {
    color: #ffc86b;
}
.status-pill {
    border-radius: 999px;
    padding: 6px 12px;
    background-color: rgba(255, 255, 255, 0.07);
}
.status-pill-ready {
    background-color: rgba(36, 201, 126, 0.18);
}
.status-pill-error {
    background-color: rgba(239, 82, 96, 0.18);
}
.status-pill-label {
    font-size: 12px;
    font-weight: 800;
    color: #c1cee2;
}
.status-pill-ready .status-pill-label {
    color: #52e6a1;
}
.status-pill-error .status-pill-label {
    color: #ff8e99;
}
.footer {
    margin-top: 4px;
}
.message {
    font-size: 15px;
    color: #dce7f7;
}
.message-error {
    color: #ff9aa4;
}
.detail {
    font-size: 13px;
    color: #7e8fa8;
}
.pad-hint {
    font-size: 12px;
    color: #6d7f98;
}
button.primary:focus,
button.secondary:focus,
button.ghost:focus {
    border: 2px solid #f4f8ff;
    box-shadow: 0 0 0 4px rgba(120, 167, 255, 0.30);
}
button.primary,
button.secondary,
button.ghost {
    border-radius: 16px;
    padding: 15px 32px;
    font-size: 17px;
    font-weight: 800;
    border: 1px solid transparent;
    background-image: none;
}
button.primary {
    background-image: linear-gradient(to bottom, #22c97e, #18a869);
    color: #04160c;
    box-shadow: 0 8px 24px rgba(20, 180, 105, 0.28);
}
button.primary:hover {
    background-image: linear-gradient(to bottom, #34dc91, #20ba76);
}
button.primary:disabled {
    background-image: none;
    background-color: #182438;
    color: #53647d;
    box-shadow: none;
}
button.secondary {
    background-color: rgba(255, 255, 255, 0.07);
    color: #d9e5f6;
    border: 1px solid rgba(255, 255, 255, 0.12);
}
button.secondary:hover {
    background-color: rgba(255, 255, 255, 0.11);
}
button.secondary:disabled {
    color: #506079;
    background-color: rgba(255, 255, 255, 0.025);
}
button.ghost {
    background-color: transparent;
    color: #8292aa;
    padding: 15px 18px;
}
button.ghost:hover {
    color: #d9e5f6;
}
"""


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


STEPS = (
    ("Pareando", STATUS_PAIRING),
    ("Confiando", STATUS_TRUSTING),
    ("Conectando", STATUS_CONNECTING),
    ("Pronto", STATUS_READY),
)


class Radar(Gtk.DrawingArea):
    """Lightweight Cairo radar; animates only while a scan is active."""

    def __init__(self):
        super().__init__()
        self.set_has_window(False)
        self.override_background_color(
            Gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 0))
        self._size = 300
        self.set_size_request(self._size, self._size)
        self.get_style_context().add_class("radar")
        self._angle = 0.0
        self._active = False
        self._ready = False
        self._error = False
        self.connect("draw", self._draw)
        GLib.timeout_add(40, self._tick)

    def set_radar_size(self, size: int) -> None:
        if size != self._size:
            self._size = size
            self.set_size_request(size, size)

    def set_state(self, active: bool, ready: bool, error: bool) -> None:
        if (ready, error, active) != (self._ready, self._error, self._active):
            self.queue_draw()
        self._active = active
        self._ready = ready
        self._error = error

    def _tick(self):
        if self._active:
            self._angle = (self._angle + 0.035) % (2 * math.pi)
            self.queue_draw()
        return True

    def _draw(self, _widget, cr):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        size = min(width, height)
        cx, cy = width / 2.0, height / 2.0
        radius = size * 0.42

        # GtkDrawingArea paints its theme background as a rectangle before
        # this callback. Cover the whole allocation with the surrounding
        # window colour so the radar does not look like a pasted square.
        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        if cairo is not None:
            backdrop = cairo.LinearGradient(0, 0, 0, height)
            backdrop.add_color_stop_rgb(0, 0.030, 0.057, 0.114)
            backdrop.add_color_stop_rgb(1, 0.036, 0.075, 0.145)
            cr.set_source(backdrop)
        else:
            cr.set_source_rgb(0.031, 0.059, 0.118)
        cr.paint()
        cr.restore()

        if cairo is not None:
            glow = cairo.RadialGradient(cx, cy, 0, cx, cy, radius * 1.12)
            glow.add_color_stop_rgba(0, 0.16, 0.30, 0.72, 0.055)
            glow.add_color_stop_rgba(1, 0.16, 0.30, 0.72, 0.0)
            cr.set_source(glow)
        else:
            cr.set_source_rgba(0.16, 0.30, 0.72, 0.035)
        cr.arc(cx, cy, radius * 1.08, 0, 2 * math.pi)
        cr.fill()

        cr.set_line_width(1.0)
        for factor, alpha in ((0.38, 0.16), (0.68, 0.13), (1.0, 0.11)):
            cr.set_source_rgba(0.42, 0.62, 1.0, alpha)
            cr.arc(cx, cy, radius * factor, 0, 2 * math.pi)
            cr.stroke()

        # Sweep: a soft radial wedge rotating around the centre.
        cr.save()
        cr.arc(cx, cy, radius, self._angle, self._angle + 0.95)
        cr.line_to(cx, cy)
        cr.close_path()
        cr.set_source_rgba(0.25, 0.52, 1.0, 0.12 if self._active else 0.04)
        cr.fill()
        cr.restore()

        # A few stable dots make the radar read as a device sweep.
        dots = ((0.55, 0.4), (0.72, 0.65), (0.38, 0.72), (0.62, 0.82),
                (0.82, 0.35), (0.28, 0.55))
        for index, (dx, dy) in enumerate(dots):
            x = cx + (dx - 0.5) * radius * 1.45
            y = cy + (dy - 0.5) * radius * 1.45
            pulse = 0.45 + 0.35 * math.sin(self._angle * 1.5 + index)
            if self._error:
                cr.set_source_rgba(0.95, 0.34, 0.38, 0.65)
            elif self._ready:
                cr.set_source_rgba(0.31, 0.90, 0.62, 0.8)
            else:
                cr.set_source_rgba(0.42, 0.65, 1.0, pulse)
            cr.arc(x, y, 3.2, 0, 2 * math.pi)
            cr.fill()

        # Centre disc.
        if self._error:
            centre = (0.95, 0.30, 0.35, 0.18)
            ink = (1.0, 0.56, 0.60, 1.0)
        elif self._ready:
            centre = (0.20, 0.82, 0.50, 0.18)
            ink = (0.38, 0.95, 0.66, 1.0)
        else:
            centre = (0.25, 0.50, 1.0, 0.18)
            ink = (0.70, 0.82, 1.0, 1.0)
        cr.set_source_rgba(*centre)
        cr.arc(cx, cy, 42, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(*ink)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(17)
        text = "OK" if self._ready else ("!" if self._error else "BT")
        ext = cr.text_extents(text)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                   cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)


class SetupWindow(Gtk.Window):
    def __init__(self, session: PairingSession):
        super().__init__(title="Controller Manager")
        self._session = session
        self._tick_id: Optional[int] = None
        self._animation_id: Optional[int] = None
        self._closing = False
        self._launch_requested = False
        self._device_sig = None
        self._last_phase = None
        self._fullscreen_refreshed = False
        self._was_ready = False
        self._pad_attempted = False
        self._pad = GamepadInput(
            self._pad_action_from_thread,
            logger=logging.getLogger("controller-setup"))
        self._pair_only = bool(getattr(session, "pair_only", False))
        self._snapshot = session.snapshot()

        self.set_name("setup-window")
        self.set_decorated(False)
        self.set_default_size(1280, 720)
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key)

        self._load_css()
        self._build()
        self.connect("size-allocate", self._on_size_allocate)
        self.show_all()
        self._present_fullscreen()
        self._tick_id = GLib.timeout_add(100, self._tick)
        self._animation_id = GLib.timeout_add(90, self._animation_tick)

    # -- setup ---------------------------------------------------------------

    def _load_css(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS.encode("utf-8"))
        except Exception as ex:
            print(f"controller-setup: CSS failed to load: {ex}")
            return
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _present_fullscreen(self):
        display = Gdk.Display.get_default()
        if display is not None:
            monitor = display.get_primary_monitor()
            if monitor is None and display.get_n_monitors():
                monitor = display.get_monitor(0)
            if monitor is not None:
                geometry = monitor.get_geometry()
                # Gtk.Window has no is_fullscreen() in GTK3; unfullscreen()
                # is a safe no-op when the window is not fullscreen.
                self.unfullscreen()
                self.move(geometry.x, geometry.y)
                self.resize(geometry.width, geometry.height)
                self._update_radar_size(geometry.height)
        self.fullscreen()
        self.present()

    def _on_size_allocate(self, _widget, allocation):
        self._update_radar_size(allocation.height)

    def _update_radar_size(self, height: int) -> None:
        if height >= 900:
            size = 300
        elif height >= 760:
            size = 250
        elif height >= 650:
            size = 170
        else:
            size = 150
        self._radar.set_radar_size(size)

    def _label(self, text="", css=None, xalign=0.0, wrap=False) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=xalign)
        if css:
            label.get_style_context().add_class(css)
        if wrap:
            label.set_line_wrap(True)
            label.set_max_width_chars(90)
        return label

    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class("root")
        self.add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        brand_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        brand = self._label("CONTROLLER MANAGER", "brand")
        sub = self._label("Central de prepara\u00e7\u00e3o para jogar", "brand-sub")
        brand_box.pack_start(brand, False, False, 0)
        brand_box.pack_start(sub, False, False, 0)
        header.pack_start(brand_box, False, False, 0)

        self._game_badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                   spacing=7)
        self._game_badge.get_style_context().add_class("badge")
        self._game_dot = self._label("\u25cf", "badge-label")
        self._game_text = self._label("PREPARANDO", "badge-label")
        self._game_badge.pack_start(self._game_dot, False, False, 0)
        self._game_badge.pack_start(self._game_text, False, False, 0)
        header.pack_end(self._game_badge, False, False, 0)

        self._manager_badge = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self._manager_badge.get_style_context().add_class("badge")
        self._manager_text = self._label("CM: ATIVO", "badge-label")
        self._manager_badge.pack_start(self._manager_text, False, False, 0)
        header.pack_end(self._manager_badge, False, False, 0)
        root.pack_start(header, False, False, 0)

        # The content block is vertically centred in the space the header
        # leaves, so a large TV does not show one huge gap above the footer.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_valign(Gtk.Align.CENTER)
        root.pack_start(body, True, True, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center.set_halign(Gtk.Align.CENTER)
        self._title = self._label("Preparando seus controles", "hero-title",
                                  xalign=0.5)
        self._subtitle = self._label(
            "Conecte um controle Bluetooth para come\u00e7ar", "hero-sub",
            xalign=0.5)
        center.pack_start(self._title, False, False, 0)
        center.pack_start(self._subtitle, False, False, 0)
        body.pack_start(center, False, False, 0)

        radar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        radar_box.set_halign(Gtk.Align.CENTER)
        self._radar = Radar()
        radar_box.pack_start(self._radar, False, False, 0)
        self._countdown = self._label("", "countdown", xalign=0.5)
        radar_box.pack_start(self._countdown, False, False, 0)
        body.pack_start(radar_box, False, False, 0)

        section = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._section_label = self._label("Controles encontrados",
                                          "section-label")
        section.pack_start(self._section_label, False, False, 0)
        body.pack_start(section, False, False, 0)

        self._device_scroll = Gtk.ScrolledWindow()
        self._device_scroll.set_policy(Gtk.PolicyType.NEVER,
                                       Gtk.PolicyType.AUTOMATIC)
        self._device_scroll.set_min_content_height(52)
        self._device_scroll.set_max_content_height(240)
        self._device_scroll.set_propagate_natural_height(True)
        self._device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=8)
        self._device_scroll.add(self._device_box)
        body.pack_start(self._device_scroll, False, False, 0)

        self._message = self._label("", "message", wrap=True)
        self._detail = self._label("", "detail", wrap=True)
        body.pack_start(self._message, False, False, 0)
        body.pack_start(self._detail, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.get_style_context().add_class("footer")
        self._more_button = Gtk.Button(label="Conectar mais controles")
        self._more_button.get_style_context().add_class("secondary")
        self._more_button.connect("clicked", self._on_more)
        self._retry_button = Gtk.Button(label="Tentar novamente")
        self._retry_button.get_style_context().add_class("secondary")
        self._retry_button.connect("clicked", self._on_retry)
        self._retry_button.set_no_show_all(True)
        self._retry_button.hide()
        self._close_button = Gtk.Button(label="Fechar")
        self._close_button.get_style_context().add_class("ghost")
        self._close_button.connect("clicked", lambda *_: self._quit())
        self._open_button = Gtk.Button(label="Abrir Heroic")
        self._open_button.get_style_context().add_class("primary")
        self._open_button.connect("clicked", self._on_open_heroic)
        footer.pack_start(self._more_button, False, False, 0)
        footer.pack_start(self._retry_button, False, False, 0)
        footer.pack_start(self._close_button, False, False, 0)
        self._pad_hint = self._label(
            "D-pad/Stick: navegar   \u2022   A: selecionar   \u2022   "
            "B: fechar   \u2022   Start: "
            + ("Concluir" if self._pair_only else "Heroic"),
            "pad-hint", xalign=0.5)
        self._pad_hint.set_no_show_all(True)
        self._pad_hint.hide()
        footer.pack_start(self._pad_hint, True, True, 0)
        footer.pack_end(self._open_button, False, False, 0)
        body.pack_start(footer, False, False, 0)

        self._refresh(self._snapshot)

    # -- snapshot rendering --------------------------------------------------

    def _tick(self):
        if self._closing:
            return False
        snapshot = self._session.snapshot()
        self._refresh(snapshot)
        self._snapshot = snapshot
        if snapshot.heroic_ready:
            if not self._pad_attempted:
                self._pad_attempted = True
                self._pad.start()
            if not self._was_ready:
                self._was_ready = True
                GLib.idle_add(self._focus_primary)
        else:
            self._was_ready = False
        if (self._launch_requested and snapshot.phase == PHASE_CLOSED):
            GLib.timeout_add(450, self._quit)
        return True

    def _animation_tick(self):
        if self._closing:
            return False
        snapshot = self._snapshot
        scanning = snapshot.phase in (PHASE_SCANNING, PHASE_ACTIVATING,
                                      PHASE_CHECKING, PHASE_LAUNCHING)
        ready = any(device.ready for device in snapshot.devices)
        error = snapshot.phase == PHASE_ERROR
        self._radar.set_state(scanning, ready, error)
        return True

    def _refresh(self, snapshot: UiSnapshot):
        if not self._fullscreen_refreshed and snapshot.phase in (
                PHASE_SCANNING, PHASE_TIMEOUT, PHASE_READY):
            self._fullscreen_refreshed = True
            GLib.timeout_add(150, self._reassert_fullscreen)

        self._set_game_mode(snapshot.game_mode_active, snapshot.phase)
        self._set_manager_status(snapshot)
        self._set_headline(snapshot)
        self._set_countdown(snapshot)
        self._render_devices(snapshot)

        scanning = snapshot.phase in (PHASE_SCANNING, PHASE_ACTIVATING,
                                      PHASE_CHECKING, PHASE_LAUNCHING)
        if scanning:
            self._countdown.get_style_context().add_class("countdown-active")
        else:
            self._countdown.get_style_context().remove_class(
                "countdown-active")

        if snapshot.error:
            self._message.get_style_context().add_class("message-error")
        else:
            self._message.get_style_context().remove_class("message-error")
        self._message.set_text(snapshot.message)
        detail = snapshot.detail or ""
        self._detail.set_text(detail)

        self._open_button.set_label(
            "Concluir" if self._pair_only else "Abrir Heroic")
        manager_ok = snapshot.manager_ok or self._pair_only
        self._open_button.set_sensitive(
            snapshot.heroic_ready and manager_ok
            and snapshot.phase != PHASE_LAUNCHING)
        self._more_button.set_sensitive(snapshot.can_scan_more)
        self._retry_button.set_sensitive(snapshot.can_retry)
        manager_retry = snapshot.heroic_ready and not snapshot.manager_ok
        self._retry_button.set_label(
            "Verificar novamente" if manager_retry else "Tentar novamente")
        show_retry = ((snapshot.can_retry and not snapshot.can_scan_more)
                      or manager_retry)
        if show_retry:
            self._retry_button.set_no_show_all(False)
            self._retry_button.show()
        else:
            self._retry_button.hide()
        show_pad_hint = (snapshot.heroic_ready and self._pad.available
                         and snapshot.phase != PHASE_LAUNCHING)
        if show_pad_hint:
            self._pad_hint.set_no_show_all(False)
            self._pad_hint.show()
        else:
            self._pad_hint.hide()

    def _set_manager_status(self, snapshot: UiSnapshot):
        context = self._manager_badge.get_style_context()
        context.remove_class("badge-warn")
        context.remove_class("badge-error")
        if snapshot.manager_ok:
            text = "CM: ATIVO"
            if snapshot.manager_count:
                text += f" ({snapshot.manager_count})"
        else:
            text = "CM: INDISPONIVEL"
            context.add_class("badge-error")
        self._manager_text.set_text(text)
        self._manager_badge.set_tooltip_text(
            snapshot.manager_detail or "Controller Manager")

    def _set_game_mode(self, active: bool, phase: str):
        context = self._game_badge.get_style_context()
        if active:
            context.add_class("badge-on")
            self._game_dot.set_text("\u25cf")
            self._game_text.set_text("GAME MODE ATIVO")
        else:
            context.remove_class("badge-on")
            self._game_dot.set_text("\u25cb")
            if self._pair_only:
                self._game_text.set_text("PAREAMENTO")
            elif phase in (PHASE_CHECKING, PHASE_ACTIVATING):
                self._game_text.set_text("ATIVANDO")
            elif phase == PHASE_CLOSING_HEROIC:
                self._game_text.set_text("FECHANDO HEROIC")
            elif phase == PHASE_ERROR:
                self._game_text.set_text("GAME MODE INATIVO")
            else:
                self._game_text.set_text("PREPARANDO")

    def _set_headline(self, snapshot: UiSnapshot):
        if snapshot.phase == PHASE_CHECKING:
            title = "Preparando o ambiente"
            subtitle = "Verificando o Heroic e as depend\u00eancias."
        elif snapshot.phase == PHASE_ACTIVATING:
            title = "Ativando o Game Mode"
            subtitle = "A tela e o \u00e1udio est\u00e3o sendo ajustados para a TV."
        elif snapshot.phase == PHASE_CLOSING_HEROIC:
            title = "Fechando o Heroic"
            subtitle = ("Encerrando a instancia aberta para abrir no modo "
                        "console.")
        elif snapshot.phase == PHASE_VERIFYING_MANAGER:
            title = "Verificando o Controller Manager"
            subtitle = "Garantindo que o daemon esta gerenciando o controle."
        elif snapshot.phase == PHASE_SCANNING:
            title = "Procurando controles"
            subtitle = ("Ligue o controle e mantenha-o em modo de "
                        "pareamento.")
        elif snapshot.phase == PHASE_READY:
            title = "Controle conectado"
            subtitle = ("Feche esta tela para voltar ao overlay."
                        if self._pair_only
                        else "Escolha abrir o Heroic ou adicionar outro controle.")
        elif snapshot.phase == PHASE_TIMEOUT:
            title = ("Busca conclu\u00edda" if snapshot.heroic_ready
                     else "Nenhum controle encontrado")
            subtitle = ("Voc\u00ea ainda pode abrir o Heroic com o controle "
                        "conectado." if snapshot.heroic_ready
                        else "Coloque o controle em modo de pareamento e "
                             "tente novamente.")
        elif snapshot.phase == PHASE_ERROR:
            title = "Algo precisa de aten\u00e7\u00e3o"
            subtitle = snapshot.detail or "Verifique o aviso abaixo."
        elif snapshot.phase == PHASE_LAUNCHING:
            title = "Abrindo o Heroic"
            subtitle = "Iniciando o modo console em tela cheia."
        else:
            title = "Preparando seus controles"
            subtitle = "Conecte um controle Bluetooth para come\u00e7ar."
        self._title.set_text(title)
        self._subtitle.set_text(subtitle)

    def _set_countdown(self, snapshot: UiSnapshot):
        if snapshot.phase in (PHASE_SCANNING, PHASE_ACTIVATING,
                              PHASE_CHECKING):
            self._countdown.set_text(snapshot.countdown)
        else:
            self._countdown.set_text("")

    def _render_devices(self, snapshot: UiSnapshot):
        signature = tuple(
            (d.identity, d.name, d.status, d.error) for d in snapshot.devices)
        if signature == self._device_sig:
            return
        self._device_sig = signature
        for child in self._device_box.get_children():
            self._device_box.remove(child)
        self._section_label.set_text(
            "Controles encontrados" if not snapshot.devices
            else f"Controles encontrados  ({len(snapshot.devices)})")
        if not snapshot.devices:
            empty = self._label(
                "Os controles aparecer\u00e3o aqui durante a busca.",
                "empty-note", xalign=0.5)
            self._device_box.pack_start(empty, False, False, 0)
            self._device_box.show_all()
            return
        for device in snapshot.devices:
            self._device_box.pack_start(
                self._device_card(device), False, False, 0)
        self._device_box.show_all()

    def _device_card(self, device) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        card.get_style_context().add_class("card")
        if device.status == STATUS_READY:
            card.get_style_context().add_class("card-ready")
        elif device.status == STATUS_ERROR:
            card.get_style_context().add_class("card-error")

        holder = Gtk.Box()
        holder.get_style_context().add_class("icon-holder")
        if device.status == STATUS_READY:
            holder.get_style_context().add_class("icon-holder-ready")
        elif device.status == STATUS_ERROR:
            holder.get_style_context().add_class("icon-holder-error")
        image = Gtk.Image.new_from_icon_name("input-gaming",
                                             Gtk.IconSize.DIALOG)
        image.set_pixel_size(38)
        holder.pack_start(image, False, False, 0)
        card.pack_start(holder, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = self._label(device.name, "device-name")
        detail = self._label(device.detail or device.label, "device-detail")
        text.pack_start(name, False, False, 0)
        text.pack_start(detail, False, False, 0)
        steps = self._steps_markup(device.status)
        step_label = Gtk.Label(xalign=0.0)
        step_label.set_markup(steps)
        step_label.get_style_context().add_class("step")
        text.pack_start(step_label, False, False, 0)
        if device.status == STATUS_ERROR and device.error:
            error = self._label(device.error, "device-error", wrap=True)
            error.set_max_width_chars(78)
            text.pack_start(error, False, False, 0)
        card.pack_start(text, True, True, 0)

        pill = Gtk.Box()
        pill.get_style_context().add_class("status-pill")
        if device.status == STATUS_READY:
            pill.get_style_context().add_class("status-pill-ready")
        elif device.status == STATUS_ERROR:
            pill.get_style_context().add_class("status-pill-error")
        pill_label = self._label(device.label, "status-pill-label")
        pill.pack_start(pill_label, False, False, 0)
        card.pack_end(pill, False, False, 0)
        return card

    def _steps_markup(self, status: str) -> str:
        if status == STATUS_ERROR:
            return "<span foreground='#ff8e99'>Falhou</span>"
        rank = {STATUS_PAIRING: 0, STATUS_TRUSTING: 1,
                STATUS_CONNECTING: 2, STATUS_READY: 3}.get(status)
        parts = []
        for index, (label, _step) in enumerate(STEPS):
            if rank is None:
                colour = "#65758e"
            elif index < rank:
                colour = "#52e6a1"
            elif index == rank:
                colour = "#ffc86b" if not status == STATUS_READY else "#52e6a1"
            else:
                colour = "#65758e"
            parts.append(f"<span foreground='{colour}'>{_esc(label)}</span>")
        return "  \u2022  ".join(parts)

    # -- gamepad navigation --------------------------------------------------

    def _pad_action_from_thread(self, action: str) -> None:
        GLib.idle_add(self._on_pad_action, action)

    def _focusable_buttons(self):
        buttons = (self._open_button, self._more_button,
                   self._retry_button, self._close_button)
        return [b for b in buttons
                if b.get_visible() and b.get_sensitive()]

    def _focus_primary(self):
        if self._closing:
            return False
        if self._open_button.get_visible() and self._open_button.get_sensitive():
            self._open_button.grab_focus()
        return False

    def _move_pad_focus(self, step: int) -> None:
        buttons = self._focusable_buttons()
        if not buttons:
            return
        focus = self.get_focus()
        if focus in buttons:
            index = buttons.index(focus)
        else:
            index = -1 if step > 0 else 0
        buttons[(index + step) % len(buttons)].grab_focus()

    def _on_pad_action(self, action: str):
        if self._closing or self._snapshot.phase == PHASE_LAUNCHING:
            return False
        if action == GamepadInput.ACTION_PREV:
            self._move_pad_focus(-1)
        elif action == GamepadInput.ACTION_NEXT:
            self._move_pad_focus(1)
        elif action == GamepadInput.ACTION_BACK:
            self._quit()
        elif action == GamepadInput.ACTION_OPEN:
            if self._open_button.get_sensitive():
                self._on_open_heroic()
        elif action == GamepadInput.ACTION_MORE:
            if self._more_button.get_sensitive():
                self._on_more()
        elif action == GamepadInput.ACTION_ACTIVATE:
            buttons = self._focusable_buttons()
            focus = self.get_focus()
            if focus in buttons:
                if focus is self._close_button:
                    self._quit()
                else:
                    focus.emit("clicked")
            else:
                self._focus_primary()
        return False

    # -- actions -------------------------------------------------------------

    def _reassert_fullscreen(self):
        self._present_fullscreen()
        return False

    def _on_more(self, *_):
        self._launch_requested = False
        if not self._session.scan_more():
            self._message.set_text("Aguarde a busca atual terminar.")

    def _on_retry(self, *_):
        self._launch_requested = False
        if not self._session.retry():
            self._message.set_text("Aguarde a busca atual terminar.")

    def _on_open_heroic(self, *_):
        if self._pair_only:
            self._quit()
            return
        if not self._snapshot.heroic_ready:
            return
        self._launch_requested = True
        self._open_button.set_sensitive(False)
        self._message.set_text("Abrindo o Heroic...")
        self._detail.set_text("Finalizando a busca Bluetooth.")
        threading.Thread(target=self._launch_worker, daemon=True).start()

    def _launch_worker(self):
        ok, error = self._session.open_heroic()
        GLib.idle_add(self._launch_done, ok, error)

    def _launch_done(self, ok: bool, error: str):
        if ok:
            self._message.set_text("Heroic aberto")
            self._detail.set_text("Bom jogo!")
            GLib.timeout_add(650, self._quit)
        else:
            self._launch_requested = False
            self._message.set_text("N\u00e3o foi poss\u00edvel abrir o Heroic")
            self._detail.set_text(error)
        return False

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._quit()
            return True
        return False

    def _on_delete(self, *_):
        self._quit()
        return True

    def _quit(self):
        if self._closing:
            return False
        self._closing = True
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
        if self._animation_id is not None:
            GLib.source_remove(self._animation_id)
            self._animation_id = None
        self._pad.stop()
        self._session.shutdown()
        Gtk.main_quit()
        return False


def run_ui(session: PairingSession) -> int:
    SetupWindow(session)
    Gtk.main()
    return 0
