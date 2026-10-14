"""BlueZ peripheral: GATT table, advertisement, and pairing agent for the are10.

Mirrors are10_profile.json (captured from the real are10):
  * advertisement: pairing-mode format (see Advertisement)
  * MultiLink 6A4E2800 with config 2803 and channel pairs 2810/2820..2812/2822
  * Battery 180F, Device Information 180A, vendor 6A4E3400 (logged only)

Linux only; requires dbus-fast and BlueZ >= 5.56.
"""

import asyncio
import logging
import struct
from typing import Callable, Dict, List, Optional

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from .device import Are10Device

log = logging.getLogger("are10emu.bluez")

BLUEZ = "org.bluez"
APP_PATH = "/com/are10emu"
ADV_PATH = APP_PATH + "/adv"
AGENT_PATH = APP_PATH + "/agent"

COMPANY_ID = 0x0087
PRODUCT_BYTES = bytes.fromhex("0e26")
SERVICE_UUID16 = 0xFE1F
SERVICE_DATA = bytes.fromhex("000104")


def vendor_uuid(short: int) -> str:
    return f"6a4e{short:04x}-667b-11e3-949a-0800200c9a66"


def sig_uuid(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# Vendor chars refuse unencrypted access, like the real are10 ("Insufficient Encryption").
ENC_RW = ["encrypt-read", "encrypt-write"]
# The are10 also rejects unencrypted CCCD writes (ATT error 0x05), which makes iOS pair
# *before* the app sends anything. Without this, the app's first MultiLink write goes
# out unencrypted and BlueZ silently drops it.
ENC_NOTIFY = ["encrypt-notify"]


class Service(ServiceInterface):
    def __init__(self, path: str, uuid: str):
        super().__init__("org.bluez.GattService1")
        self.path, self.uuid = path, uuid
        self.chars: List["Characteristic"] = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    def managed(self):
        return {"UUID": Variant("s", self.uuid), "Primary": Variant("b", True)}


class Characteristic(ServiceInterface):
    def __init__(self, service: Service, index: int, uuid: str, flags: List[str],
                 value: bytes = b"", on_write: Optional[Callable[[bytes, dict], None]] = None):
        super().__init__("org.bluez.GattCharacteristic1")
        self.service = service
        self.path = f"{service.path}/char{index}"
        self.uuid, self.flags = uuid, flags
        self._value = value
        self.on_write = on_write
        self.notifying = False
        service.chars.append(self)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service.path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        offset = options["offset"].value if "offset" in options else 0
        log.info("read %s @%d → %s", self.uuid[:8], offset, self._value[offset:].hex())
        return self._value[offset:]

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        value = bytes(value)
        log.debug("write %s (%s): %s", self.uuid[:8], options.get("type", Variant("s", "request")).value,
                  value.hex())
        if self.on_write:
            self.on_write(value, {k: v.value for k, v in options.items()})
        else:
            log.info("write to %s: %s", self.uuid[:8], value.hex())

    @method()
    def StartNotify(self):
        log.info("notify on %s", self.uuid[:8])
        self.notifying = True

    @method()
    def StopNotify(self):
        log.info("notify off %s", self.uuid[:8])
        self.notifying = False

    def notify(self, value: bytes):
        self._value = value
        if not self.notifying:
            log.warning("dropping notification on %s (not subscribed): %s", self.uuid[:8],
                        value.hex())
            return
        self.emit_properties_changed({"Value": value})

    def managed(self):
        return {"UUID": Variant("s", self.uuid), "Service": Variant("o", self.service.path),
                "Flags": Variant("as", self.flags)}


class Application(ServiceInterface):
    """ObjectManager root that BlueZ queries for our services."""

    def __init__(self):
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self.services: List[Service] = []

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        objects = {}
        for s in self.services:
            objects[s.path] = {"org.bluez.GattService1": s.managed()}
            for c in s.chars:
                objects[c.path] = {"org.bluez.GattCharacteristic1": c.managed()}
        return objects


class Advertisement(ServiceInterface):
    """Pairing-mode advertisement, as captured from the real are10 (solid blue LED):

    ADV:      flags 0x06, UUID16 FE1F, mfg 0087 → 0e26 01 <unit_id u32 LE>
    SCAN_RSP: service data FE1F → 000104, name (identity.local_name)

    BlueZ decides the ADV/SCAN_RSP split itself; iOS merges both, so apps
    can't tell the difference.
    """

    def __init__(self, unit_id: int, local_name: str):
        super().__init__("org.bluez.LEAdvertisement1")
        self.local_name = local_name
        self.mfg_data = PRODUCT_BYTES + b"\x01" + struct.pack("<I", unit_id)

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [sig_uuid(SERVICE_UUID16)]

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        return {COMPANY_ID: Variant("ay", self.mfg_data)}

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":
        return {sig_uuid(SERVICE_UUID16): Variant("ay", SERVICE_DATA)}

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.local_name

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @method()
    def Release(self):
        log.info("advertisement released")


class Agent(ServiceInterface):
    """NoInputNoOutput agent: accepts Just Works pairing like the are10 (no screen)."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        log.info("pairing confirmation for %s (auto-accepted)", device)

    @method()
    def RequestAuthorization(self, device: "o"):
        log.info("pairing authorization for %s (auto-accepted)", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def Cancel(self):
        log.info("pairing cancelled")


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


class Are10Peripheral:
    def __init__(self, device: Are10Device, adapter: str = "hci0", battery: int = 100):
        self.device = device
        self.adapter = adapter
        self.adapter_path = ""
        self.host: Optional[str] = None  # device path of the app that's writing to us
        self.app = Application()
        self.channels: List[Characteristic] = []
        ident = device.identity
        self._build_gatt(battery, {0x2A29: ident.manufacturer, 0x2A24: ident.model,
                                   0x2A25: ident.serial, 0x2A28: ident.firmware})
        device.notify = self._notify

    def _build_gatt(self, battery: int, dis: Dict[int, str]):
        ml = Service(APP_PATH + "/ml", vendor_uuid(0x2800))
        Characteristic(ml, 0, vendor_uuid(0x2803),
                       ["read", "write", "write-without-response"] + ENC_RW, bytes(4))
        for ch in range(3):
            rx = Characteristic(ml, 1 + ch * 2, vendor_uuid(0x2810 + ch),
                                ["read", "write", "write-without-response", "notify"]
                                + ENC_RW + ENC_NOTIFY,
                                on_write=self._writer(ch))
            Characteristic(ml, 2 + ch * 2, vendor_uuid(0x2820 + ch),
                           ["write", "write-without-response", "encrypt-write"],
                           on_write=self._writer(ch))
            self.channels.append(rx)

        bas = Service(APP_PATH + "/bas", sig_uuid(0x180F))
        Characteristic(bas, 0, sig_uuid(0x2A19), ["read", "notify"], bytes([battery]))

        dev_info = Service(APP_PATH + "/dis", sig_uuid(0x180A))
        for i, (short, text) in enumerate(dis.items()):
            Characteristic(dev_info, i, sig_uuid(short), ["read"], text.encode())

        g34 = Service(APP_PATH + "/g34", vendor_uuid(0x3400))
        Characteristic(g34, 0, vendor_uuid(0x3401), ["notify"])
        Characteristic(g34, 1, vendor_uuid(0x3403), ["read", "notify"])
        Characteristic(g34, 2, vendor_uuid(0x3402), ["write", "indicate"])

        self.app.services = [ml, bas, dev_info, g34]

    def _writer(self, channel: int):
        def on_write(value: bytes, options: dict):
            host = options.get("device")
            if host is not None and host != self.host:
                log.info("host is %s", host)
                self.host = host
            if "mtu" in options:
                self.device.set_mtu(options["mtu"])
            log.debug("ch%d ← %s", channel, value.hex())
            self.device.on_write(channel, value)
        return on_write

    def _notify(self, channel: int, data: bytes):
        log.debug("ch%d → %s", channel, data.hex())
        self.channels[channel].notify(data)

    async def _iface(self, path: str, name: str):
        introspection = await self.bus.introspect(BLUEZ, path)
        return self.bus.get_proxy_object(BLUEZ, path, introspection).get_interface(name)

    def _on_signal(self, msg: Message):
        if (msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged"
                or not msg.body or msg.body[0] != "org.bluez.Device1"
                or not msg.path.startswith(self.adapter_path + "/")):
            return
        connected = msg.body[1].get("Connected")
        if connected is None:
            return
        if connected.value:
            log.info("host connected: %s", msg.path)
        elif self.host is not None and msg.path != self.host:
            # Another phone or tablet (still remembering the emulator) dropping its own
            # link; the app session belongs to self.host, so leave it alone.
            log.info("other device disconnected: %s", msg.path)
        else:
            log.info("host disconnected: %s", msg.path)
            self.host = None
            self.device.on_disconnect()
            for c in self.channels:
                c.notifying = False

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.adapter_path = await find_adapter(self.bus, self.adapter)

        props = await self._iface(self.adapter_path, "org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await props.call_set("org.bluez.Adapter1", "Alias", Variant("s", self.device.identity.local_name))
        await props.call_set("org.bluez.Adapter1", "Pairable", Variant("b", True))

        self.bus.export(AGENT_PATH, Agent())
        agents = await self._iface("/org/bluez", "org.bluez.AgentManager1")
        await agents.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await agents.call_request_default_agent(AGENT_PATH)

        self.bus.export(APP_PATH, self.app)
        for s in self.app.services:
            self.bus.export(s.path, s)
            for c in s.chars:
                self.bus.export(c.path, c)
        gatt = await self._iface(self.adapter_path, "org.bluez.GattManager1")
        await gatt.call_register_application(APP_PATH, {})

        self.bus.export(ADV_PATH, Advertisement(self.device.identity.unit_id,
                                                   self.device.identity.local_name))
        adv = await self._iface(self.adapter_path, "org.bluez.LEAdvertisingManager1")
        await adv.call_register_advertisement(ADV_PATH, {})

        await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=["type='signal',interface='org.freedesktop.DBus.Properties',"
                  "member='PropertiesChanged',arg0='org.bluez.Device1'"]))
        self.bus.add_message_handler(self._on_signal)
        log.info("advertising as %r on %s", self.device.identity.local_name, self.adapter_path)
