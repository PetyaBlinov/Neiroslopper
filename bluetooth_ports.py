"""Classify Windows SPP ports from pySerial metadata, without opening devices.

Windows exposes paired Classic Bluetooth/RFCOMM services as virtual COM ports.
Enumeration does not prove that a board is powered, running FluidNC, or that a
port is outgoing: several Windows drivers omit direction and device names.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class BluetoothPort:
    device: str
    label: str
    detected: bool


def is_bluetooth_port(port):
    metadata = " ".join(str(getattr(port, field, "") or "")
                        for field in ("hwid", "description", "manufacturer"))
    return bool(re.search(r"BTHENUM|BTHMODEM|RFCOMM|BLUETOOTH|БЛЮТУЗ", metadata, re.I))


def bluetooth_choices(ports):
    """Return detected BT ports first and honest, unselected fallback COMs.

    Known USB adapters are excluded. Explicit incoming ports are excluded too;
    unspecified direction is left to the user's Windows COM-port settings.
    No registry scan, radio discovery, pairing, connection, or I/O takes place.
    """
    found = {}
    for port in ports:
        device = str(getattr(port, "device", "") or "").upper()
        if not re.fullmatch(r"COM[1-9]\d*", device):
            continue
        detected = is_bluetooth_port(port)
        description = " ".join(str(getattr(port, "description", "") or "").split())
        hardware = str(getattr(port, "hwid", "") or "").upper()
        if detected:
            if re.search(r"\bINCOMING\b|ВХОДЯЩ", description, re.I):
                continue
            clean = re.sub(r"\s*\(" + re.escape(device) + r"\)\s*", " ", description, flags=re.I).strip()
            label = device + " · " + (clean[:100] if clean and clean.casefold() != "n/a" else "Bluetooth")
        else:
            if (getattr(port, "vid", None) is not None or hardware.startswith(("USB", "FTDIBUS")) or
                    re.search(r"USB|CH340|CH341|CP210|FTDI", description, re.I)):
                continue
            label = device + " · тип порта не определён"
        candidate = BluetoothPort(device, label, detected)
        if device not in found or (detected and not found[device].detected):
            found[device] = candidate
    return sorted(found.values(), key=lambda item: (not item.detected, int(item.device[3:])))
