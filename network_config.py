"""Read FluidNC runtime YAML from the already identified LAN TCP peer.

Adapted from the teammate's PR #2 (0174a3a). This fixed GET command never
changes settings. The numeric socket peer is reused; proxies, redirects,
credentials and a second hostname lookup are not part of this transport.
"""
import http.client
import ipaddress
import math
import socket
import threading
import time

from network_transport import _lan_address

MAX_CONFIG_BYTES = 120_000
HTTP_PORT = 80
CONFIG_PATH = "/command?plain=%24Config%2FDump"


class RuntimeConfigUnavailable(OSError):
    """HTTP cannot supply the body; the caller may read canonical YAML via TCP."""


class RuntimeConfigCancelled(Exception):
    pass


def read_runtime_config(wire, cancelled, *, timeout=12):
    """Return complete UTF-8 lines or an explicit error, never a partial body.

    Call only after the motion TCP session has verified FluidNC identity. The
    HTTP address is derived solely from its confirmed numeric peer. Network
    errors and absent HTTP service permit a console fallback, while malformed
    content or an unexpected peer must not be hidden by trying another source.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 12:
        raise ValueError("Некорректное время чтения конфигурации MKS.")
    if not hasattr(cancelled, "is_set"):
        raise ValueError("Не задан сигнал отмены чтения конфигурации.")
    if cancelled.is_set():
        raise RuntimeConfigCancelled()
    if not str(getattr(wire, "port", "")).lower().startswith("tcp:") or getattr(wire, "closed", False):
        raise ValueError("Нужно действующее TCP-соединение с MKS.")
    try:
        peer = str(ipaddress.ip_address(wire.peer_address))
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError("Не подтверждён IP-адрес подключённой MKS; конфигурация не прочитана.") from exc
    if not _lan_address(peer):
        raise ValueError("IP-адрес подключённой MKS должен быть локальным.")

    deadline = time.monotonic() + float(timeout)
    http_socket = None
    response = None
    finished = threading.Event()
    expired = threading.Event()
    watcher = None

    def remaining():
        if cancelled.is_set():
            raise RuntimeConfigCancelled()
        value = deadline - time.monotonic()
        if value <= 0:
            raise RuntimeConfigUnavailable("Истекло время чтения конфигурации MKS по HTTP.")
        return value

    def watch_deadline():
        # HTTPResponse can wait inside a chunk/header readline. A per-recv
        # timeout alone would let a trickling peer renew that wait forever.
        # Shutdown this exact socket, without closing/reusing its descriptor.
        while not finished.wait(.025):
            if cancelled.is_set() or time.monotonic() >= deadline:
                expired.set()
                try:
                    http_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    try:
        # Connect directly to a numeric address, without DNS, proxies or an
        # HTTPConnection that closes/detaches Connection:close sockets early.
        family = socket.AF_INET6 if ":" in peer else socket.AF_INET
        http_socket = socket.socket(family, socket.SOCK_STREAM)
        http_socket.settimeout(remaining())
        http_socket.connect((peer, HTTP_PORT))
        try:
            actual_peer = str(ipaddress.ip_address(http_socket.getpeername()[0]))
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise ValueError("HTTP-соединение не подтвердило адрес подключённой MKS.") from exc
        if actual_peer != peer:
            raise ValueError("HTTP-конфигурация пришла не от подключённой MKS.")
        watcher = threading.Thread(target=watch_deadline, daemon=True,
                                   name="FluidNC HTTP read deadline")
        watcher.start()
        http_socket.settimeout(remaining())
        host = f"[{peer}]" if ":" in peer else peer
        request = (f"GET {CONFIG_PATH} HTTP/1.1\r\nHost: {host}\r\n"
                   "Accept: text/plain\r\nConnection: close\r\n\r\n").encode("ascii")
        http_socket.sendall(request)
        http_socket.settimeout(remaining())
        response = http.client.HTTPResponse(http_socket)
        response.begin()
        if 300 <= response.status < 400:
            raise ValueError("MKS перенаправила запрос конфигурации; переход на другой адрес запрещён.")
        if response.status != 200:
            raise RuntimeConfigUnavailable("MKS не вернула конфигурацию: HTTP " + str(response.status))
        encoding = response.getheader("Content-Encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise ValueError("Неподдерживаемая упаковка HTTP-конфигурации MKS.")
        length = response.getheader("Content-Length")
        if length is not None:
            if not length.isascii() or not length.isdigit() or len(length) > 6:
                raise ValueError("Некорректный размер HTTP-конфигурации MKS.")
            if int(length) > MAX_CONFIG_BYTES:
                raise ValueError("Слишком большая конфигурация MKS по HTTP.")
        body = bytearray()
        while True:
            http_socket.settimeout(remaining())
            chunk = response.read1(min(8192, MAX_CONFIG_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_CONFIG_BYTES:
                raise ValueError("Слишком большая конфигурация MKS по HTTP.")
        remaining()
        if length is not None and len(body) != int(length):
            raise ValueError("HTTP-конфигурация MKS оборвана.")
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("HTTP-конфигурация MKS содержит некорректный UTF-8.") from exc
        if "\x00" in text:
            raise ValueError("HTTP-конфигурация MKS содержит недопустимые символы.")
        lines = text.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        # Plain HTTP mode often omits the console ACK; framing completes it.
        if lines and lines[-1].strip() == "ok":
            lines.pop()
        if not lines or not any(line.strip() for line in lines):
            raise ValueError("MKS вернула пустую конфигурацию по HTTP.")
        return lines
    except RuntimeConfigUnavailable:
        raise
    except http.client.IncompleteRead as exc:
        if cancelled.is_set():
            raise RuntimeConfigCancelled() from exc
        if expired.is_set():
            raise RuntimeConfigUnavailable("Истекло время чтения конфигурации MKS по HTTP.") from exc
        raise ValueError("HTTP-конфигурация MKS оборвана.") from exc
    except (OSError, http.client.HTTPException) as exc:
        if cancelled.is_set():
            raise RuntimeConfigCancelled() from exc
        if expired.is_set():
            raise RuntimeConfigUnavailable("Истекло время чтения конфигурации MKS по HTTP.") from exc
        if isinstance(exc, http.client.HTTPException) and not isinstance(exc, http.client.RemoteDisconnected):
            raise ValueError("MKS вернула некорректный HTTP-ответ конфигурации.") from exc
        raise RuntimeConfigUnavailable("HTTP-служба конфигурации MKS недоступна.") from exc
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(.2)
        if response is not None:
            response.close()
        if http_socket is not None:
            http_socket.close()
