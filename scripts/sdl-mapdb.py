#!/usr/bin/env python3
"""
SDL_GameControllerDB importer for controller-manager.

Downloads (or reads) the community gamecontrollerdb.txt and, for an ATTACHED
controller, derives a ready quirk map (source evdev code -> standard code) - the
same shape as QUIRK_BUTTON_MAP in controller-manager.py - by resolving SDL
mappings and cross-checking every candidate code against the device's actual
EV_KEY capabilities.

Why cross-checking is mandatory: SDL mappings address a LOGICAL gamepad control
to a RAW button INDEX (an SDL-ABI number), not to an evdev code. Physical
indices 0..10 correspond to the standard codes via SDL's IndexButton layout
{south=a, east=b, north=x, west=y, tl, tr, select, start, mode, l3, r3}, so a
candidate map is only emitted for codes the pad really advertises. Pads whose
DB has conflicting Linux entries (e.g. the DualSense ships two different
layouts in the DB) are SKIPPED with a warning rather than guessed.

Usage:
  scripts/sdl-mapdb.py --fetch                # download gamecontrollerdb.txt
  scripts/sdl-mapdb.py                        # auto-detect connected pads
  scripts/sdl-mapdb.py --vendor 0x045e --product 0x02e0
                                              # one pad (must be connected)
  scripts/sdl-mapdb.py --bind --ident <uniq>  # also emit a _bindings preset

ASCII-only. Exits 0 even when it finds nothing to add (that is the expected
safe outcome for a correctly-mapped pad).
"""

import argparse, json, os, sys, urllib.request

DB_URL = "https://raw.githubusercontent.com/mdqinc/SDL_GameControllerDB/master/gamecontrollerdb.txt"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "gamecontrollerdb.txt")

# SDL IndexButton: standard EV_KEY code -> raw gamepad button index.
STANDARD_INDEX = {
    0x130: 0,   # BTN_SOUTH / A
    0x131: 1,   # BTN_EAST  / B
    0x133: 2,   # BTN_NORTH / X
    0x134: 3,   # BTN_WEST  / Y
    0x136: 4,   # BTN_TL    / LB
    0x137: 5,   # BTN_TR    / RB
    0x13a: 6,   # BTN_SELECT (Back/View)
    0x13b: 7,   # BTN_START  (Menu)
    0x13c: 8,   # BTN_MODE   (Guide)
    0x13d: 9,   # BTN_THUMBL (L3)
    0x13e: 10,  # BTN_THUMBR (R3)
}
CODE_BY_INDEX = {v: k for k, v in STANDARD_INDEX.items()}

# SDL logical control -> our standard code (== output identity labels).
LOGICAL_CODE = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134,
    "leftshoulder": 0x136, "rightshoulder": 0x137,
    "back": 0x13a, "start": 0x13b, "guide": 0x13c,
    "leftstick": 0x13d, "rightstick": 0x13e,
}


def parse_guid(guid):
    """(bus, vendor, product) from an SDL GUID string (16 hex bytes)."""
    raw = bytes.fromhex(guid.lower())
    bus = raw[0]
    vendor = raw[4] | (raw[5] << 8)
    product = raw[8] | (raw[9] << 8)
    return bus, vendor, product


def load_db(path):
    entries = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                guid, rest = line.split(",", 1)
            except ValueError:
                continue
            try:
                key = parse_guid(guid)
            except ValueError:
                continue            # malformed / non-hex GUID line
            fields = dict(p.split(":", 1) for p in rest.split(",")
                          if ":" in p)
            if fields.get("platform") != "Linux":
                continue
            entries.setdefault(key, []).append(fields)
    return entries


def device_caps(path):
    """Set of EV_KEY codes a connected /dev/input/eventX advertises."""
    import evdev
    from evdev import ecodes as e
    dev = evdev.InputDevice(path)
    try:
        return {c for c in dev.capabilities().get(e.EV_KEY, [])
                if isinstance(c, int)}
    finally:
        dev.close()


