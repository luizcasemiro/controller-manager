# Decision: Per-Button Remapping GUI & Bindings

## Context

Remapping is offered at the whole-pad level: pick a mode per controller and the program
re-wires the whole button face to the target identity. Users need finer control - swap two
buttons, move a trigger, or restore a single button to a custom output without changing
the controller's mode. Whatever mechanism is added must stay GUI-free in the daemon (the
daemon is deliberately headless behind its D-Bus API) and must not bypass the mode system.

## Decision

A separate GTK3 application (`controller-gui.py`) edits per-controller button bindings
through a new D-Bus service exposed by the daemon. Binding is captured by physically
pressing the button to record (press-to-record).

- **Per-controller, not global.** Bindings are keyed by the same stable per-device
  identity as modes, so two identical pads each keep their own remap, and the set follows
  the pad across reconnects.
- **Per-device input codes, per-identity output codes.** A binding maps the *device*
  button code the pad actually reports to the *output* code of the selected target
  identity. It therefore composes with whichever mode the controller is in, and
  `_bindings` does not need a mode column of its own.
- **Empty means off.** A controller with no bindings entry uses the mode's plain
  quirk remap; an empty entry means the program mapping is disabled for that pad.
- **Launched from the tray** item ("Remap buttons..." per controller); the daemon keeps a
  single GUI subprocess per session and refuses to start a second one. The daemon never
  blocks on the GUI: it only spawns the process and answers its D-Bus calls.
- **Capture coordination.** While capturing, the daemon releases the pad's remap grab
  (the gate keeps the node hidden) and pauses re-asserts of the mode for that pad, so a
  button press is read raw from the real device instead of the virtual one. The capture
  times out after a few seconds or is cancelled when the GUI closes the dialog.
- **Persistence.** Written by the daemon into `~/.config/controller-modes.json` under the
  reserved `_bindings` key - the same file as modes, so the two stay in one source of
  truth (single lock, single save path).

## Implications

### The user layer must win outright

Composition is `user -> quirk`: an explicit user binding on a device code
completely overrides the program remap for that code; every other button keeps the
program's mapping. Bindings are deliberately *not* positional: they
follow the device code, so they survive mode switches and identity changes.

### The value "may be anything", the label "may fall back"

`GetTargetButtons` advertises the output identity's button codes, and the GUI maps each to
a label. Codes without a label still accept a binding; the picture shows the code's hex
value. `-1` clears a binding. A per-controller entry is dropped from the config entirely
once it holds no bindings, keeping the file tidy.

### Capture cannot run while the pad is grabbed

The remapper's grab would swallow and forward the press, so the GUI cannot record at any
other time. The daemon therefore: (a) refuses a second concurrent capture, (b) refuses
capture on a controller it does not know, and (c) guards the reconnect/reconcile logic so
a mid-capture pad is not re-grabbed - a re-grab would eat the very press the user is about
to record.

## Alternatives considered

- **Bindings inside the tray menu** - a radio/checkbox forest for every button of every
  pad; unusable and unbounded.
- **In-daemon GUI** - rejected: the daemon must stay headless; a toolkit (GTK) plus a
  window in the service adds a heavy dependency and a crash surface for a menu-only
  process.
- **Config-file hand editing** - error-prone (raw ecodes), no discoverability of codes,
  no press-to-record, no live apply.
- **A `SetBinding`-only D-Bus API without capture support** - the GUI would have to probe
  the pad itself, racing the daemon's grab. Letting the daemon own the release/capture
  cycle keeps the virtual device and the real device consistent.