"""Process, configuration and logging helpers for the pairing session.

Nothing here imports GTK or D-Bus, so this module (and the session state
machine) stays easy to unit test. Process boundaries are injectable, which is
also what gives the tests a deterministic way to simulate a busy Heroic or a
failing Game Mode script.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Sequence, Tuple

APP_ID = "com.heroicgameslauncher.hgl"
DEFAULT_HEROIC_ARGS = ("--console", "--fullscreen")

# The existing Game Mode script lives outside this repository. Keep the
# current workstation path as a default but let the environment or the user's
# config file override it, so the repository stays portable.
def _default_gamemode_script() -> str:
    candidates = (
        os.path.expanduser("~/Documentos/dev/gamemode/gamemode.sh"),
        os.path.expanduser("~/Documents/dev/gamemode/gamemode.sh"),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


DEFAULT_GAMEMODE_SCRIPT = _default_gamemode_script()
CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "controller-setup.json")


def state_dir() -> str:
    base = os.environ.get(
        "XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(base, "controller-setup")


def log_path() -> str:
    return os.path.join(state_dir(), "session.log")


def setup_logging() -> logging.Logger:
    os.makedirs(state_dir(), exist_ok=True)
    logger = logging.getLogger("controller-setup")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(
            log_path(), maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


@dataclass(frozen=True)
class SetupConfig:
    gamemode_script: str = DEFAULT_GAMEMODE_SCRIPT
    flatpak_app: str = APP_ID
    heroic_args: Tuple[str, ...] = DEFAULT_HEROIC_ARGS
    heroic_command: Tuple[str, ...] = ()


def load_config() -> SetupConfig:
    data = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    script = os.environ.get("GAMEMODE_SCRIPT") or data.get(
        "gamemode_script") or DEFAULT_GAMEMODE_SCRIPT
    app = data.get("flatpak_app") or APP_ID
    args = data.get("heroic_args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        args = list(DEFAULT_HEROIC_ARGS)
    command = data.get("heroic_command")
    if not isinstance(command, list) or not command or not all(
            isinstance(a, str) for a in command):
        command = []
    else:
        command = list(command)
        command[0] = os.path.expanduser(command[0])
    return SetupConfig(
        gamemode_script=os.path.expanduser(str(script)),
        flatpak_app=str(app),
        heroic_args=tuple(args),
        heroic_command=tuple(command),
    )


class GameModeRunner:
    """Run the existing monitor/audio Game Mode script in its own process."""

    def __init__(self, script_path: str, timeout: float = 45.0,
                 runner=subprocess.run):
        self.script_path = script_path
        self.timeout = timeout
        self._runner = runner

    def validate(self) -> Optional[str]:
        if not self.script_path:
            return "O caminho do script de Game Mode nao foi configurado."
        if not os.path.isfile(self.script_path):
            return ("Script de Game Mode nao encontrado em "
                    + self.script_path)
        if not os.access(self.script_path, os.X_OK):
            return ("O script de Game Mode nao tem permissao de execucao: "
                    + self.script_path)
        return None

    def _invoke(self, argument: str) -> Tuple[bool, str]:
        error = self.validate()
        if error:
            return False, error
        try:
            result = self._runner(
                [self.script_path, argument],
                capture_output=True, text=True, timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, ("O Game Mode demorou demais para responder. "
                           "Verifique o monitor e o audio.")
        except Exception as ex:
            return False, "Nao foi possivel iniciar o Game Mode: " + str(ex)
        if result.returncode != 0:
            detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
            detail = detail.splitlines()[-1] if detail else ""
            return False, ("O Game Mode falhou" +
                           (": " + detail if detail else "."))
        return True, ""

    def activate(self) -> Tuple[bool, str]:
        return self._invoke("1")

    def disable(self) -> Tuple[bool, str]:
        return self._invoke("0")

    def is_active(self) -> bool:
        path = os.path.join(
            os.environ.get("XDG_STATE_HOME",
                           os.path.expanduser("~/.local/state")),
            "gamemode", "mode")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() == "jogo"
        except OSError:
            return False


def is_heroic_cmdline(line: str, flatpak_app: str = APP_ID) -> bool:
    """True for the Heroic launcher itself, not game/runtime children.

    Many Heroic-launched games and umu runtimes have a command line containing
    the app-data path ``~/.var/app/com.heroicgameslauncher.hgl``. Those must
    never be killed by the force-close step. The launcher is recognised only
    by its executable name/path or by an actual ``flatpak run <app>`` command.
    """
    tokens = line.split()
    if not tokens:
        return False
    for token in tokens:
        name = os.path.basename(token.rstrip("/"))
        if name in ("heroic", "heroic-games-launcher"):
            return True
    if "/app/bin/heroic" in line:
        return True
    if (os.path.basename(tokens[0]) == "flatpak"
            and flatpak_app in tokens):
        return True
    return False


def heroic_processes(flatpak_app: str = APP_ID) -> List[str]:
    """PIDs of the Heroic launcher (and its helpers), excluding game runtimes.

    Zombie processes are skipped: after a forced relaunch their unreaped
    entries must not make ``is_running`` true again.
    """
    result = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return result
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if not raw:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read()
            # Zombie processes have already exited; their parent may not have
            # reaped them yet, but they must not block a forced relaunch.
            close = stat.rfind(b")")
            if close >= 0 and stat[close + 2:close + 3] == b"Z":
                continue
        except OSError:
            continue
        line = raw.replace(b"\x00", b" ").decode(errors="replace")
        if is_heroic_cmdline(line, flatpak_app):
            result.append(pid)
    return result


def find_flatpak() -> Optional[str]:
    found = shutil.which("flatpak")
    if found:
        return found
    for candidate in ("/usr/bin/flatpak", "/usr/local/bin/flatpak",
                      "/var/lib/flatpak/exports/bin/flatpak"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class HeroicLauncher:
    """Validate and start Heroic in console mode without owning the process."""

    def __init__(self, flatpak_app: str = APP_ID,
                 args: Sequence[str] = DEFAULT_HEROIC_ARGS,
                 flatpak: Optional[str] = None,
                 command: Sequence[str] = (),
                 popen=subprocess.Popen,
                 runner=subprocess.run,
                 process_provider=heroic_processes,
                 killer=os.kill,
                 clock=time.monotonic,
                 sleep=time.sleep):
        self.flatpak_app = flatpak_app
        self.args = tuple(args)
        self.flatpak = flatpak
        self.command = tuple(command)
        self._popen = popen
        self._runner = runner
        self._process_provider = process_provider
        self._kill = killer
        self._clock = clock
        self._sleep = sleep

    def running_pids(self) -> List[int]:
        return [int(pid) for pid in self._process_provider(self.flatpak_app)
                if int(pid) != os.getpid()]

    def is_running(self) -> bool:
        return bool(self.running_pids())

    def close(self, grace: float = 2.0) -> Tuple[bool, str]:
        """Terminate a running Heroic, escalating to SIGKILL if needed.

        The user explicitly wants a fresh console-mode instance; a normal
        single-instance launch would reuse the existing window and ignore
        ``--console``. Send SIGTERM to every matching process (the Flatpak
        launcher and its helpers), wait briefly, then force-kill whatever is
        left.
        """
        pids = self.running_pids()
        if not pids:
            return True, ""
        for pid in pids:
            try:
                self._kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as ex:
                return False, "Sem permissao para encerrar o Heroic: " + str(ex)
            except Exception:
                pass
        deadline = self._clock() + max(0.0, float(grace))
        remaining = pids
        while self._clock() < deadline:
            self._sleep(0.2)
            remaining = self.running_pids()
            if not remaining:
                return True, ""
        for pid in remaining:
            try:
                self._kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as ex:
                return False, "Sem permissao para encerrar o Heroic: " + str(ex)
            except Exception:
                pass
        self._sleep(0.3)
        remaining = self.running_pids()
        if remaining:
            return False, ("O Heroic nao encerrou. PIDs restantes: "
                           + ", ".join(str(p) for p in remaining))
        return True, ""

    def _build_command(self) -> Tuple[Optional[List[str]], Optional[str]]:
        if self.command:
            executable = self.command[0]
            if os.path.sep in executable and not (
                    os.path.isfile(executable)
                    and os.access(executable, os.X_OK)):
                return None, "Heroic configurado nao e executavel: " + executable
            if os.path.sep not in executable and not shutil.which(executable):
                return None, "Heroic configurado nao encontrado: " + executable
            return list(self.command), None
        flatpak = self.flatpak or find_flatpak()
        if flatpak:
            try:
                result = self._runner(
                    [flatpak, "info", self.flatpak_app],
                    capture_output=True, text=True, timeout=12, check=False)
            except Exception:
                result = None
            if result is not None and result.returncode == 0:
                return [flatpak, "run", self.flatpak_app, *self.args], None
        native = shutil.which("heroic") or shutil.which("heroic-games-launcher")
        if native:
            return [native, *self.args], None
        if flatpak:
            return None, ("Heroic nao encontrado no Flatpak ("
                          + self.flatpak_app + ").")
        return None, ("Heroic nao encontrado. Instale o Flatpak ou configure "
                      "heroic_command em ~/.config/controller-setup.json.")

    def validate(self) -> Optional[str]:
        command, error = self._build_command()
        return error

    def launch(self) -> Tuple[bool, str]:
        command, error = self._build_command()
        if error:
            return False, error
        if self.is_running():
            ok, error = self.close()
            if not ok:
                return False, error
        os.makedirs(state_dir(), exist_ok=True)
        try:
            with open(os.path.join(state_dir(), "heroic.log"), "ab") as out:
                self._popen(
                    command,
                    stdout=out,
                    stderr=out,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception as ex:
            return False, "Nao foi possivel abrir o Heroic: " + str(ex)
        return True, ""


def describe_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