def resolve_layout(fields):
    """SDL mapping -> {logical control: evdev source code}, best effort.
    Returns None when the layout involves a raw index SDL does not assign to a
    standard code (index > 10)."""
    out = {}
    for logical, code in LOGICAL_CODE.items():
        spec = fields.get(logical)
        if spec is None:
            continue
        if not spec.startswith("b"):
            continue                      # axis/hat binds: not our concern
        idx = int(spec[1:])
        src = CODE_BY_INDEX.get(idx)
        if src is None:
            return None                   # extra-button indices are un-derivable
        out[logical] = src
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true",
                    help="download gamecontrollerdb.txt next to this script")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--vendor", type=lambda s: int(s, 0))
    ap.add_argument("--product", type=lambda s: int(s, 0))
    ap.add_argument("--bus", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--ident", help="controller ident shown by the daemon")
    ap.add_argument("--bind", action="store_true",
                    help="emit a _bindings preset for the pad (device code -> "
                         "output code under the Xbox identity)")
    args = ap.parse_args()

    if args.fetch:
        print(f"fetching {DB_URL}")
        urllib.request.urlretrieve(DB_URL, args.db)
        print(f"saved {args.db}")
        return

    if not os.path.exists(args.db):
        print(f"SDL database not found: {args.db}  (run with --fetch)",
              file=sys.stderr)
        sys.exit(1)

    db = load_db(args.db)

    import evdev
    targets = []
    for d in evdev.list_devices():
        dev = evdev.InputDevice(d)
        try:
            name = dev.name
            vendor, product, bustype = dev.info.vendor, dev.info.product, \
                dev.info.bustype
        finally:
            dev.close()
        if args.vendor is not None and vendor != args.vendor:
            continue
        if args.product is not None and product != args.product:
            continue
        if args.bus not in (None, bustype):
            continue
        targets.append({"path": d, "name": name,
                        "bus": bus_of(bustype), "vendor": vendor,
                        "product": product})
    if not targets:
        print("no connected pad matches; nothing to do", file=sys.stderr)
        sys.exit(1)

    found_any = False
    for t in targets:
        key = (t["bus"], t["vendor"], t["product"])
        fields_list = db.get(key, [])
        print(f"\n== {t['name']}  {t['path']}  "
              f"(bus={t['bus']:#04x} vendor={t['vendor']:#06x} "
              f"product={t['product']:#06x})")
        print(f"   SDL Linux entries matched by GUID: {len(fields_list)}")
        if not fields_list:
            print("   no entry in the SDL database for this pad")
            continue
        caps = device_caps(t["path"])

        layouts = []
        for fields in fields_list:
            layout = resolve_layout(fields)
            if layout is not None:
                layouts.append((fields, layout))
        if len(layouts) != 1:
            print("   SKIP: conflicting or un-derivable SDL layouts for this "
                  "pad (won't guess codes; the kernel usually already reports "
                  "a consonant layout)")
            continue

        _, layout = layouts[0]
        quirk = {}
        for logical, src in layout.items():
            dst = LOGICAL_CODE[logical]
            if src == dst:
                continue
            if src not in caps:
                print(f"   note: {logical} resolves to {src:#06x} which the "
                      f"pad does not advertise - skipped")
                continue
            quirk[src] = dst
        found_any = found_any or bool(quirk)

        if quirk:
            key_src = "(0x%04x, 0x%04x)" % (t["vendor"], t["product"])
            print("   ready quirk table for controller-manager.py "
                  f"QUIRK_BUTTON_MAP[{key_src}]:")
            for src, dst in sorted(quirk.items()):
                print(f"       e.BTN_X: {src:#06x} -> e.BTN_Y: {dst:#06x}")
            if args.bind and args.ident:
                out = {str(src): dst for src, dst in quirk.items()}
                print(json.dumps({"_bindings": {args.ident: out}}, indent=2))
        else:
            print("   layout matches standard gamepad order - no quirk needed")

        if not found_any:
            pass

    print("\nresult: %d quirk map(s) generated" % int(found_any))


def bus_of(bustype):
    from evdev import ecodes as e
    return {e.BUS_USB: 0x03, e.BUS_BLUETOOTH: 0x05}.get(bustype, bustype)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)