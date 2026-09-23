#!/usr/bin/env bash
# Deploy controller-manager from this repo into the live system locations.
#
# User-space files need no privileges; the hidraw gate (root-owned helper) and
# its sudoers rule require sudo. Run from anywhere:  ./install.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

echo "==> User-space (controller-manager + binding GUI + service)"
install -D -m 0755 controller-manager.py       "$HOME/.local/bin/controller-manager.py"
install -D -m 0755 controller-gui.py           "$HOME/.local/bin/controller-gui.py"
install -D -m 0644 controller-manager.service  "$HOME/.config/systemd/user/controller-manager.service"

echo "==> Root-space (hidraw gate + led helper + udev rule + sudoers) - sudo required"
sudo install -m 0755 -o root -g root controller-hidraw-gate /usr/local/bin/controller-hidraw-gate
sudo install -m 0755 -o root -g root controller-led         /usr/local/bin/controller-led

# Gate udev rule: gated hidraw nodes must be born inaccessible (MODE 0000,
# no uaccess ACL), otherwise Steam re-opens them the instant the gate's
# driver rebind recreates them. The 72- prefix must outrank the 71-*
# controller rules from steam-devices (they re-add uaccess for Sony pads).
# Remove the superseded 71- name from earlier installs, then reload rules
# so everything applies without a reboot.
sudo install -m 0644 -o root -g root 72-controller-manager.rules /etc/udev/rules.d/72-controller-manager.rules
sudo rm -f /etc/udev/rules.d/71-controller-manager.rules
sudo udevadm control --reload
# Replay hidraw events so the new rule is evaluated for already-connected pads
# too (no markers exist yet, so nodes simply stay open - this only makes the
# rule effective now instead of at the next reconnect).
sudo udevadm trigger --subsystem-match=hidraw

# HID-BPF report-descriptor fixup for the Bluetooth Xbox pad reporting product
# 0x02FD. Its firmware advertises a truncated (unbalanced) HID descriptor that
# no driver can parse, so the pad produces no input node at all and is invisible
# to everything downstream (see docs/decisions/xbox-02fd-hid-bpf.md). The fixup
# appends the two missing END_COLLECTION bytes so the descriptor parses.
#
# This needs a build toolchain (clang/bpftool/libbpf-devel), udev-hid-bpf, and a
# BTF-enabled kernel. It only matters if you own that specific pad, so a missing
# prerequisite is a skip-with-warning, not a failure - DualSense-only setups are
# unaffected.
if command -v udev-hid-bpf >/dev/null 2>&1; then
    if bpf_o="$(./hid-bpf/build.sh)"; then
        echo "==> HID-BPF (Xbox 02FD descriptor fixup) - sudo required"
        sudo udev-hid-bpf install --force "hid-bpf/$bpf_o"
        # Attach to an already-connected 02FD pad without a reconnect (the udev
        # rule otherwise only fires on the next add event).
        sudo udevadm control --reload
        sudo udevadm trigger --action=add --subsystem-match=hid
    else
        echo "note: skipping Xbox 02FD HID-BPF fixup (build prerequisites missing;" >&2
        echo "      see hid-bpf/build.sh output above). Only affects the 0x02FD pad." >&2
    fi
else
    echo "note: udev-hid-bpf not installed - skipping Xbox 02FD HID-BPF fixup." >&2
    echo "      Fedora: sudo dnf install udev-hid-bpf clang bpftool libbpf-devel" >&2
fi

# Render the sudoers rule for the installing user, validate it in isolation,
# then install it. Validating before placement avoids leaving a broken file in
# /etc/sudoers.d.
rendered_sudoers="$(mktemp)"
trap 'rm -f "$rendered_sudoers"' EXIT
sed "s/__INSTALL_USER__/$(id -un)/" controller-hidraw.sudoers > "$rendered_sudoers"
sudo visudo -cf "$rendered_sudoers"
sudo install -m 0440 -o root -g root "$rendered_sudoers" /etc/sudoers.d/controller-hidraw
sudo visudo -c

echo "==> Reload + enable (autostart) + restart user service"
systemctl --user daemon-reload
# enable (without --now) links the service into the session's autostart so it
# comes up on the next login; restart applies the freshly installed code to the
# already-running instance (which enable --now would leave untouched).
systemctl --user enable controller-manager.service
systemctl --user restart controller-manager.service
sleep 1
# Report the state without aborting on a non-active service (is-active exits
# non-zero then, which `set -e` would turn into a silent abort BEFORE the
# hint below). A failed start is usually a missing Python runtime dep.
if systemctl --user is-active --quiet controller-manager.service; then
    echo "service: active"
    echo "Done."
else
    echo "service: NOT active" >&2
    echo "check:  journalctl --user -u controller-manager.service -n 30" >&2
    exit 1
fi
