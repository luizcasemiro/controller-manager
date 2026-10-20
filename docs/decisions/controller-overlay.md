# Decision: Controller Overlay and Chord

## Context

Once a game is running, reaching the pairing dashboard or the Game Mode off command with
a keyboard breaks the console experience. The controller already carries the natural
inputs: both analog triggers and the Home/PS/Guide button.

The chord must work for a native controller and for a pad whose events are grabbed by the
remapper, without stealing input from the game while no chord is pressed.

## Decision

- **Daemon-owned chord monitor.** `controller_setup.combo.ComboMonitor` reads every evdev
  gamepad without `EVIOCGRAB`. When the remapper has the physical pad grabbed, the
  virtual output carries the same trigger and Guide capabilities, so the monitor reads
  that instead. The daemon launches the overlay once per chord.
- **LT + RT, then Home.** Both triggers must be at least 70% and held for 0.5 s; Home is
  the deliberate confirmation and fires the chord when pressed. The state machine fires
  once, requires a full trigger release, and has no auto-repeat.
- **Standalone overlay process.** `controller-overlay.py` is a GTK3 fullscreen window with
  always-on-top hints and a single-instance flock. It is not part of the daemon, so a
  crashed overlay cannot take the tray or remapper down.
- **Reuse the pairing UI.** "Parear novo controle" runs `controller-setup.py --pair-only`
  with the overlay hidden. Pair-only skips Heroic and Game Mode transitions. When the
  pairing UI exits, the overlay returns.
- **Daemon health is mandatory before Heroic.** The pairing session and overlay check the
  user service plus the `ListControllers` D-Bus API. An inactive or unreachable service is
  restarted once. A control that is not listed yet gets a bounded grace period (the
  daemon polls every two seconds). Heroic remains blocked until the daemon is healthy,
  and a short post-launch check logs whether it stayed up.
- **One clear exit path.** "Sair do Game Mode" requires confirmation, runs the existing
  Game Mode off command and closes the overlay. Closing the overlay itself never changes
  monitor or audio.
- **The overlay never kills game runtimes.** Only the Heroic launcher executable and
  `flatpak run com.heroicgameslauncher.hgl` are force-closed; umu/game processes under the
  Heroic data directory are ignored.

## Consequences

- The chord cannot be seen when another process owns an exclusive evdev grab and no
  virtual output exists. That is a kernel-level limitation; the normal remap path is
  covered through its virtual pad.
- The chord is not suppressed from the game. It requires a deliberate three-button hold
  to reduce accidental activation.
- A failed health check is visible in the pairing dashboard and overlay instead of
  silently launching a game without the daemon managing the controller.
- The overlay has its own process, lock and log, but shares the same CSS language and
  gamepad navigation as the pairing dashboard.
