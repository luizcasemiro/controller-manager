# Decision: XApp.StatusIcon tray fallback for Cinnamon / Linux Mint

## Context

The daemon's tray is a `StatusNotifierItem` registered with the
`org.kde.StatusNotifierWatcher`. Cinnamon - and therefore Linux Mint - never runs that
watcher: its `systray` applet is XEmbed-only, and the `xapp-status` applet displays
`org.x.StatusIcon.*` services, not SNI items. On Mint the tray was simply invisible: the
daemon owned `org.kde.StatusNotifierItem-ctrlmgr-1` on the bus, registered with - and was
never registered to - a watcher that does not exist here.

(Mint's `xapp-status` applet hosts `org.x.StatusIcon` services, the XApp tray protocol
that replaces `Gtk.StatusIcon`. A C++ application normally publishes one through
`libxapp`'s `XApp.StatusIcon`; the panel's `StatusIconMonitor` discovers the
well-known name, shows an icon from its properties, and forwards button presses to the
owner, which pops its own menu widget.)

## Decision

1. **Serve the tray twice, second session only the once-visible.** The SNI item stays the
   primary tray on desktops that host it (KDE, GNOME+appindicator). When a
   `StatusNotifierWatcher` proves absent - Cinnamon/Mint - the daemon additionally serves
   the same menu as an `XApp.StatusIcon`. The XApp icon is shown exactly while no SNI
   watcher owns its well-known name and hidden the moment one appears, so a desktop
   supporting both never sees two icons.
2. **One menu model, two renderers.** The menu is defined as plain semantic tuples and
   cached/rendered per host: `com.canonical.dbusmenu` on the SNI path, a freshly-built
   `Gtk.Menu` on the XApp path. Both build from the same `_semantic_menu_items()` so the
   choices, radio grouping and click targets are identical.
3. **Rebuild the Gtk menu per click, never cache it.** The controller list can change
   under a running desktop, and the XApp menu is a process-local widget popped by GTK on
   button release - there is no host round-trip to refresh. Each `button-press-event`
   rebuilds both the primary and secondary (left/right click) menus from the current
   state; GTK owns one menu per button.
4. **GTK is an optional, guarded import.** The SNI path never links a toolkit; the XApp
   fallback imports GTK+3 and `XApp` from `gi` only when the typelibs exist
   (`_XAPP_AVAILABLE`), and inits `Gdk` only then. A machine without either falls back to
   SNI-only exactly as before.
5. **A radio click applies the mode from a settled group state.** Changing a
   `Gtk.RadioMenuItem` group fires `toggled` on the deselected member too, while
   `get_active()` still reports the stale value; applying the mode inside that handler
   would set the *previous* mode once more. The check runs in an idle, wired after the
   build-time activation, when exactly one radio is active.

## Consequences

- The tray works on Cinnamon / Linux Mint out of the box, using the panel's native
  `xapp-status` applet - no extra tray host, no extension.
- The daemon's "never links a GUI toolkit" property relaxes to "links GTK only for the
  Cinnamon/Mint fallback, and only while it is in use"; the SNI path stays toolkit-free.
- Because the XApp name registers only while a monitor applet is present, headless and
  KDE/Plasma sessions create the icon object but publish nothing.
- Two extra runtime imports (`Gtk`/`XApp` typelibs) are tolerated when absent; the
  fallback simply does not appear.

Regression-tested in `tests/test_xapp_fallback.py` (menu structure, radio click routing,
renumber/remap/quit actions).