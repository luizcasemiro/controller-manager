"""BlueZ D-Bus backend for the visual pairing session.

The backend talks to the system bus directly instead of scraping
``bluetoothctl`` output. That keeps the device list structured, makes the
pairing agent callback-based, and avoids depending on a terminal UI's text
format. Every operation here is blocking and is called from the session
worker thread, never from GTK's main loop.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import dbus
import dbus.service
from dbus.exceptions import DBusException

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

AGENT_PATH = "/org/ctrlmgr/Agent"
AGENT_MANAGER_PATH = "/org/bluez"

# A conservative name list. BlueZ sometimes exposes no Icon and a zero Class
# during the first discovery pass, so the alias is the only hint available.
# Keep this list about controllers, never about generic input devices.
_GAMEPAD_NAME_RE = re.compile(
    r"(controller|gamepad|game\s*pad|joystick|dualsense|dualshock|"
    r"xbox|8bitdo|8-bitdo|steam\s*controller|switch\s*pro|joy-con|"
    r"stadia|shield\s*controller|wireless\s+controller)",
    re.IGNORECASE,
)

_REJECT_NAME_RE = re.compile(
    r"(keyboard|mouse|trackpad|touchpad|headset|headphone|earbud|earphone|"
    r"speaker|soundbar|audio|phone|iphone|android|galaxy|pixel|tablet|"
    r"printer|scanner|watch|band|tag|beacon)",
    re.IGNORECASE,
)

_REJECT_ICONS = {
    "input-keyboard",
    "input-mouse",
    "input-tablet",
    "phone",
    "smartphone",
    "audio-card",
    "audio-headset",
    "audio-headphones",
    "audio-speakers",
    "printer",
    "computer",
    "camera-photo",
    "camera-video",
    "video-display",
}

_POSITIVE_ICONS = {"input-gaming", "input-gaming-symbolic"}


class BackendError(RuntimeError):
    """A user-presentable BlueZ failure (no adapter, pair refused, ...)."""


@dataclass(frozen=True)
class DeviceSnapshot:
    """One ``org.bluez.Device1`` object as seen in a discovery pass."""

    path: str
    address: str
    name: str
    icon: str = ""
    device_class: int = 0
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    rssi: Optional[int] = None
    properties: Dict[str, object] = field(default_factory=dict, compare=False)

    @property
    def identity(self) -> str:
        return self.address or self.path

    @property
    def is_gamepad(self) -> bool:
        return is_gamepad(self.properties)


def _as_bool(value) -> bool:
    try:
        return bool(value)
    except Exception:
        return False


def _device_from_props(path: str, props: dict) -> DeviceSnapshot:
    address = str(props.get("Address", "") or "")
    name = str(props.get("Alias") or props.get("Name") or address or path)
    rssi = props.get("RSSI")
    try:
        rssi = int(rssi) if rssi is not None else None
    except (TypeError, ValueError):
        rssi = None
    try:
        device_class = int(props.get("Class", 0) or 0)
    except (TypeError, ValueError):
        device_class = 0
    return DeviceSnapshot(
        path=path,
        address=address,
        name=name,
        icon=str(props.get("Icon", "") or ""),
        device_class=device_class,
        paired=_as_bool(props.get("Paired", False)),
        trusted=_as_bool(props.get("Trusted", False)),
        connected=_as_bool(props.get("Connected", False)),
        rssi=rssi,
        properties=dict(props),
    )


def _bluetooth_class_gamepad(device_class: int) -> Optional[bool]:
    """Classify the Bluetooth Class of Device.

    Returns True/False when the major class is conclusive, None when the
    field is absent or uncategorized and the name should decide. Major class
    0x05 is Peripheral; the low byte distinguishes joystick (0x04) and gamepad
    (0x08) from keyboard/mouse/remote classes.
    """
    if not device_class:
        return None
    major = (device_class >> 8) & 0x1F
    if major == 0x00:
        return None
    if major != 0x05:
        return False
    minor = device_class & 0xFF
    if minor in (0x04, 0x08):
        return True
    # Keyboard/pointing/remote classes are conclusive rejections. An
    # uncategorized peripheral (minor 0x00) still falls through to the name,
    # because some controllers expose a generic class until they connect.
    if minor in (0x0C, 0x40, 0x80, 0xC0):
        return False
    return None


def is_gamepad(props: dict) -> bool:
    """True only for plausible Bluetooth game controllers.

    Order matters: BlueZ's Icon and Class are more reliable than the name.
    When either says this is a keyboard, mouse, phone or audio device, the
    name cannot override it. Unknown devices are not paired automatically.
    """
    props = props or {}
    icon = str(props.get("Icon", "") or "").strip().lower()
    name = str(props.get("Alias") or props.get("Name") or "").strip()

    if icon in _POSITIVE_ICONS or icon.endswith("input-gaming"):
        return True
    if icon in _REJECT_ICONS:
        return False

    try:
        device_class = int(props.get("Class", 0) or 0)
    except (TypeError, ValueError):
        device_class = 0
    class_verdict = _bluetooth_class_gamepad(device_class)
    if class_verdict is not None:
        return class_verdict

    if not name:
        return False
    if _REJECT_NAME_RE.search(name):
        return False
    return bool(_GAMEPAD_NAME_RE.search(name))


def _dbus_error_text(ex: Exception) -> str:
    if isinstance(ex, DBusException):
        name = ex.get_dbus_name() or "org.bluez.Error"
        message = ex.get_dbus_message() or str(ex)
        return f"{name}: {message}"
    return str(ex)


class _PairingAgent(dbus.service.Object):
    """Minimal BlueZ agent that auto-confirms only the active candidate.

    ``NoInputNoOutput`` lets BlueZ use Just Works for most gamepads. If a
    controller asks a question outside the candidate path, the request is
    rejected instead of being silently accepted.
    """

    def __init__(self, bus, path: str):
        super().__init__(bus, path)
        self._candidate: Optional[str] = None

    def set_candidate(self, path: Optional[str]) -> None:
        self._candidate = path

    def _allowed(self, device) -> bool:
        return self._candidate is not None and str(device) == self._candidate

    def _reject(self):
        raise DBusException("org.bluez.Error.Rejected")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        if not self._allowed(device):
            self._reject()
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        if not self._allowed(device):
            self._reject()
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        if not self._allowed(device):
            self._reject()
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        if not self._allowed(device):
            self._reject()
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        if not self._allowed(device):
            self._reject()
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        if not self._allowed(device):
            self._reject()
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        if not self._allowed(device):
            self._reject()
        return None

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        return None


class BlueZBackend:
    """Blocking BlueZ facade used from the pairing session worker."""

    def __init__(self, bus=None):
        self._bus = bus
        self._manager = None
        self._agent_manager = None
        self._agent: Optional[_PairingAgent] = None
        self._agent_registered = False
        self._started_adapters: List[str] = []
        self._pairable_restore: Dict[str, bool] = {}

    # -- connection / helpers ------------------------------------------------

    def _ensure_bus(self):
        if self._bus is not None:
            return self._bus
        try:
            # The GTK main loop is installed by the UI process before this is
            # reached. DBusGMainLoop makes the agent's incoming method calls
            # dispatch while the worker blocks in Pair().
            from dbus.mainloop.glib import DBusGMainLoop

            DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
        except Exception as ex:
            raise BackendError(
                "Nao foi possivel conectar ao barramento do sistema: "
                + _dbus_error_text(ex)) from ex
        return self._bus

    def _ensure_manager(self):
        if self._manager is not None:
            return self._manager
        bus = self._ensure_bus()
        try:
            # BlueZ exposes two different objects: the standard ObjectManager
            # at "/" and the agent manager at "/org/bluez". RegisterAgent on
            # the root object produces "method ... doesn't exist".
            root = bus.get_object(BLUEZ, "/")
            self._manager = dbus.Interface(root, OBJECT_MANAGER_IFACE)
            agent_root = bus.get_object(BLUEZ, AGENT_MANAGER_PATH)
            self._agent_manager = dbus.Interface(
                agent_root, AGENT_MANAGER_IFACE)
        except Exception as ex:
            raise BackendError(
                "O servico Bluetooth (BlueZ) nao esta disponivel: "
                + _dbus_error_text(ex)) from ex
        return self._manager

    def _managed_objects(self) -> Dict[str, dict]:
        manager = self._ensure_manager()
        try:
            return manager.GetManagedObjects(timeout=10)
        except Exception as ex:
            raise BackendError(
                "Nao foi possivel consultar os dispositivos Bluetooth: "
                + _dbus_error_text(ex)) from ex

    def _adapter_paths(self, objects: Dict[str, dict]) -> List[str]:
        return [path for path, interfaces in objects.items()
                if ADAPTER_IFACE in interfaces]

    def _adapter_iface(self, path: str):
        bus = self._ensure_bus()
        return dbus.Interface(bus.get_object(BLUEZ, path), ADAPTER_IFACE)

    def _device_iface(self, path: str):
        bus = self._ensure_bus()
        return dbus.Interface(bus.get_object(BLUEZ, path), DEVICE_IFACE)

    def _device_props(self, path: str):
        bus = self._ensure_bus()
        obj = bus.get_object(BLUEZ, path)
        return dbus.Interface(obj, PROPERTIES_IFACE)

    # -- discovery / agent ---------------------------------------------------

    def begin_scan(self) -> None:
        objects = self._managed_objects()
        adapters = self._adapter_paths(objects)
        if not adapters:
            raise BackendError(
                "Nenhum adaptador Bluetooth encontrado. Verifique se o "
                "Bluetooth esta ligado.")
        self._started_adapters = []
        self._pairable_restore = {}
        # Power on adapters that are off. This is the one place where we
        # change adapter state: a hidden controller cannot be discovered
        # otherwise, and BlueZ reports a useful permission error if denied.
        for path in adapters:
            props = objects[path].get(ADAPTER_IFACE, {})
            if not _as_bool(props.get("Powered", False)):
                try:
                    self._adapter_iface(path).Set(
                        ADAPTER_IFACE, "Powered", dbus.Boolean(True),
                        timeout=10)
                except Exception as ex:
                    self._restore_pairable()
                    raise BackendError(
                        "Nao foi possivel ligar o adaptador Bluetooth: "
                        + _dbus_error_text(ex)) from ex
            # Pairing initiated by the host still needs a pairable adapter on
            # many BlueZ versions. Remember the old value so the scan does not
            # leave the workstation permanently pairable.
            if not _as_bool(props.get("Pairable", False)):
                try:
                    self._adapter_iface(path).Set(
                        ADAPTER_IFACE, "Pairable", dbus.Boolean(True),
                        timeout=10)
                    self._pairable_restore[path] = False
                except Exception:
                    pass
        # The agent must exist before Pair(); registering it before discovery
        # is harmless and keeps the controller's first pairing request from
        # racing a missing agent.
        try:
            self._register_agent()
        except Exception:
            self._restore_pairable()
            raise
        for path in adapters:
            try:
                self._adapter_iface(path).StartDiscovery(timeout=10)
                self._started_adapters.append(path)
            except DBusException as ex:
                name = ex.get_dbus_name() or ""
                # Another client (bluetoothctl, the desktop applet) already
                # owns discovery. BlueZ shares the device cache, so use it but
                # do not stop a scan we did not start.
                if name.endswith("InProgress") or name.endswith("AlreadyExists"):
                    continue
                self._stop_started_adapters()
                self._restore_pairable()
                self._unregister_agent()
                raise BackendError(
                    "Nao foi possivel iniciar a busca Bluetooth: "
                    + _dbus_error_text(ex)) from ex
            except Exception as ex:
                self._stop_started_adapters()
                self._restore_pairable()
                self._unregister_agent()
                raise BackendError(
                    "Nao foi possivel iniciar a busca Bluetooth: "
                    + _dbus_error_text(ex)) from ex

    def _register_agent(self) -> None:
        if self._agent_registered:
            return
        bus = self._ensure_bus()
        self._ensure_manager()
        self._agent = _PairingAgent(bus, AGENT_PATH)
        try:
            self._agent_manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput",
                                               timeout=10)
            self._agent_registered = True
            # Become the default so Pair() asks us, not a desktop agent.
            try:
                self._agent_manager.RequestDefaultAgent(AGENT_PATH, timeout=10)
            except Exception:
                # Registration is enough on some BlueZ versions; a desktop
                # agent may keep the default. Pairing then reports a useful
                # error instead of failing silently here.
                pass
        except Exception as ex:
            self._agent = None
            raise BackendError(
                "Nao foi possivel registrar o agente de pareamento: "
                + _dbus_error_text(ex)) from ex

    def _unregister_agent(self) -> None:
        if not self._agent_registered:
            return
        try:
            self._agent_manager.UnregisterAgent(AGENT_PATH, timeout=10)
        except Exception:
            pass
        self._agent_registered = False
        if self._agent is not None:
            try:
                self._agent.remove_from_connection()
            except Exception:
                pass
            self._agent = None

    def _stop_started_adapters(self) -> None:
        for path in list(self._started_adapters):
            try:
                self._adapter_iface(path).StopDiscovery(timeout=10)
            except Exception:
                pass
        self._started_adapters = []

    def _restore_pairable(self) -> None:
        for path, previous in list(self._pairable_restore.items()):
            try:
                self._adapter_iface(path).Set(
                    ADAPTER_IFACE, "Pairable", dbus.Boolean(previous),
                    timeout=10)
            except Exception:
                pass
        self._pairable_restore = {}

    def end_scan(self) -> None:
        self._stop_started_adapters()
        self._restore_pairable()
        self._unregister_agent()

    # -- device reads --------------------------------------------------------

    def devices(self) -> List[DeviceSnapshot]:
        objects = self._managed_objects()
        result = []
        for path, interfaces in objects.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue
            try:
                result.append(_device_from_props(str(path), props))
            except Exception:
                continue
        result.sort(key=lambda d: (not d.connected, not d.paired,
                                   d.name.lower(), d.address))
        return result

    def find_device(self, identity: str) -> Optional[DeviceSnapshot]:
        for device in self.devices():
            if device.identity == identity or device.path == identity:
                return device
        return None

    # -- device operations ---------------------------------------------------

    def _set_trusted(self, path: str) -> None:
        try:
            self._device_props(path).Set(
                DEVICE_IFACE, "Trusted", dbus.Boolean(True), timeout=10)
        except Exception as ex:
            raise BackendError(
                "Nao foi possivel confiar no controle: "
                + _dbus_error_text(ex)) from ex

    def _wait_connected(self, path: str, timeout: float = 6.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self._device_props(path).Get(DEVICE_IFACE,
                                                     "Connected", timeout=5)
                if _as_bool(value):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def pair(self, device: DeviceSnapshot) -> None:
        """Pair the device using the temporary agent, then wait for Paired."""
        if self._agent is not None:
            self._agent.set_candidate(device.path)
        try:
            self._device_iface(device.path).Pair(timeout=45)
        except DBusException as ex:
            name = ex.get_dbus_name() or ""
            if not name.endswith("AlreadyExists"):
                raise BackendError(
                    "Falha ao parear " + device.name + ": "
                    + _dbus_error_text(ex)) from ex
        except Exception as ex:
            raise BackendError(
                "Falha ao parear " + device.name + ": "
                + _dbus_error_text(ex)) from ex
        # Pair() normally blocks until authentication finishes, but some
        # adapters return early. Give BlueZ a short window to settle.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            try:
                props = self._device_props(device.path)
                if _as_bool(props.Get(DEVICE_IFACE, "Paired", timeout=5)):
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise BackendError(
            "O controle nao confirmou o pareamento. Coloque-o em modo de "
            "pareamento e tente novamente.")

    def connect(self, device: DeviceSnapshot) -> None:
        try:
            self._device_iface(device.path).Connect(timeout=30)
        except DBusException as ex:
            name = ex.get_dbus_name() or ""
            if not name.endswith("AlreadyConnected"):
                raise BackendError(
                    "Falha ao conectar " + device.name + ": "
                    + _dbus_error_text(ex)) from ex
        except Exception as ex:
            raise BackendError(
                "Falha ao conectar " + device.name + ": "
                + _dbus_error_text(ex)) from ex
        if not self._wait_connected(device.path):
            raise BackendError(
                "O controle nao ficou disponivel apos a conexao. Ligue-o "
                "novamente e tente de novo.")

    def set_trusted(self, device: DeviceSnapshot) -> None:
        self._set_trusted(device.path)

    def forget(self, device: DeviceSnapshot) -> None:
        """Best-effort disconnect used when the GUI is closing mid-pair."""
        try:
            self._device_iface(device.path).Disconnect(timeout=5)
        except Exception:
            pass


def format_dbus_error(ex: Exception) -> str:
    """Small helper shared with the UI for non-BlueZ integration errors."""
    return _dbus_error_text(ex)
