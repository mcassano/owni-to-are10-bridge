#!/usr/bin/env python3
"""Capture the real are10's BLE advertisement and GATT table.

This is the blueprint the emulator has to match: the advertisement fields
Dome Tee Nero scans for, plus every service/characteristic it may discover.

Usage: .venv/bin/python tools/recon_are10.py [--out local/are10_profile.json] [--timeout 15]
Power on the are10 and make sure no phone is currently connected to it.
"""

import argparse
import asyncio
import json
import sys

from bleak import BleakClient, BleakScanner

COMPANY_ID = 0x0087
# Vendor chars require an encrypted (bonded) link; reading one unbonded
# makes the are10 drop the connection, so they're skipped.
VENDOR_UUID_SUFFIX = "-667b-11e3-949a-0800200c9a66"


def looks_like_are10(device, adv):
    return COMPANY_ID in adv.manufacturer_data


async def find_are10(timeout):
    print(f"Scanning {timeout}s for are10...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for device, adv in found.values():
        if looks_like_are10(device, adv):
            return device, adv
    return None, None


async def dump_gatt(device):
    services = []
    async with BleakClient(device) as client:
        print(f"Connected, MTU={client.mtu_size}")
        for svc in client.services:
            s = {"uuid": svc.uuid, "description": svc.description, "characteristics": []}
            for ch in svc.characteristics:
                c = {
                    "uuid": ch.uuid,
                    "handle": ch.handle,
                    "properties": ch.properties,
                    "descriptors": [{"uuid": d.uuid, "handle": d.handle} for d in ch.descriptors],
                }
                if "read" in ch.properties and not ch.uuid.endswith(VENDOR_UUID_SUFFIX):
                    try:
                        value = await client.read_gatt_char(ch)
                        c["value_hex"] = value.hex()
                        if value.isascii() and value.decode().isprintable():
                            c["value_text"] = value.decode()
                    except Exception as e:  # encrypted/auth-required chars land here
                        c["read_error"] = str(e)
                s["characteristics"].append(c)
            services.append(s)
        return client.mtu_size, services


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="local/are10_profile.json")
    ap.add_argument("--timeout", type=float, default=15)
    args = ap.parse_args()

    device, adv = await find_are10(args.timeout)
    if device is None:
        sys.exit("No are10 found. Is it on, and disconnected from your phone?")

    profile = {
        "address": device.address,  # macOS reports a per-host UUID, not the MAC
        "advertisement": {
            "local_name": adv.local_name,
            "rssi": adv.rssi,
            "tx_power": adv.tx_power,
            "manufacturer_data": {f"0x{k:04X}": v.hex() for k, v in adv.manufacturer_data.items()},
            "service_uuids": adv.service_uuids,
            "service_data": {k: v.hex() for k, v in adv.service_data.items()},
        },
    }
    print(json.dumps(profile["advertisement"], indent=2))

    profile["mtu"], profile["services"] = await dump_gatt(device)
    for s in profile["services"]:
        print(f"\nService {s['uuid']} ({s['description']})")
        for c in s["characteristics"]:
            extra = c.get("value_text") or c.get("value_hex") or c.get("read_error") or ""
            print(f"  {c['uuid']}  {','.join(c['properties']):<32} {extra}")

    with open(args.out, "w") as f:
        json.dump(profile, f, indent=2)
    print(f"\nSaved {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
