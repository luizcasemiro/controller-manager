# Runbook: Visual Pairing, Overlay and Heroic Console

Prepare the TV/audio environment, pair Bluetooth controllers from a fullscreen dashboard
and launch Heroic in console mode. This procedure is optional and independent from the
tray daemon.

## Preconditions

- A working Controller Manager installation (`controller-manager.service` is active).
- BlueZ running and at least one Bluetooth adapter powered on.
- `python3-dbus`, `python3-gi`/GTK3 and `python3-evdev` installed.
- A Game Mode script that accepts `1` to enable the TV/audio profile. The default is
  `~/Documentos/dev/gamemode/gamemode.sh`; override it in
  `~/.config/controller-setup.json` or with `GAMEMODE_SCRIPT`.
- Flatpak and Heroic (`com.heroicgameslauncher.hgl`) installed.
- KDE Connect with the Run Command plugin enabled and the phone trusted.
- The controller is powered on and, for a first pairing, in pairing mode.

## Steps

1. Install the user-space files:

   ```bash
   cd controller-manager
   ./install.sh
   ```

   This copies `controller-setup.py` and the `controller_setup/` package to
   `~/.local/bin`.

2. Review the optional configuration:

   ```bash
   cat ~/.config/controller-setup.json
   ```

   Example:

   ```json
   {
     "gamemode_script": "~/Documentos/dev/gamemode/gamemode.sh",
     "flatpak_app": "com.heroicgameslauncher.hgl",
     "heroic_args": ["--console", "--fullscreen"],
     "heroic_command": []
   }
   ```

   `heroic_command`, when non-empty, replaces the Flatpak launch entirely:
   `["/opt/heroic/heroic", "--console", "--fullscreen"]`.

3. Add the KDE Connect command. Either run the helper:

   ```bash
   python3 scripts/kdeconnect-install-command.py
   ```

   The helper edits only the Run Command `commands=` value, creates a timestamped
   `.bak-*` backup and refuses to continue if the edit cannot be verified. Restart KDE
   Connect afterwards.

   Or add it manually in the KDE Connect app:

   ```text
   Name: Preparar controles e abrir Heroic
   Command: /home/<user>/.local/bin/controller-setup.py
   ```

4. On the phone, run **Preparar controles e abrir Heroic**.

5. The dashboard first closes an already-running Heroic (`SIGTERM`, then `SIGKILL` if
   needed), activates Game Mode, searches for up to 60 seconds and stops at the first
   newly connected controller. Use **Conectar mais controles** for another search.

   Once a controller is ready the dashboard is fully usable from the couch:

   | Input | Action |
   |---|---|
   | D-pad up/left, left stick up | previous action |
   | D-pad down/right, left stick down | next action |
   | A / Cross | select the focused action |
   | B / Circle | close the dashboard (keeps Game Mode active) |
   | Start / Menu | open Heroic immediately |
   | Select / View | scan for another controller |

   If a controller is remapped by the daemon, the UI reads its virtual evdev output, so
   navigation keeps working.

6. Press **Abrir Heroic** (or Start) to start:

   ```bash
   flatpak run com.heroicgameslauncher.hgl --console --fullscreen
   ```

The KDE Connect command returns immediately; the UI runs in a detached process. For
debugging from a terminal, force it to stay in the foreground:

```bash
~/.local/bin/controller-setup.py --foreground
```

### Controller overlay

After the controller is connected, hold **LT + RT** above 70% for 0.5 s, then press
**Home/PS/Guide**. The daemon launches the overlay (it reads the remapper's virtual
output for remapped pads).

Menu actions:

- **Parear novo controle** - opens this pairing dashboard in pair-only mode, without
  changing Game Mode or closing Heroic. The overlay returns when the pairing window
  closes.
- **Abrir Heroic** - verifies the Controller Manager, activates Game Mode, force-closes
  any running Heroic and launches `--console --fullscreen`.
- **Sair do Game Mode** - asks for confirmation and runs `gamemode.sh 0`.
- **Fechar overlay** - closes the menu without restoring monitor/audio.

