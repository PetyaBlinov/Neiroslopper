"""Local hints for an explicit Wi-Fi search; never opens a connection.

Call remember_verified_endpoint only after the user's chosen board has
successfully connected. Saved numeric peers are discovery hints, not proof
that the same FluidNC board is still reachable at that address.
"""
import ipaddress
import json
from pathlib import Path

from network_transport import parse_tcp_endpoint


MAX_REMEMBERED_ENDPOINTS = 8
_MAX_FILE_SIZE = 16384
_FILENAME = "connection-settings.json"


def _numeric_endpoint(value):
    endpoint = parse_tcp_endpoint(value)
    # Store the peer that was actually verified, not an unresolved hostname.
    address = ipaddress.ip_address(endpoint.host)
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"tcp://{host}:{endpoint.port}"


def load_verified_endpoints(directory):
    """Return at most eight sanitized hints without searching or connecting."""
    if directory is None:
        return []
    try:
        with (Path(directory) / _FILENAME).open(encoding="utf-8") as stream:
            text = stream.read(_MAX_FILE_SIZE + 1)
        if len(text) > _MAX_FILE_SIZE:
            return []
        value = json.loads(text)
    except (OSError, ValueError, UnicodeError):
        return []
    if (not isinstance(value, dict) or type(value.get("version")) is not int or
            value["version"] != 1 or not isinstance(value.get("wifi_endpoints"), list)):
        return []
    endpoints = []
    for item in value["wifi_endpoints"]:
        try:
            endpoint = _numeric_endpoint(item)
        except ValueError:
            continue
        if endpoint not in endpoints:
            endpoints.append(endpoint)
        if len(endpoints) == MAX_REMEMBERED_ENDPOINTS:
            break
    return endpoints


def remember_verified_endpoint(directory, endpoint):
    """Remember one explicitly selected, successfully connected numeric peer.

    Only the endpoint is persisted: no Wi-Fi password, device credentials or
    command is stored. Loading this file never selects or connects a board.
    """
    if directory is None:
        return
    endpoint = _numeric_endpoint(endpoint)
    endpoints = [endpoint] + [old for old in load_verified_endpoints(directory) if old != endpoint]
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _FILENAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"version": 1,
                                     "wifi_endpoints": endpoints[:MAX_REMEMBERED_ENDPOINTS]},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
