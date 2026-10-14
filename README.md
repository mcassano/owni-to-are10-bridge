# owni-to-are10-bridge

Feed an Owni launch monitor into Dome Tee Nero, which only accepts an are10, using a Raspberry Pi 4.
Two independent pieces; they're not connected to each other yet.

- **are10 emulator** (`are10emu`). The Pi presents itself to Dome Tee Nero on iOS as an
  are10, and sends it shots that arrive on a GSPro OpenConnect TCP port (921).
  **Works:** pairing, sync and shots on a course.
- **Owni logger** (`owni-logger`). The Pi connects to the Owni whenever it's on
  and logs every shot. It runs on a USB Bluetooth adapter (TP-Link UB500 Plus); the emulator
  keeps the Pi's built-in radio.

```
Owni ──BLE──► Pi: owni-logger ──(not yet)──► Pi: are10emu ──BLE──► Dome Tee Nero (iOS)
                                          TCP:921 ◄── are10shot (test shots)
```

## Layout

| Path | What |
|---|---|
| `are10emu/wire.py` | CRC-16/ARC, COBS, GFDI frames, MultiLink control messages |
| `are10emu/pb.py` | Minimal protobuf codec for the are10's Smart messages |
| `are10emu/device.py` | are10 device-side protocol state machine (transport-agnostic) |
| `are10emu/bluez.py` | BlueZ GATT server, advertisement and pairing agent |
| `are10emu/shots.py` | Shot model plus GSPro OpenConnect conversion |
| `are10_emulator.py` | are10 emulator entry point |
| `send_shot.py` | Test shots (installed on the Pi as `are10shot`) |
| `ownilog/protocol.py` | Owni commands and notification parsing |
| `ownilog/central.py` | BlueZ GATT client for the Owni |
| `owni_logger.py` | Owni logger entry point |
| `deploy/` | Pi install scripts and systemd units |
| `tests/` | Unit tests for both pieces |
| `tools/pklg_att.py`, `tools/pklg_gfdi.py` | Decode PacketLogger traces: ATT/SMP, then MultiLink/GFDI |
| `tools/replay_trace.py` | Replay a trace's phone writes into the emulator and diff against the real are10 |
| `tools/hcimon.py` | HCI capture on the Pi (btmon segfaults there) |
| `tools/recon_are10.py`, `tools/probe_are10.py` | One-off are10 recon (GATT dump; replay against the real device) |
| `local/` | **Not in git.** The device identity (see below), profile dumps, third-party protocol docs, a client-library test harness |
| `captures/` | **Not in git.** PacketLogger traces and session logs; they contain pairing keys |

## Device identity (`local/`)

The emulator reports the real device's names and serves its device description file. Those come
from your own hardware and live outside the repo:

- `local/identity.json`: `local_name`, `manufacturer`, `model`, `serial`, `firmware`, `unit_id`,
  `software_version`, `product_number`, `friendly_name`, `device_name` (see `Identity` in
  `are10emu/device.py`). Get them from `tools/recon_are10.py` (advertisement and Device
  Information) and from a PacketLogger capture of the app syncing with the real device (5024 device info).
- `local/device.xml`: the file the real device serves as file `0xFFFD`, taken from that capture.

The unit tests use placeholder values; the replay tool and the Pi install need `local/`.

## Testing on the Mac (no hardware needed)

```sh
.venv/bin/python -m unittest discover -s tests -v
# replay a capture of the app and the real device (ADDR = the real device's BLE address)
python tools/pklg_att.py captures/dtn_are10.pklg "$ADDR" > /tmp/att.txt && python tools/replay_trace.py /tmp/att.txt --until 16.5
```

## Pi setup

You need Raspberry Pi OS Bookworm or later. The Pi is on Wi-Fi only, and its SD card is 4 GB, so watch disk space.

```sh
PI=pi@raspberrypi.local   # your Pi
rsync -a --exclude .venv --exclude .git --exclude captures --exclude local/reference --exclude 'local/*.bundle' \
    --exclude local/client-harness ./ "$PI":bridge/
ssh "$PI" 'cd ~/bridge && sudo ./deploy/install.sh'                       # are10 emulator + are10shot
ssh "$PI" 'cd ~/bridge && sudo ./deploy/install-owni-logger.sh --enable'  # Owni logger
```

`install.sh` copies the emulator and `local/identity.json` + `device.xml` to `/opt/are10emu` and installs dbus-fast. It also sets BlueZ to
LE-only with `JustWorksRepairing = always` and `DeviceID = false`, and starts the `are10emu`
service. The original BlueZ config is kept as `/etc/bluetooth/main.conf.are10emu-orig`.

Both services pick their radio by Bluetooth address (`Environment=ADAPTER=` in
`deploy/*.service`), because `hci0`/`hci1` swap between boots once a USB adapter is plugged in.
Set them to your own radios (`bluetoothctl list`). The emulator must stay on the built-in radio,
since the iPhone is bonded to that address. The logger clears the USB adapter's RF-kill block itself: Raspberry Pi OS starts a
USB adapter blocked the first time it's seen in a USB port.

Logs: `journalctl -u are10emu -f` and `journalctl -u owni-logger -f`. To run either one by hand
with per-frame logging, stop its service and run it with `-v`.

## Using the are10 emulator

1. Turn the real are10 off.
2. The first time, forget the device in iOS Settings → Bluetooth and in Dome Tee Nero. Then connect from Dome Tee Nero and accept the pairing prompt.
3. On the Pi, send a shot with `are10shot`. Variants: `are10shot 7iron`, `are10shot wedge`, `are10shot --speed 160 --axis -5`.
   HLA negative = left; spin axis positive = fade.

## Status and open items

**are10 emulator.** Matches the real are10's handshake byte for byte (38/38 messages when replayed
against `captures/dtn_are10.pklg`).
- **"Sync failed" in the app.** After sync, the app pushes a ~2 MB firmware file. The emulator
  declines it (create status UNSUPPORTED), and the app reports the sync as failed. Shots still work.
- **Two apps share the device.** Client `4` and the app's own client (an ASCII id) use separate channels, so close-all
  only closes the requesting client's handles. The app's own client also sends a few messages that aren't
  mapped yet: service 4 `03`, protobuf field 13, message 5045. None of them block shots.
- **Swing tempo.** SwingMetrics are omitted because the Owni doesn't measure them.

**Owni logger.** It connected, armed the Owni, and logged a putt end to end. Shots go to
`/var/lib/owni-logger/shots.jsonl` in GSPro convention (spin axis negated from the Owni's raw
value), with the raw values kept alongside.
- **Use a USB adapter.** On the built-in radio, link setup fails about half the time (LE Read
  Remote Features → `0x3e`, likely Wi-Fi/Bluetooth coexistence). That radio also can't connect
  out to the Owni while the iPhone is connected to the emulator. On the UB500 Plus it connects
  on the first try.
- **The Owni takes 10–20 s to advertise again after a disconnect.** The logger keeps scanning
  until it reappears.
- **Club selection is fixed at startup** (`--club`, default driver). Selecting the putter is the
  Owni's putting mode.
- **BlueZ quirks for the Owni:** set `Trusted` first; keep `Connect()` pending, because sending it
  with no-reply makes BlueZ drop the link; register a pairing agent in the logger's process.
