"""Read-only serial metadata for the USB connection picker."""
import re


def usb_port_choices(ports):
    """List ordinary COM ports, excluding known wireless virtual services.

    Some USB drivers provide only a COM name, so an unknown transport remains
    selectable. This function opens no device and performs no discovery.
    """
    found = set()
    for port in ports:
        device = str(getattr(port, "device", "") or "").upper()
        if not re.fullmatch(r"COM[1-9]\d*", device):
            continue
        metadata = " ".join(str(getattr(port, field, "") or "")
                            for field in ("hwid", "description", "manufacturer"))
        if re.search(r"BTHENUM|BTHMODEM|RFCOMM|BLUETOOTH|БЛЮТУЗ", metadata, re.I):
            continue
        found.add(device)
    return sorted(found, key=lambda name: int(name[3:]))
