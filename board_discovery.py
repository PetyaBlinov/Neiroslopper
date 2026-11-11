"""Identify this MKS over its LAN without opening a motion session."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import re
import secrets
import select
import socket
import time
import urllib.request

from network_transport import _lan_address

DISCOVERY_PORT = 8889
MACHINE_LINE = '[MSG:Machine: JD_Plotter_DLC32_V22]'
NETWORK = re.compile(r'\[MSG:Mode=(STA|AP):SSID=[^\r\n]*?:'
                     r'(?:Status=Connected:)?IP=([0-9.]+):MAC=([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})\]')


def board_address(lines, peer=None):
    if not any(line.startswith('[VER:') and 'FluidNC' in line for line in lines):
        raise ValueError('Нет ответа FluidNC на запрос версии.')
    if MACHINE_LINE not in lines:
        raise ValueError('Ответила другая конфигурация станка.')
    for line in lines:
        match = NETWORK.fullmatch(line)
        if not match or (match[1] == 'STA' and ':Status=Connected:' not in line):
            continue
        ip = str(ipaddress.IPv4Address(match[2]))
        if _lan_address(ip) and ipaddress.IPv4Address(ip).is_private and not ip.startswith(('127.', '169.254.')):
            if peer is None or peer == ip:
                return ip, match[1]
    raise ValueError('MKS не подтвердила свой адрес на этом соединении.')


def _udp_candidates(timeout):
    nonce = secrets.token_hex(8).encode('ascii')
    packet = b'JD-FIND/1 ' + nonce
    expected = re.compile(rb'JD-MKS/1 ' + nonce + rb' [0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}')
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                     if _lan_address(item[4][0]) and not item[4][0].startswith(('127.', '169.254.'))}
    except OSError:
        addresses = set()
    with ExitStack() as sockets:
        ready = []
        for address in sorted(addresses | {'0.0.0.0'}):
            try:
                wire = sockets.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                wire.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                wire.bind((address, 0))
                wire.sendto(packet, ('255.255.255.255', DISCOVERY_PORT))
                ready.append(wire)
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        found = set()
        while ready and time.monotonic() < deadline:
            readable, _, _ = select.select(ready, [], [], max(0, deadline - time.monotonic()))
            for wire in readable:
                data, (ip, port) = wire.recvfrom(80)
                if expected.fullmatch(data) and port == DISCOVERY_PORT and _lan_address(ip) and ip not in found:
                    found.add(ip)
                    yield ip


def _probe(ip, expected_mac=None):
    from wifi_discovery import _probe as probe
    import threading
    if not _lan_address(ip):
        return False
    result = probe(f'tcp://{ip}:23', 'JD discovery', threading.Event(), time.monotonic() + 1.0)
    return bool(result and (expected_mac is None or result.mac == expected_mac))


def discover_board(preferred, expected_mac=None):
    if _probe(preferred, expected_mac):
        return preferred
    with ExitStack() as stack:
        candidates = _udp_candidates(1.2)
        stack.callback(candidates.close)
        for ip in candidates:
            if ip != preferred and _probe(ip, expected_mac):
                return ip
    for ip in _subnet_candidates(preferred, expected_mac):
        return ip
    return None


def _subnet_candidates(preferred, expected_mac=None):
    """Search local /24 blocks if the phone does not forward discovery broadcasts."""
    try:
        local = {entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                 if _lan_address(entry[4][0]) and not entry[4][0].startswith(('127.', '169.254.'))}
    except OSError:
        return
    networks = {ipaddress.IPv4Network(address + '/24', strict=False) for address in local}
    candidates = [str(address) for network in sorted(networks) for address in network.hosts()
                  if str(address) not in local and str(address) != preferred]
    try:
        suffix = int(preferred.rsplit('.', 1)[1])
        candidates.sort(key=lambda address: int(address.rsplit('.', 1)[1]) != suffix)
    except (ValueError, IndexError):
        pass

    # Check the old host number first. Android commonly keeps the lease's
    # final octet when the hotspot chooses a different subnet.
    likely = [address for address in candidates if address.rsplit('.', 1)[1] == preferred.rsplit('.', 1)[-1]]
    for address in likely:
        if _probe(address, expected_mac):
            yield address
            return
    candidates = [address for address in candidates if address not in likely]

    def reachable(address):
        try:
            with socket.create_connection((address, 80), timeout=.18):
                pass
            return address if _probe(address, expected_mac) else None
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=32, thread_name_prefix='MKS search') as pool:
        futures = [pool.submit(reachable, address) for address in candidates]
        for future in as_completed(futures):
            address = future.result()
            if address:
                for pending in futures:
                    pending.cancel()
                yield address
                return
