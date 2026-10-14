"""BlueZ (D-Bus) GATT client for the Owni.

Two BlueZ quirks:
  1. Set Device1.Trusted before connecting, or BlueZ treats the unpaired
     device as temporary and drops it after a few seconds.
  2. Device1.Connect() may never send a D-Bus reply for the Owni, so the call is
     left pending in a background task and ServicesResolved is polled instead.
     (Sending it with NO_REPLY_EXPECTED makes BlueZ 5.66 on
     the Pi drop the link ~3 s later with "terminated by local host".)
The Owni is never paired or bonded.
"""

import asyncio
import glob
import logging
import os
from typing import Callable, Optional

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from . import protocol

log = logging.getLogger("ownilog.central")

BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
CHAR = "org.bluez.GattCharacteristic1"
PROPS = "org.freedesktop.DBus.Properties"
SETTLE = 0.25


class LinkLost(Exception):
    pass


AGENT_PATH = "/com/ownilog/agent"


class Agent(ServiceInterface):
    """NoInputNoOutput agent for this process.

    BlueZ uses the calling client's agent for security it starts during
    Connect(). Without one, the attempt fails and BlueZ aborts the link
    (att_connect_cb: "Function not implemented"). The Owni itself never pairs.
    """

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        pass

    @method()
    def RequestAuthorization(self, device: "o"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def Cancel(self):
        pass


async def find_adapter(bus: MessageBus, adapter: str, wait: float = 30.0) -> str:
    """Object path for "hci0" or a controller address ("E4:5F:01:70:45:CE").

    hciN numbering follows USB enumeration order, so it can swap between boots
    once a USB adapter is plugged in; an address always names the same radio.
    Waits for the adapter to appear, since USB ones register late at boot.
    """
    if ":" not in adapter:
        return f"/org/bluez/{adapter}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        reply = await bus.call(Message(destination=BLUEZ, path="/",
                                       interface="org.freedesktop.DBus.ObjectManager",
                                       member="GetManagedObjects"))
        for path, ifaces in reply.body[0].items():
            props = ifaces.get("org.bluez.Adapter1")
            if props and props["Address"].value.upper() == adapter.upper():
                return path
        if loop.time() > deadline:
            raise RuntimeError(f"no Bluetooth adapter with address {adapter}")
        await asyncio.sleep(1)


def unblock_rfkill(hci: str):
    """Clear the adapter's soft RF-kill block.

    systemd-rfkill saves block state per port, and on Raspberry Pi OS a USB
    adapter plugged into a new port comes up blocked, so Powered=true fails.
    """
    for soft in glob.glob("/sys/class/rfkill/rfkill*/soft"):
        base = os.path.dirname(soft)
        try:
            with open(os.path.join(base, "name")) as f:
                if f.read().strip() != hci:
                    continue
            with open(soft) as f:
                if f.read().strip() == "0":
                    return
            with open(soft, "w") as f:
                f.write("0")
            log.info("cleared RF-kill block on %s", hci)
        except OSError as e:
            log.warning("could not clear RF-kill on %s: %s", hci, e)


