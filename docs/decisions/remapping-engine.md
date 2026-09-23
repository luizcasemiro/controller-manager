# Decision: Remapping Engine - evdev Grab + uinput

## Context

To make a controller appear as a different type, the system must intercept the physical
device's events and re-emit them under a new identity, before applications enumerate
input devices. This has to work for any launcher and any application, without per-title
configuration.

## Decision

Use the Linux input subsystem directly:

- **`EVIOCGRAB`** on the source `evdev` node for exclusive access, so no other consumer
  sees the physical device while it is remapped.
- A **`uinput`** virtual device created with the target identity (vendor/product/version)
  and the source's capabilities, into which the source's events are forwarded.

This operates below the application layer, so the result is uniform across launchers and
needs no game- or app-specific settings.

## Consequences and the pitfalls that shaped the design

### The virtual device is created before the grab

`uinput` creation happens first, then the grab. If the grab fails (another process holds
the device), the virtual device is explicitly closed. Skipping that cleanup would leave an
orphaned virtual controller with no remapper behind it.

### Virtual devices must be excluded from detection

The detection scan filters out the daemon's own virtual devices by name and by path.
Without this, a freshly created virtual pad would be detected as a new physical controller
and remapped again - an endless loop.

### Capabilities are advertised in translated form

Some controllers expose a non-standard evdev button layout. Forwarding raw codes would
make the consuming side map the *target* identity against the *source's* layout, landing
buttons in the wrong place. The remapper holds a per-source quirk table and advertises the
translated (standard) codes on the virtual device, then translates each event as it is
forwarded.

### Target-identity translation was tried and removed

The kernel aliases positional and lettered button constants (`BTN_X == BTN_NORTH ==
0x133`, `BTN_Y == BTN_WEST == 0x134`) and driver families assign them inverted:
`hid-playstation` emits Triangle (top) as `0x133` and Square (left) as `0x134`, while
`xpad` emits X (left) as `0x133` and Y (top) as `0x134`. A first engine version therefore
carried a per-target table swapping `0x133 <-> 0x134` when the virtual device was an
"X-Box 360 pad" (quirk first: source -> standard, then target: standard -> target).

Field use showed the swap was counterproductive: whether it is needed depends on how the
consuming app resolves codes, and the apps actually used read the codes natively
(index/canonical order), so the swap inverted their X/Triangle and Y/Square. The target
layer was removed; the remapper now applies only the per-source quirk table and the
per-controller user bindings, and face buttons pass through identity-agnostically. A
per-controller user binding remains the tool for any title that needs a different
arrangement.

### The event loop must be interruptible while idle

`evdev`'s blocking read waits for the next event. The first implementation tried to stop a
remapper by setting a flag and **closing the source fd from another thread**, expecting
the blocked read to wake. On Linux this is unreliable: closing a file descriptor does not
deterministically interrupt a `read()` blocked on it in another thread. When the
controller was idle (no events arriving), the remapper thread stayed blocked, kept its
`uinput` device open, and leaked an orphaned virtual controller - intermittently,
depending on whether a stray event happened to arrive.

The loop now uses `selectors` over **two** descriptors: the source fd and a **self-pipe**.
`stop()` writes one byte to the pipe; `select()` returns immediately and the loop exits and
cleans up (release grab, close source, close virtual device, close pipe). This wakes the
loop deterministically regardless of controller activity.

## Alternatives considered

- **Per-application environment toggles** - fragile, must be repeated for every launcher,
  and several do not reliably propagate environment variables to the application.
- **Userspace remappers that translate to keyboard/mouse** - wrong layer; they fight with
  controller-aware applications and produce mixed input.
