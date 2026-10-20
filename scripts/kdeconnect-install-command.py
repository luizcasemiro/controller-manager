#!/usr/bin/env python3
"""Install the Controller Manager command into KDE Connect's Run Command plugin.

The plugin stores each paired device's commands as a JSON object wrapped in a
KConfig ``@ByteArray(...)`` value. This helper edits only that value, keeps a
timestamped backup, verifies the result and is idempotent. It never touches
keys, certificates or other plugin settings.

Run it while KDE Connect is closed, or restart kdeconnectd afterwards so the
plugin reloads its configuration.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

DEFAULT_COMMAND = os.path.expanduser("~/.local/bin/controller-setup.py")
DEFAULT_NAME = "Preparar controles e abrir Heroic"


class ConfigError(RuntimeError):
    pass


def _unescape_kconfig(value: str) -> str:
    """Decode a KConfig quoted string value without touching JSON escapes.

    KConfig writes ``commands="@ByteArray({...})"``: the OUTER quoting is
    KConfig's, while ``\\"`` inside belongs to that outer string. Decode only
    the KConfig escape layer; the JSON parser handles the rest.
    """
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _escape_kconfig(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_commands(value: str) -> Dict[str, dict]:
    value = value.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = _unescape_kconfig(value[1:-1])
    value = value.strip()
    if value.startswith("@ByteArray(") and value.endswith(")"):
        value = value[len("@ByteArray("):-1]
    try:
        data = json.loads(value)
    except ValueError as ex:
        raise ConfigError("invalid commands JSON: " + str(ex)) from ex
    if not isinstance(data, dict):
        raise ConfigError("commands value is not a JSON object")
    return data


def render_commands(commands: Dict[str, dict]) -> str:
    payload = json.dumps(commands, ensure_ascii=False, separators=(",", ":"))
    return '"@ByteArray(' + _escape_kconfig(payload) + ')"'


def _command_id() -> str:
    return "_" + str(uuid.uuid4()).replace("-", "_") + "_"


def add_command(commands: Dict[str, dict], command: str,
                name: str) -> Tuple[Dict[str, dict], bool]:
    for entry in commands.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("command", "")) == command:
            if entry.get("name") != name:
                entry["name"] = name
                return commands, True
            return commands, False
    commands[_command_id()] = {"command": command, "name": name}
    return commands, True


def update_config_file(path: str, command: str, name: str,
                       dry_run: bool = False) -> Tuple[bool, str]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    found = False
    changed = False
    updated: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("commands="):
            found = True
            raw = stripped[len("commands="):]
            commands = parse_commands(raw)
            commands, changed = add_command(commands, command, name)
            newline = "\n" if line.endswith("\n") else ""
            updated.append("commands=" + render_commands(commands) + newline)
        else:
            updated.append(line)
    if not found:
        raise ConfigError("no commands= entry in " + path)
    if not changed:
        return False, "already configured"
    if dry_run:
        return True, "would update"

    backup = path + ".bak-" + time.strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    mode = os.stat(path).st_mode
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(updated))
    os.chmod(tmp, mode)
    os.replace(tmp, path)

    # Re-read and verify instead of trusting the in-memory edit.
    with open(path, encoding="utf-8") as f:
        check_text = f.read()
    check_line = next((line for line in check_text.splitlines()
                       if line.strip().startswith("commands=")), "")
    parsed = parse_commands(check_line.strip()[len("commands="):])
    if not any(str(v.get("command", "")) == command
               for v in parsed.values() if isinstance(v, dict)):
        shutil.copy2(backup, path)
        raise ConfigError("verification failed; restored " + backup)
    return True, "updated (backup: " + backup + ")"


def config_files(root: str) -> List[str]:
    pattern = os.path.join(root, "*", "kdeconnect_runcommand", "config")
    return sorted(glob.glob(pattern))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument(
        "--config-root",
        default=os.path.join(
            os.environ.get("XDG_CONFIG_HOME",
                           os.path.expanduser("~/.config")),
            "kdeconnect"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    files = config_files(args.config_root)
    if not files:
        print("KDE Connect Run Command config not found under "
              + args.config_root)
        print("Add the command manually in KDE Connect: " + args.command)
        return 0

    failed = False
    for path in files:
        try:
            changed, detail = update_config_file(
                path, args.command, args.name, dry_run=args.dry_run)
            prefix = "changed" if changed else "unchanged"
            print(prefix + ": " + path + " (" + detail + ")")
        except Exception as ex:
            failed = True
            print("skipped: " + path + ": " + str(ex), file=sys.stderr)
    if failed:
        print("Some KDE Connect device configs were left untouched.",
              file=sys.stderr)
        return 1
    if not args.dry_run:
        print("Restart KDE Connect (or kdeconnectd) to reload the command list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