class OwniLink:
    def __init__(self, adapter: str = "hci0"):
        self.adapter = adapter
        self.adapter_path = ""
        self.bus: Optional[MessageBus] = None
        self.device_path: Optional[str] = None
        self.cmd_path: Optional[str] = None
        self.evt_path: Optional[str] = None
        self.connected = False
        self.on_notify: Callable[[bytes], None] = lambda data: None
        self._write_lock = asyncio.Lock()
        self._connect_call: Optional[asyncio.Task] = None

    # ── D-Bus helpers ──

    async def _call(self, path, interface, member, signature="", body=None, flags=None):
        msg = Message(destination=BLUEZ, path=path, interface=interface, member=member,
                      signature=signature, body=body or [], flags=flags or MessageFlag.NONE)
        if flags == MessageFlag.NO_REPLY_EXPECTED:
            await self.bus.send(msg)
            return None
        reply = await self.bus.call(msg)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {reply.body}")
        return reply.body

    async def _objects(self) -> dict:
        (objs,) = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return objs

    async def _get(self, path, interface, name):
        (value,) = await self._call(path, PROPS, "Get", "ss", [interface, name])
        return value.value

    async def _set(self, path, interface, name, variant):
        await self._call(path, PROPS, "Set", "ssv", [interface, name, variant])

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.adapter_path = await find_adapter(self.bus, self.adapter)
        await self._power_on()
        self.bus.add_message_handler(self._on_message)
        # Not the default agent (the are10 emulator may own that); BlueZ still uses
        # it for operations this process starts.
        self.bus.export(AGENT_PATH, Agent())
        await self._call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                         [AGENT_PATH, "NoInputNoOutput"])

    async def _power_on(self, timeout: float = 10.0):
        # BlueZ may already be powering the adapter after the RF-kill unblock
        # (Set then fails with Busy), so retry until Powered reads true.
        unblock_rfkill(self.adapter_path.rsplit("/", 1)[-1])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await self._get(self.adapter_path, "org.bluez.Adapter1", "Powered"):
            try:
                await self._set(self.adapter_path, "org.bluez.Adapter1", "Powered", Variant("b", True))
            except RuntimeError:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.5)

    # ── discovery ──

    async def is_advertising(self, device_path: str) -> bool:
        """True if BlueZ has a recent advertisement (RSSI) for the device."""
        try:
            await self._get(device_path, DEVICE, "RSSI")
            return True
        except RuntimeError:
            return False

    @staticmethod
    def _is_owni(props: dict) -> Optional[str]:
        name = props.get("Name", Variant("s", "")).value or props.get("Alias", Variant("s", "")).value
        if name.startswith(protocol.GAP_NAME_PREFIX):  # cached GAP name, after a first connection
            return name
        # Advertisement: manufacturer data under the reserved test company ID,
        # holding an ASCII model code. Neither part alone is specific enough.
        mfg = props.get("ManufacturerData")
        if mfg is not None and 0xFFFF in mfg.value:
            payload = bytes(mfg.value[0xFFFF].value)
            if payload.startswith(protocol.MODEL_CODE_PREFIX):  # e.g. "0300A"
                return name or f"Owni({payload.decode(errors='replace')})"
        return None

    async def find(self, scan_seconds: float = 10.0) -> Optional[str]:
        """Scan up to scan_seconds; return the Owni's device path, or None."""
        await self._call(self.adapter_path, "org.bluez.Adapter1", "SetDiscoveryFilter", "a{sv}",
                         [{"Transport": Variant("s", "le")}])
        await self._call(self.adapter_path, "org.bluez.Adapter1", "StartDiscovery")
        found = None
        try:
            deadline = asyncio.get_running_loop().time() + scan_seconds
            while found is None and asyncio.get_running_loop().time() < deadline:
                for path, ifaces in (await self._objects()).items():
                    props = ifaces.get(DEVICE)
                    if props and path.startswith(self.adapter_path + "/") and "RSSI" in props:
                        name = self._is_owni(props)
                        if name:
                            found = path
                            log.info("found %s at %s (RSSI %s)", name, path, props["RSSI"].value)
                            break
                if found is None:
                    await asyncio.sleep(0.5)
        finally:
            # Connecting while discovery is active hangs BlueZ.
            try:
                await self._call(self.adapter_path, "org.bluez.Adapter1", "StopDiscovery")
            except RuntimeError:
                pass
            await asyncio.sleep(SETTLE)
        return found

    # ── connection ──

    async def connect(self, device_path: str, timeout: float = 10.0):
        self.device_path = device_path
        await self._set(device_path, DEVICE, "Trusted", Variant("b", True))
        await self._add_match(f"type='signal',sender='{BLUEZ}',interface='{PROPS}',"
                              f"member='PropertiesChanged',path_namespace='{device_path}'")
        # Keep the Connect() call outstanding; BlueZ keeps the link while a caller waits.
        self._connect_call = asyncio.get_running_loop().create_task(
            self._call(device_path, DEVICE, "Connect"))

        # The Pi 4's link setup to the Owni is flaky (LE Read Remote Features fails
        # with 0x3e about half the time), so fail fast and let the caller retry.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        was_connected = False
        while loop.time() < deadline:
            if self._connect_call.done() and self._connect_call.exception() is not None:
                raise LinkLost(f"connect failed: {self._connect_call.exception()}")
            if await self._get(device_path, DEVICE, "ServicesResolved"):
                break
            connected = await self._get(device_path, DEVICE, "Connected")
            if was_connected and not connected:
                raise LinkLost("link dropped during service discovery")
            was_connected = was_connected or connected
            await asyncio.sleep(0.1)
        else:
            raise LinkLost("services never resolved")
        self.connected = True
        await asyncio.sleep(SETTLE)

        for attempt in range(4):  # discovery can complete with no characteristics; retry
            for path, ifaces in (await self._objects()).items():
                ch = ifaces.get(CHAR)
                if ch and path.startswith(device_path):
                    uuid = ch["UUID"].value.lower()
                    if uuid == protocol.CMD_UUID:
                        self.cmd_path = path
                    elif uuid == protocol.EVT_UUID:
                        self.evt_path = path
            if self.cmd_path and self.evt_path:
                break
            await asyncio.sleep(0.5)
        else:
            raise LinkLost("Owni characteristics not found")

        await asyncio.sleep(SETTLE)
        await self._call(self.evt_path, CHAR, "StartNotify")
        log.info("connected; notifications on")

    async def read_text(self, uuid: str) -> Optional[str]:
        for path, ifaces in (await self._objects()).items():
            ch = ifaces.get(CHAR)
            if ch and path.startswith(self.device_path) and ch["UUID"].value.lower() == uuid:
                (value,) = await self._call(path, CHAR, "ReadValue", "a{sv}", [{}])
                return bytes(value).decode(errors="replace")
        return None

    async def write(self, data: bytes):
        if not self.connected:
            raise LinkLost("not connected")
        async with self._write_lock:
            await self._call(self.cmd_path, CHAR, "WriteValue", "aya{sv}",
                             [data, {"type": Variant("s", "request")}])

    async def disconnect(self):
        if self._connect_call is not None:
            if self._connect_call.done():
                self._connect_call.exception()  # mark retrieved
            else:
                self._connect_call.cancel()
            self._connect_call = None
        if self.device_path and self.connected:
            self.connected = False
            try:
                await asyncio.wait_for(self._call(self.device_path, DEVICE, "Disconnect"), 5)
            except Exception as e:  # already gone
                log.debug("disconnect: %s", e)
        self.cmd_path = self.evt_path = None

    # ── signals ──

    async def _add_match(self, rule: str):
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member="AddMatch",
                                    signature="s", body=[rule]))

    def _on_message(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged":
            return
        iface, changed = msg.body[0], msg.body[1]
        if msg.path == self.evt_path and iface == CHAR and "Value" in changed:
            self.on_notify(bytes(changed["Value"].value))
        elif msg.path == self.device_path and iface == DEVICE and "Connected" in changed:
            if not changed["Connected"].value and self.connected:
                log.info("link lost")
                self.connected = False
