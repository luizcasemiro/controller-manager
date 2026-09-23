# Decision: SDL Game Controller DB Import

## Context

The per-button binding GUI started empty per controller, and users looking for
"ready-made binds" expect a library of layouts to apply instead of recording
every button by hand. Sunshine/Moonlight (the popular streaming duo) were named
as a candidate source, but neither ships a button-remap preset library: Sunshine
only selects the virtual gamepad *identity* (`gamepad = ds5/xone/...`), and
Moonlight relies on the community **SDL_GameControllerDB**
(`gamecontrollerdb.txt`, the file SDL and Moonlight load at startup).

The SDL database is the real "library" - but it is a *layout report*, not a set
of user remaps. It answers "which physical button is which gamepad control" per
device GUID, in the format:

```
...,PS5 Controller,a:b1,b:b2,back:b8,...,x:b0,y:b3,platform:Linux,
```

## What is safe to import

`controller-manager.QUIRK_BUTTON_MAP` has exactly the same job as the SDL
database: for a source pad with a non-standard kernel layout, map the source's
evdev code to the standard gamepad code. The database can feed that table for
pads the daemon does not know yet.

`scripts/sdl-mapdb.py` resolves the mapping the safe way:

- **GUID match.** Parses bus/vendor/product out of the 16-byte SDL GUID and
  filters `platform:Linux` entries.
- **SDL-ABI -> evdev.** A mapping value `bN` is a RAW button index. Indices 0..10
  correspond to fixed standard codes (SDL's IndexButton layout: south=A, east=B,
  north=X, west=Y, tl, tr, select, start, guide, l3, r3); any other index is
  un-derivable and the layout is skipped.
- **Verification against the physical pad.** The pad must be connected. A
  candidate code is only emitted if the device actually advertises it, so the
  tool never invents a code the pad does not have.
- **Ambiguity = skip.** If a (bus, vendor, product) has several conflicting
  Linux entries - which happens for real devices such as the DualSense, which
  ships under two different face-button layouts in the database - the importer
  refuses to guess and reports "SKIP". Guessing there would ship a wrong quirk.

## Result for today's supported pads

- **DualSense (054c:09cc):** no quirk needed - the kernel already reports a
  standard layout; the database's conflicting entries are skipped. Nothing to
  import, which is the intended safe outcome.
- **Xbox (0x02e0 quirks etc.):** the database largely describes the default
  layout; the daemon's stable per-firmware quirks remain authoritative.

The importer is therefore a **maintenance tool for future pads**, not a source
of user-facing remap presets. User remaps stay per-controller and per-person in
`_bindings`; there is no meaningful "community remap library" because remaps are
literally personal preferences (which is why the GUI records them by hand).

## Alternatives considered

- **Vendoring a preset pack.** There is none to vendor: neither Sunshine nor
  Moonlight publishes remap presets, and the SDL database is a layout index, not
  a remap set.
- **Applying SDL mappings unconditionally.** Rejected: indices depend on the SDL
  revision and the kernel driver and conflict between database entries for the
  same physical pad; an unverified import would silently ship wrong buttons.
- **Skipping the database entirely.** Keeps the supported families working, but
  leaves no path to onboard a future quirky pad. The verified importer is that
  path.