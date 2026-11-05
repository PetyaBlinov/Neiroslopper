"""Click-initiated, bounded FluidNC discovery; never controls the machine.

FluidNC advertises _telnet._tcp.local. in station mode; its optional HTTP
service has no FluidNC-specific TXT record. Consequently unrelated HTTP
services are ignored. Every candidate still has to answer $I with FluidNC
identity and an ACK. No subnet scan, configuration, reset or reconnect occurs.
"""
from dataclasses import dataclass
import importlib.util
import ipaddress
import math
import queue
import re
import threading
import time

from machine_profiles import fluidnc_identity
from network_transport import TcpWire, parse_tcp_endpoint


MDNS_AVAILABLE = importlib.util.find_spec("zeroconf") is not None
MAX_CANDIDATES = 16
MAX_WORKERS = 4
SERVICE_TYPES = ("_telnet._tcp.local.", "_http._tcp.local.")


@dataclass(frozen=True)
class DiscoveredFluidNC:
    label: str
    endpoint: str
    source: str
    identity: str


class _Stop:
    def __init__(self, parent):
        self.parent = parent
        self.done = threading.Event()

    def is_set(self):
        return self.done.is_set() or self.parent.is_set()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.done.wait(min(.05, remaining))
        return True


def _canonical(value):
    endpoint = parse_tcp_endpoint(value)
    # The chooser targets FluidNC's CNC service, never its binary DWIN tunnel.
    if endpoint.port != 23:
        raise ValueError("Для поиска FluidNC нужен порт 23.")
    return endpoint.url


def _service_candidates(service_type, name, info):
    """Turn one advertisement into LAN endpoints, never a trusted result."""
    if info is None or service_type not in SERVICE_TYPES:
        return []
    server = str(getattr(info, "server", ""))
    if service_type == "_http._tcp.local." and "fluidnc" not in (name + server).lower():
        return []
    if service_type == "_telnet._tcp.local." and getattr(info, "port", None) != 23:
        return []
    candidates = []
    for address in info.parsed_addresses()[:4]:
        host = f"[{address}]" if ":" in address else address
        try:
            endpoint = _canonical(f"tcp://{host}:23")
        except ValueError:
            continue
        if endpoint not in candidates:
            candidates.append(endpoint)
    return candidates


def _browse_mdns(stop, deadline, submit):
    """Runs in a daemon so an OS/mDNS failure cannot hold the chooser open."""
    browser = zc = None
    try:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

        services = queue.Queue(maxsize=32)

        class Listener(ServiceListener):
            def add_service(self, zeroconf, service_type, name):
                if not stop.is_set():
                    try:
                        services.put_nowait((service_type, name))
                    except queue.Full:
                        pass

            update_service = add_service

            def remove_service(self, zeroconf, service_type, name):
                pass

        if stop.is_set():
            return
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        browser = ServiceBrowser(zc, list(SERVICE_TYPES), Listener())
        examined = set()
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                service_type, name = services.get(timeout=.04)
            except queue.Empty:
                continue
            if (service_type, name) in examined or len(examined) >= 24:
                continue
            examined.add((service_type, name))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            info = zc.get_service_info(service_type, name, timeout=max(1, int(min(.15, remaining) * 1000)))
            for endpoint in _service_candidates(service_type, name, info):
                submit(endpoint, "mDNS")
    except (ImportError, OSError, RuntimeError, ValueError):
        # AP, the default hostname and saved addresses still work without mDNS.
        return
    finally:
        if browser is not None:
            browser.cancel()
        if zc is not None:
            zc.close()


