"""The XApp.StatusIcon tray fallback (used where no StatusNotifierItem host
exists - Cinnamon/Linux Mint): builds a Gtk menu from the SAME semantic model
as the SNI dbusmenu, so the two hosts expose identical choices, and routes
clicks to the same actions (set_mode / renumber / open_gui / quit).

The daemon deliberately never runs (no bus, no devices): we only import the
module and drive the Gtk menu builder. The Gtk bring-up is skipped on purpose
by the test, so it runs on any machine with the daemon's deps and a Gtk XApp
typelib; without them it SKIPs (exit 77), mirroring test_menu_ids.py."""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "controller-manager.py")

try:
    spec = importlib.util.spec_from_file_location("ctrlmgr_xapp", MODULE)
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
except ModuleNotFoundError as ex:
    print(f"SKIP: runtime dependency missing ({ex.name}) - needs evdev/dbus/gi")
    sys.exit(77)

if not cm._XAPP_AVAILABLE:
    print("SKIP: Gtk/XApp typelib unavailable - XApp fallback disabled")
    sys.exit(77)

from gi.repository import GLib

def pump_idle():
    """Run pending GLib idles exactly once per pass until quiet - the radio
    click handler defers to an idle so the group state has settled."""
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

class Pad:
    def __init__(self, ident, name="DualSense", family="ps5", mode="ps5-native",
                 player=None):
        self.ident = ident; self.name = name
        self.family = family; self.mode = mode; self.player = player

class FakeMgr:
    def __init__(self):
        self.instances = []
        self.set_mode_calls = []
        self.renumber_calls = 0
        self.open_gui_calls = []
    def get_instances(self):
        return list(self.instances)
    def set_mode(self, ident, mode):
        self.set_mode_calls.append((ident, mode))
    def renumber(self):
        self.renumber_calls += 1
    def open_gui(self, ident):
        self.open_gui_calls.append(ident)

def build(mgr):
    return cm._xapp_build_menu(mgr, on_quit=lambda: None)

def labels_of(menu):
    return [c.get_label() for c in menu.get_children()]

def items_of(menu):
    return [c for c in menu.get_children() if isinstance(c, cm.Gtk.MenuItem)]

def find(menu, label):
    return next((c for c in menu.get_children()
                 if c.get_label() == label), None)

QUIT_LABEL = "Quit"
XBOX       = cm.MODE_LABELS["ps5-xbox"]   # e.g. 'Output as Xbox'
NATIVE     = cm.MODE_LABELS["ps5-native"]

# ── empty vs. connected state ────────────────────────────────────────────────
print("Scenario A: empty vs connected controller menu")
mgr  = FakeMgr()
menu = build(mgr)
check("No controller connected" in labels_of(menu),
      "empty state shows a no-controller info line")
check(QUIT_LABEL in labels_of(menu), "Quit is always present")
info_items = [c for c in menu.get_children()
              if c.get_label() == "No controller connected"]
check(info_items and not info_items[0].get_sensitive(),
      "info line is non-interactive")

mgr.instances = [Pad("ac:36:1b:70:70:e8")]
menu = build(mgr)
check("DualSense" in labels_of(menu), "controller header listed")
check(NATIVE in labels_of(menu) and XBOX in labels_of(menu),
      "both ps5 modes offered as radios")
nat = find(menu, NATIVE); xb  = find(menu, XBOX)
check(nat.get_active() and not xb.get_active(),
      "package default (native) is the checked radio")

# ── radio click routes to set_mode; a lone-mode family is a plain line ───────
print("Scenario B: radio click sets the mode; Xbox shows a static line")
xb.set_active(True)
pump_idle()
check(mgr.set_mode_calls == [("ac:36:1b:70:70:e8", "ps5-xbox")],
      "click on the unchecked radio reaches set_mode")

mgr_x = FakeMgr()
mgr_x.instances = [Pad("xbox-1", name="Xbox Series X", family="xbox",
                       mode="xbox-native")]
menu_x = build(mgr_x)
check(cm.MODE_LABELS["xbox-native"] in labels_of(menu_x),
      "single-mode family shows its mode as a static line")
radios = [c for c in menu_x.get_children() if isinstance(c, cm.Gtk.RadioMenuItem)]
check(not radios, "single-mode family has no clickable radio")

# ── renumber entry only while a numbering gap exists ────────────────────────
print("Scenario C: Renumber players entry and click")
mgr.instances = [Pad("A", player=1), Pad("B", player=3)]
menu = build(mgr)
ren = find(menu, "Renumber players")
check(ren is not None, "a numbering gap surfaces the Renumber item")
ren.emit("activate")
check(mgr.renumber_calls == 1, "Renumber click calls mgr.renumber")

mgr.instances = [Pad("A", player=1), Pad("B", player=2)]
check(find(build(mgr), "Renumber players") is None,
      "contiguous numbering shows no Renumber entry")

# ── action + Quit routing ────────────────────────────────────────────────────
print("Scenario D: Remap buttons... and Quit routing")
mgr.instances = [Pad("ac:36:1b:70:70:e8")]
guis = []
mgr.open_gui = guis.append
menu = build(mgr)
find(menu, "Remap buttons...").emit("activate")
check(guis == ["ac:36:1b:70:70:e8"],
      "Remap click opens the GUI pre-selecting this pad")

quit_calls = []
cm._xapp_build_menu  # noqa: keep name importable for clarity below
qmenu = cm._xapp_build_menu(mgr, on_quit=lambda: quit_calls.append(1))
find(qmenu, QUIT_LABEL).emit("activate")
check(len(quit_calls) == 1, "Quit click reaches the on_quit callback")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)