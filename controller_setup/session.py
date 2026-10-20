"""Pairing state machine.

The session is intentionally UI-free. It owns the scan window, the decision to
stop after the first new controller, per-device progress and the two actions
the UI exposes (scan again / open Heroic). The GTK layer polls snapshots at a
low frequency; no D-Bus or ``subprocess`` call runs on the main loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .bluez import BackendError, DeviceSnapshot
from .support import describe_duration

PHASE_IDLE = "idle"
PHASE_CHECKING = "checking"
PHASE_ACTIVATING = "activating"
PHASE_SCANNING = "scanning"
PHASE_READY = "ready"
PHASE_TIMEOUT = "timeout"
PHASE_ERROR = "error"
PHASE_CLOSING_HEROIC = "closing_heroic"
PHASE_VERIFYING_MANAGER = "verifying_manager"
PHASE_LAUNCHING = "launching"
PHASE_CLOSED = "closed"

STATUS_FOUND = "found"
STATUS_PAIRING = "pairing"
STATUS_TRUSTING = "trusting"
STATUS_CONNECTING = "connecting"
STATUS_READY = "ready"
STATUS_ERROR = "error"

_STATUS_ORDER = {
    STATUS_READY: 0,
    STATUS_CONNECTING: 1,
    STATUS_TRUSTING: 2,
    STATUS_PAIRING: 3,
    STATUS_FOUND: 4,
    STATUS_ERROR: 5,
}

_STATUS_LABEL = {
    STATUS_FOUND: "Encontrado",
    STATUS_PAIRING: "Pareando...",
    STATUS_TRUSTING: "Confiando...",
    STATUS_CONNECTING: "Conectando...",
    STATUS_READY: "Conectado e pronto",
    STATUS_ERROR: "Nao foi possivel conectar",
}


@dataclass
class DeviceView:
    identity: str
    name: str
    status: str = STATUS_FOUND
    detail: str = "Aguardando o controle ficar disponivel"
    error: str = ""
    address: str = ""
    order: int = 0

    @property
    def label(self) -> str:
        return _STATUS_LABEL.get(self.status, self.detail or self.status)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY


@dataclass
class UiSnapshot:
    phase: str = PHASE_IDLE
    message: str = "Preparando..."
    detail: str = ""
    seconds_left: float = 0.0
    devices: List[DeviceView] = field(default_factory=list)
    heroic_ready: bool = False
    can_scan_more: bool = False
    can_retry: bool = False
    game_mode_active: bool = False
    busy: bool = False
    error: str = ""
    manager_ok: bool = True
    manager_detail: str = ""
    manager_count: int = 0
    pair_only: bool = False

    @property
    def countdown(self) -> str:
        return describe_duration(self.seconds_left)

    @property
    def active_scan(self) -> bool:
        return self.phase in (PHASE_SCANNING, PHASE_ACTIVATING,
                              PHASE_CHECKING, PHASE_VERIFYING_MANAGER)


class PairingSession:
    """Owns one KDE-Connect-triggered setup session."""

    def __init__(self, backend, game_mode, heroic, *,
                 scan_seconds: float = 60.0,
                 pair_after: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 logger: Optional[logging.Logger] = None,
                 manager_health=None,
                 pair_only: bool = False):
        self._backend = backend
        self._game_mode = game_mode
        self._heroic = heroic
        self._health = manager_health
        self._pair_only = bool(pair_only)
        self._scan_seconds = max(0.1, float(scan_seconds))
        self._pair_after = max(0.0, float(pair_after))
        self._clock = clock
        self._sleep = sleep
        self._log = logger or logging.getLogger("controller-setup")
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._phase = PHASE_IDLE
        self._message = "Preparando..."
        self._detail = ""
        self._error = ""
        self._deadline = 0.0
        self._game_mode_active = False
        self._devices: Dict[str, DeviceView] = {}
        self._first_seen: Dict[str, float] = {}
        self._next_order = 0
        self._attempted = set()
        self._manager_ok = True
        self._manager_detail = ""
        self._manager_count = 0

    # -- lifecycle -----------------------------------------------------------

    @property
    def pair_only(self) -> bool:
        return self._pair_only

    def start(self) -> bool:
        """Start the full check -> Game Mode -> discovery flow."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            self._cancel.clear()
            self._phase = PHASE_CHECKING
            self._message = "Verificando o Heroic..."
            self._detail = "O Game Mode sera ativado em seguida."
            self._error = ""
            self._deadline = 0.0
            self._game_mode_active = False
            self._devices = {}
            self._first_seen = {}
            self._attempted = set()
            target = self._run_pair_only if self._pair_only else self._run_full
            self._worker = threading.Thread(
                target=target, name="controller-setup", daemon=True)
            self._worker.start()
            return True

    def scan_more(self) -> bool:
        """Start a new discovery window, keeping already-ready controls."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            if not (self._game_mode_active or self._pair_only):
                return False
            self._cancel.clear()
            self._phase = PHASE_SCANNING
            self._message = "Procurando mais controles..."
            self._detail = "Coloque o proximo controle em modo de pareamento."
            self._error = ""
            self._attempted = set()
            self._worker = threading.Thread(
                target=self._run_more, name="controller-setup-more",
                daemon=True)
            self._worker.start()
            return True

    def retry(self) -> bool:
        """Retry the failed step without duplicating the Game Mode call."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            already_active = self._game_mode_active
            health_retry = (self._health is not None
                            and not self._manager_ok
                            and self._heroic_ready_locked())
            if health_retry:
                self._cancel.clear()
                self._phase = PHASE_VERIFYING_MANAGER
                self._message = "Verificando o Controller Manager..."
                self._detail = "Aguardando o daemon e o controle."
                self._worker = threading.Thread(
                    target=self._run_manager_retry,
                    name="controller-setup-health", daemon=True)
                self._worker.start()
                return True
        if already_active or self._pair_only:
            return self.scan_more()
        return self.start()

    def shutdown(self, timeout: float = 2.5) -> None:
        """Cancel the scan before the window/process goes away."""
        with self._lock:
            self._cancel.set()
            self._phase = PHASE_CLOSED
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def open_heroic(self) -> Tuple[bool, str]:
        """Stop discovery, verify the daemon, then launch Heroic."""
        if self._pair_only:
            return False, "Sessao somente de pareamento."
        with self._lock:
            if not self._heroic_ready_locked():
                return False, "Conecte um controle antes de abrir o Heroic."
            self._phase = PHASE_VERIFYING_MANAGER
            self._message = "Verificando o Controller Manager..."
            self._detail = "Garantindo que o daemon esta gerenciando o controle."
            self._cancel.set()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3.0)
        identity, name = self._ready_identity()
        if not self._check_manager(identity, name, timeout=8.0):
            with self._lock:
                self._phase = PHASE_ERROR
                self._error = self._manager_detail
                self._message = "Controller Manager indisponivel"
                self._detail = self._manager_detail
            return False, self._manager_detail
        with self._lock:
            self._phase = PHASE_LAUNCHING
            self._message = "Abrindo Heroic..."
            self._detail = "Iniciando o modo console em tela cheia."
        ok, error = self._heroic.launch()
        if not ok:
            with self._lock:
                self._phase = PHASE_ERROR
                self._error = error
                self._message = "Nao foi possivel abrir o Heroic"
                self._detail = error
            self._log.error("heroic launch failed: %s", error)
            return False, error
        self._log.info("heroic launched in console mode")
        # Short post-launch check: the daemon is an independent service and
        # can die while the launcher starts. Do not keep the UI open for the
        # full grace unless a restart is actually needed.
        self._sleep(0.8)
        identity, name = self._ready_identity()
        self._check_manager(identity, name, timeout=4.0)
        with self._lock:
            self._phase = PHASE_CLOSED
        return True, ""

    def _check_manager(self, identity: Optional[str] = None,
                       name: Optional[str] = None,
                       timeout: float = 8.0) -> bool:
        if self._health is None:
            with self._lock:
                self._manager_ok = True
                self._manager_detail = ""
            return True
        ok, detail, controllers = self._health.check(
            identity=identity, name=name, restart=True, timeout=timeout)
        with self._lock:
            self._manager_ok = bool(ok)
            self._manager_detail = detail
            self._manager_count = len(controllers or [])
        if not ok:
            self._log.warning("controller manager health: %s", detail)
        return ok

    def _verify_manager(self, device: DeviceSnapshot) -> None:
        """Check the daemon and, specifically, that it adopted this pad."""
        if self._health is not None:
            self._set_device_status(
                device.identity, STATUS_CONNECTING,
                "Verificando o Controller Manager...")
            self._check_manager(device.identity, device.name, timeout=8.0)
        # The controller is ready from BlueZ's point of view even when the
        # daemon needs a retry; keep the card ready and expose the manager
        # state separately.
        self._mark_ready(device)

    def _ready_identity(self) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            for view in self._devices.values():
                if view.status == STATUS_READY:
                    return view.identity, view.name
        return None, None

    # -- worker --------------------------------------------------------------

    def _run_full(self) -> None:
        try:
            error = self._heroic.validate()
            if error:
                self._fail(PHASE_ERROR, "Heroic indisponivel", error)
                return
            if self._heroic.is_running():
                with self._lock:
                    if self._cancel.is_set():
                        return
                    self._phase = PHASE_CLOSING_HEROIC
                    self._message = "Fechando o Heroic..."
                    self._detail = ("Encerrando a instancia aberta para abrir "
                                    "no modo console.")
                ok, error = self._heroic.close()
                if not ok:
                    self._fail(PHASE_ERROR,
                               "Nao foi possivel fechar o Heroic", error)
                    return
            error = self._game_mode.validate()
            if error:
                self._fail(PHASE_ERROR, "Game Mode indisponivel", error)
                return
            with self._lock:
                if self._cancel.is_set():
                    return
                self._phase = PHASE_ACTIVATING
                self._message = "Ativando o Game Mode..."
                self._detail = "Mudando monitor, audio e ajustes de jogo."
            ok, error = self._game_mode.activate()
            if not ok:
                self._fail(PHASE_ERROR, "Falha ao ativar o Game Mode", error)
                return
            with self._lock:
                if self._cancel.is_set():
                    return
                self._game_mode_active = True
            self._scan_cycle()
        except Exception as ex:
            self._log.exception("full setup flow failed")
            self._fail(PHASE_ERROR, "Falha inesperada", str(ex))

    def _run_pair_only(self) -> None:
        """Pairing from the overlay: no Heroic, no Game Mode transition."""
        try:
            with self._lock:
                if self._cancel.is_set():
                    return
                self._phase = PHASE_SCANNING
                self._message = "Procurando controles"
                self._detail = "Ligue o controle e mantenha-o em modo de pareamento."
            self._scan_cycle()
        except Exception as ex:
            self._log.exception("pair-only flow failed")
            self._fail(PHASE_ERROR, "Falha inesperada", str(ex))

    def _run_manager_retry(self) -> None:
        try:
            identity, name = self._ready_identity()
            ok = self._check_manager(identity, name, timeout=8.0)
            with self._lock:
                if self._cancel.is_set():
                    return
                self._phase = PHASE_READY
                if ok:
                    self._message = "Controller Manager ativo"
                    self._detail = "O controle esta sendo gerenciado."
                else:
                    self._message = "Controller Manager indisponivel"
                    self._detail = self._manager_detail
                self._deadline = 0.0
        except Exception as ex:
            self._log.exception("manager health retry failed")
            self._fail(PHASE_ERROR, "Falha ao verificar o Controller Manager",
                       str(ex))

    def _run_more(self) -> None:
        try:
            self._scan_cycle()
        except Exception as ex:
            self._log.exception("additional scan failed")
            self._fail(PHASE_ERROR, "Falha inesperada", str(ex))

    def _scan_cycle(self) -> None:
        try:
            initial = self._backend.devices()
        except Exception as ex:
            self._fail(PHASE_ERROR, "Bluetooth indisponivel", str(ex))
            return

        baseline = set()
        first_connected = None
        for device in initial:
            if not device.is_gamepad:
                continue
            self._remember(device)
            if device.connected:
                baseline.add(device.identity)
                self._mark_ready(device)
                if first_connected is None:
                    first_connected = device
        # A ready pad that momentarily reports disconnected is still not a
        # "new" success for an additional scan; do not stop on its reconnect.
        with self._lock:
            baseline.update(v.identity for v in self._devices.values()
                            if v.status == STATUS_READY)
        if first_connected is not None:
            self._verify_manager(first_connected)

        with self._lock:
            if self._cancel.is_set():
                return
            self._deadline = self._clock() + self._scan_seconds
            self._phase = PHASE_SCANNING
            self._message = ("Procurando controles"
                             if not self._heroic_ready_locked()
                             else "Procurando mais controles...")
            self._detail = "Mantenha o controle ligado e em modo de pareamento."
            self._attempted = set()

        started = False
        try:
            self._backend.begin_scan()
            started = True
            while not self._cancel.is_set():
                now = self._clock()
                if now >= self._deadline:
                    break
                try:
                    devices = self._backend.devices()
                except BackendError as ex:
                    self._log.warning("device refresh failed: %s", ex)
                    devices = []
                success = self._process_devices(devices, baseline)
                if success:
                    with self._lock:
                        if self._cancel.is_set():
                            return
                        self._phase = PHASE_READY
                        self._message = "Controle conectado"
                        self._detail = ("Tudo pronto. Abra o Heroic ou "
                                        "conecte mais um controle.")
                        self._deadline = 0.0
                    return
                if self._cancel.wait(0.35):
                    break
        except BackendError as ex:
            if not self._cancel.is_set():
                self._fail(PHASE_ERROR, "Bluetooth indisponivel", str(ex))
            else:
                self._log.info("scan aborted: %s", ex)
            return
        except Exception as ex:
            if not self._cancel.is_set():
                self._log.exception("scan failed")
                self._fail(PHASE_ERROR, "Falha na busca Bluetooth", str(ex))
            return
        finally:
            if started:
                try:
                    self._backend.end_scan()
                except Exception as ex:
                    self._log.warning("end_scan failed: %s", ex)

        with self._lock:
            if self._cancel.is_set():
                return
            self._deadline = 0.0
            if self._heroic_ready_locked():
                self._phase = PHASE_TIMEOUT
                self._message = "Busca concluida"
                self._detail = ("Nenhum controle novo, mas um controle ja "
                                "esta pronto para jogar.")
            else:
                self._phase = PHASE_TIMEOUT
                self._message = "Nenhum controle encontrado"
                self._detail = ("Coloque o controle em modo de pareamento e "
                                "tente novamente.")

    def _process_devices(self, devices: Sequence[DeviceSnapshot],
                         baseline: set) -> bool:
        """Update cards and attempt at most one device per pass.

        Returns True when a controller that was not already connected at the
        start of this window becomes ready. That is the moment the UI stops
        discovery and enables Heroic.
        """
        now = self._clock()
        # First notice auto-reconnecting/completed devices. A pad that is
        # already connected must stop the search before we spend the pass
        # pairing an unrelated candidate.
        for device in devices:
            if not device.is_gamepad:
                continue
            self._remember(device)
            if device.connected:
                if device.identity not in baseline:
                    self._verify_manager(device)
                    self._log.info("controller connected: %s", device.name)
                    return True
                self._mark_ready(device)

        for device in devices:
            if not device.is_gamepad or device.connected:
                continue
            self._remember(device)
            with self._lock:
                seen = self._first_seen.get(device.identity, now)
                attempted = device.identity in self._attempted
            if attempted:
                continue
            if now - seen < self._pair_after:
                continue
            self._attempt_device(device)
            with self._lock:
                self._attempted.add(device.identity)
                view = self._devices.get(device.identity)
                if view is not None and view.ready:
                    self._log.info("controller paired and connected: %s",
                                   device.name)
                    return True
            # One attempt per pass: the next tick refreshes the BlueZ device
            # list and can notice an auto-reconnecting control immediately.
            return False
        return False

    def _attempt_device(self, device: DeviceSnapshot) -> None:
        self._set_device_status(device.identity, STATUS_PAIRING,
                                "Pareando o controle...")
        try:
            if not device.paired:
                self._backend.pair(device)
            self._set_device_status(device.identity, STATUS_TRUSTING,
                                    "Marcando o controle como confiavel...")
            self._backend.set_trusted(device)
            self._set_device_status(device.identity, STATUS_CONNECTING,
                                    "Aguardando a conexao...")
            self._backend.connect(device)
            fresh = self._backend.find_device(device.identity)
            if fresh is None or not fresh.connected:
                raise BackendError(
                    "O controle nao apareceu como conectado no Bluetooth.")
            self._verify_manager(fresh)
        except BackendError as ex:
            self._set_device_error(device.identity, str(ex))
            self._log.warning("pair/connect failed for %s: %s",
                              device.name, ex)
        except Exception as ex:
            self._set_device_error(device.identity, str(ex))
            self._log.exception("unexpected pair/connect failure")

    # -- state helpers -------------------------------------------------------

    def _remember(self, device: DeviceSnapshot) -> None:
        with self._lock:
            if device.identity not in self._first_seen:
                self._first_seen[device.identity] = self._clock()
            view = self._devices.get(device.identity)
            if view is None:
                self._next_order += 1
                view = DeviceView(
                    identity=device.identity,
                    name=device.name,
                    address=device.address,
                    order=self._next_order,
                )
                self._devices[device.identity] = view
            else:
                view.name = device.name
                view.address = device.address

    def _mark_ready(self, device: DeviceSnapshot) -> None:
        with self._lock:
            view = self._devices.get(device.identity)
            if view is None:
                self._next_order += 1
                view = DeviceView(
                    identity=device.identity,
                    name=device.name,
                    address=device.address,
                    order=self._next_order,
                )
                self._devices[device.identity] = view
            view.status = STATUS_READY
            view.error = ""
            view.detail = "Controle disponivel para o Heroic"
            self._first_seen.setdefault(device.identity, self._clock())

    def _set_device_status(self, identity: str, status: str,
                           detail: str) -> None:
        with self._lock:
            view = self._devices.get(identity)
            if view is None:
                return
            view.status = status
            view.detail = detail
            view.error = ""

    def _set_device_error(self, identity: str, error: str) -> None:
        with self._lock:
            view = self._devices.get(identity)
            if view is None:
                return
            view.status = STATUS_ERROR
            view.detail = "Nao foi possivel conectar"
            view.error = error

    def _fail(self, phase: str, message: str, detail: str) -> None:
        with self._lock:
            if self._cancel.is_set() and self._phase == PHASE_CLOSED:
                return
            self._phase = phase
            self._message = message
            self._detail = detail
            self._error = detail
            self._deadline = 0.0
        self._log.warning("%s: %s", message, detail)

    def _heroic_ready_locked(self) -> bool:
        return any(v.status == STATUS_READY for v in self._devices.values())

    # -- UI snapshot ---------------------------------------------------------

    def snapshot(self) -> UiSnapshot:
        with self._lock:
            devices = sorted(
                self._devices.values(),
                key=lambda v: (_STATUS_ORDER.get(v.status, 9), v.order))
            seconds = 0.0
            if self._phase in (PHASE_SCANNING, PHASE_ACTIVATING,
                               PHASE_CHECKING) and self._deadline:
                seconds = max(0.0, self._deadline - self._clock())
            busy = (self._worker is not None and self._worker.is_alive())
            ready = self._heroic_ready_locked()
            can_scan = ((self._game_mode_active or self._pair_only)
                        and not busy
                        and self._phase in (PHASE_READY, PHASE_TIMEOUT))
            can_retry = (not busy and (
                self._phase in (PHASE_ERROR, PHASE_TIMEOUT)
                or (ready and not self._manager_ok)))
            return UiSnapshot(
                phase=self._phase,
                message=self._message,
                detail=self._detail,
                seconds_left=seconds,
                devices=[DeviceView(
                    identity=v.identity, name=v.name, status=v.status,
                    detail=v.detail, error=v.error, address=v.address,
                    order=v.order) for v in devices],
                heroic_ready=ready,
                can_scan_more=can_scan,
                can_retry=can_retry,
                game_mode_active=self._game_mode_active,
                busy=busy,
                error=self._error,
                manager_ok=self._manager_ok,
                manager_detail=self._manager_detail,
                manager_count=self._manager_count,
                pair_only=self._pair_only,
            )

    def wait(self, timeout: float = 2.0) -> None:
        """Test/helper hook: wait for the current worker to finish."""
        with self._lock:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