def _probe(endpoint, source, stop, deadline):
    """One read-only $I request; no serial port, state query or motion command."""
    wire = None
    try:
        remaining = deadline - time.monotonic()
        if stop.is_set() or remaining < .05:
            return None
        wire = TcpWire(endpoint, timeout=.05, write_timeout=min(.2, remaining),
                       connect_timeout=min(.6, remaining))
        if stop.is_set() or time.monotonic() >= deadline:
            return None
        if wire.write(b"$I\n") != 3:
            return None
        pending, lines = bytearray(), []
        while not stop.is_set() and time.monotonic() < deadline:
            pending.extend(wire.read(1024))
            if len(pending) > 8192:
                return None
            while b"\n" in pending:
                raw, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("error:"):
                    return None
                if line == "ok":
                    if not fluidnc_identity(lines):
                        return None
                    identity = next(line for line in lines if fluidnc_identity([line]))[:240]
                    # Never persist a DNS name that could later resolve elsewhere.
                    peer = str(ipaddress.ip_address(wire.peer_address))
                    host = f"[{peer}]" if ":" in peer else peer
                    address = _canonical(f"tcp://{host}:23")
                    machine = next((line[len("[MSG:Machine:"):-1].strip()
                                    for line in lines
                                    if line.startswith("[MSG:Machine:") and line.endswith("]")), "FluidNC")
                    machine = re.sub(r"[\x00-\x1f\x7f]", "", machine)[:64] or "FluidNC"
                    return DiscoveredFluidNC(f"{machine} · {peer}", address, source, identity)
                if line:
                    lines.append(line)
                    if len(lines) > 64:
                        return None
        return None
    except (OSError, ValueError):
        return None
    finally:
        if wire is not None:
            try:
                wire.close()
            except OSError:
                pass


def discover_fluidnc(cancel_event, remembered=(), timeout=4.0):
    """Return verified choices within timeout; never connect the application.

    Network errors and cancellation return an empty/partial list. Invalid API
    arguments raise ValueError. Call only after an explicit user search, from
    a worker thread; there is no network activity at import or application start.
    """
    if isinstance(timeout, bool):
        raise ValueError("Некорректное время поиска.")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректное время поиска.") from exc
    if not math.isfinite(timeout) or not .1 <= timeout <= 10:
        raise ValueError("Время поиска должно быть от 0,1 до 10 секунд.")
    if not hasattr(cancel_event, "is_set") or not isinstance(remembered, (tuple, list)):
        raise ValueError("Для поиска нужны сигнал отмены и список сохранённых адресов.")
    if cancel_event.is_set():
        return []
    stop = _Stop(cancel_event)
    deadline = time.monotonic() + timeout
    tasks, results = queue.Queue(), queue.Queue()
    seen, lock = set(), threading.Lock()

    def submit(endpoint, source):
        try:
            endpoint = _canonical(endpoint)
        except (ValueError, TypeError):
            return
        with lock:
            if stop.is_set() or endpoint in seen or len(seen) >= MAX_CANDIDATES:
                return
            seen.add(endpoint)
            tasks.put((endpoint, source))

    for endpoint in remembered[:8]:
        submit(endpoint, "Сохранённая плата")
    submit("tcp://192.168.0.1:23", "Точка доступа FluidNC")
    submit("tcp://fluidnc.local:23", "Имя FluidNC")

    def work():
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                endpoint, source = tasks.get(timeout=.04)
            except queue.Empty:
                continue
            try:
                result = _probe(endpoint, source, stop, min(deadline, time.monotonic() + 1.0))
                if result is not None and not stop.is_set():
                    results.put(result)
            finally:
                tasks.task_done()

    for _ in range(MAX_WORKERS):
        threading.Thread(target=work, daemon=True, name="FluidNC discovery probe").start()
    mdns_deadline = min(deadline, time.monotonic() + timeout * .65)
    threading.Thread(target=_browse_mdns, args=(stop, mdns_deadline, submit),
                     daemon=True, name="FluidNC mDNS discovery").start()
    found = {}
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                result = results.get(timeout=min(.04, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            found.setdefault(result.endpoint, result)
    finally:
        stop.done.set()
    return sorted(found.values(), key=lambda item: (item.label.casefold(), item.endpoint))
