"""Controller Manager service health checks.

The pairing UI and the overlay must not release Heroic while the tray daemon
is dead or its D-Bus API is unavailable. This module checks the user service,
the D-Bus controller list, optionally restarts the service once, and reports
whether a specific Bluetooth controller has been adopted by the daemon.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import List, Optional, Sequence, Tuple

SERVICE = "controller-manager.service"
BUS_NAME = "org.ctrlmgr.ControllerManager1"
OBJECT_PATH = "/ControllerManager"
INTERFACE = "org.ctrlmgr.ControllerManager1"


class ControllerManagerHealth:
    def __init__(self, service: str = SERVICE, bus=None,
                 runner=subprocess.run, sleep=time.sleep,
                 clock=time.monotonic,
                 controller_provider=None,
                 logger: Optional[logging.Logger] = None):
        self.service = service
        self._bus = bus
        self._runner = runner
        self._sleep = sleep
        self._clock = clock
        self._controller_provider = controller_provider
        self._log = logger or logging.getLogger("controller-setup")

    # -- commands ------------------------------------------------------------

    def _systemctl(self, *args: str, timeout: float = 12.0):
        return self._runner(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=timeout, check=False)

    def service_active(self) -> bool:
        try:
            result = self._systemctl("is-active", "--quiet", self.service)
            return result.returncode == 0
        except Exception:
            return False

    def restart(self) -> Tuple[bool, str]:
        try:
            result = self._systemctl("restart", self.service, timeout=20.0)
        except Exception as ex:
            return False, "Falha ao reiniciar o Controller Manager: " + str(ex)
        if result.returncode != 0:
            detail = ((result.stderr or "") + "\n" +
                      (result.stdout or "")).strip()
            detail = detail.splitlines()[-1] if detail else "systemctl falhou"
            return False, "Falha ao reiniciar o Controller Manager: " + detail
        return True, ""

    def _session_bus(self):
        if self._bus is not None:
            return self._bus
        import dbus
        self._bus = dbus.SessionBus()
        return self._bus

    def list_controllers(self) -> List[Tuple[str, str, str, str]]:
        if self._controller_provider is not None:
            return list(self._controller_provider())
        import dbus
        bus = self._session_bus()
        obj = bus.get_object(BUS_NAME, OBJECT_PATH)
        iface = dbus.Interface(obj, INTERFACE)
        rows = iface.ListControllers(timeout=5)
        return [tuple(str(value) for value in row) for row in rows]

    # -- checks --------------------------------------------------------------

    @staticmethod
    def _matches(rows: Sequence[Tuple[str, str, str, str]],
                 identity: Optional[str], name: Optional[str]) -> bool:
        if not identity and not name:
            return True
        identity = (identity or "").casefold()
        name = (name or "").casefold()
        identity_key = re.sub(r"[^a-z0-9]+", "", identity)
        for row in rows:
            ident, cname = row[0].casefold(), row[1].casefold()
            ident_key = re.sub(r"[^a-z0-9]+", "", ident)
            if identity and (ident == identity or
                             (identity_key and ident_key == identity_key)):
                return True
            if name and (cname == name or name in cname or cname in name):
                return True
        return False

    def check(self, identity: Optional[str] = None,
              name: Optional[str] = None,
              restart: bool = True,
              timeout: float = 8.0) -> Tuple[bool, str, List[tuple]]:
        """Wait for service + D-Bus (+ optionally the controller).

        Restarts the user service at most once, and only when the service is
        inactive or its D-Bus object is unreachable. A service that is up but
        has not adopted the controller yet is given the remaining wait time -
        its monitor polls every two seconds.
        """
        deadline = self._clock() + max(0.0, float(timeout))
        restarted = False
        last = "Controller Manager indisponivel"
        controllers: List[tuple] = []
        while True:
            service_ok = self.service_active()
            dbus_ok = False
            try:
                controllers = self.list_controllers()
                dbus_ok = True
            except Exception as ex:
                last = ("Controller Manager sem resposta D-Bus: "
                        + str(ex))

            if restart and not restarted and (not service_ok or not dbus_ok):
                ok, detail = self.restart()
                restarted = True
                if not ok:
                    last = detail
                self._sleep(0.6)
                continue

            if dbus_ok and self._matches(controllers, identity, name):
                return True, "", controllers
            if dbus_ok:
                last = ("O Controller Manager ainda nao listou o controle "
                        + (identity or name or ""))
            elif not service_ok:
                last = "Servico controller-manager.service inativo"

            if self._clock() >= deadline:
                break
            self._sleep(0.4)

        self._log.warning("controller manager health failed: %s", last)
        return False, last, controllers

    def status(self) -> Tuple[bool, str, List[tuple]]:
        return self.check(restart=False, timeout=1.0)