Both the pairing UI and the overlay show `Controller Manager: ATIVO` or
`INDISPONIVEL`. When the daemon is down, the flow restarts
`controller-manager.service` once and waits for the controller to appear in
`ListControllers`; Heroic stays blocked until that succeeds. A short post-launch
check confirms that the daemon stayed up after Heroic started.

Navigation: D-pad/left stick moves, A selects, B closes/cancels, Start opens Heroic,
Select opens pairing.

Closing the dashboard without pressing the button leaves Game Mode active. Use the
existing Game Mode off command to restore the desktop.

## Verification

```bash
# Deployment
ls -l ~/.local/bin/controller-setup.py
ls -l ~/.local/bin/controller-overlay.py
ls -l ~/.local/bin/controller_setup/

# Daemon and D-Bus
systemctl --user is-active controller-manager.service
journalctl --user -u controller-manager.service -n 40

# Session log (created on first use)
tail -n 40 ~/.local/state/controller-setup/session.log

# Heroic output
tail -n 40 ~/.local/state/controller-setup/heroic.log

# KDE Connect command entry
cat ~/.config/kdeconnect/*/kdeconnect_runcommand/config
```

Expected dashboard states:

- `ATIVANDO` while the Game Mode script runs;
- `Controller Manager: ATIVO` after the service/D-Bus check;
- `Procurando controles` with a countdown during discovery;
- `Pareando -> Confiando -> Conectando -> Conectado e pronto` per controller;
- **Abrir Heroic** enabled after the first ready controller and a healthy daemon;
- discovery stops immediately after each new controller connects.

The overlay should show `Game Mode: ATIVO`, `Controller Manager: ATIVO` and the managed
controller count when opened with **LT + RT + Home**.

The controller should then appear in the Controller Manager tray within a few seconds.

## Failure modes

| Symptom | Check |
|---|---|
| Dashboard does not open | `DISPLAY`/session environment and `python3-gi`; inspect the KDE Connect command output and `session.log` |
| `Script de Game Mode nao encontrado` | Correct `gamemode_script` in `~/.config/controller-setup.json` or `GAMEMODE_SCRIPT` |
| `Game Mode falhou` | Run the script manually and inspect its error (HDMI/TV connected, `xrandr`, `wpctl`) |
| `Heroic nao encontrado no Flatpak` | `flatpak info com.heroicgameslauncher.hgl` |
| `Heroic nao encerrou` | Check `ps -ef | grep heroic`, permissions and `session.log`; the session escalates from `SIGTERM` to `SIGKILL` |
| `Controller Manager indisponivel` | Check `systemctl --user status controller-manager.service`, journal and the D-Bus name; the flow already retries the restart once |
| Overlay does not open with the chord | Confirm the controller exposes both triggers and `BTN_MODE`; a process holding an exclusive evdev grab without a virtual output can hide the chord |
| Overlay opens but D-pad does nothing | Check `python3-evdev` access and whether another tool holds the virtual pad |
| `Parear novo controle` returns immediately | Check `session.log`; another `controller-setup` instance may hold its single-instance lock |
| Controller appears but is not paired | Put it in pairing mode; some models require a physical confirm button |
| Keyboard/mouse never appear | Expected: the filter accepts only gamepads |
| D-pad does not move the focus | Check `evdev` access and whether another tool grabbed the pad; the virtual remap output is also read automatically |
| Search finds no controller | Check `bluetoothctl devices`, adapter power, range and pairing mode |
| KDE Connect still shows old command | Restart KDE Connect/kdeconnectd after editing its config |
| Command runs twice | Only one `controller-setup` instance is allowed; a second invocation exits |

## Rollback

```bash
# Remove the user-space command and package
rm -f ~/.local/bin/controller-setup.py ~/.local/bin/controller-overlay.py
rm -rf ~/.local/bin/controller_setup

# Remove the KDE Connect command through the app, or restore the newest backup:
ls ~/.config/kdeconnect/*/kdeconnect_runcommand/config.bak-*
# cp <backup> <config>; then restart KDE Connect

# Optional local state
rm -rf ~/.local/state/controller-setup
```

The Game Mode script and Heroic remain untouched. The tray daemon and its configuration
are not changed by this feature.
