"""Explicitly armed reconnection to one verified MAC; never replays a job."""
import queue
import threading
import time
from network_transport import parse_tcp_endpoint


def _search(events, token, stop, endpoint, mac):
    if stop.is_set():
        return
    try:
        from board_discovery import discover_board
        address = discover_board(parse_tcp_endpoint(endpoint).host, mac)
    except (OSError, ValueError):
        address = None
    if not stop.is_set():
        events.put((token, f'tcp://{address}:23' if address else None))


class Reconnect:
    def __init__(self):
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.token = object()
        self.endpoint = self.mac = None
        self.worker = None
        self.next_attempt = 0
        self.backoff = 1

    def disarm(self):
        self.stop.set()
        self.token = object()
        self.endpoint = self.mac = None

    def arm(self, endpoint, mac):
        self.disarm()
        if not mac:
            raise ValueError('Плата не передала MAC. Доступно обычное подключение.')
        self.endpoint, self.mac = parse_tcp_endpoint(endpoint).url, mac
        self.stop = threading.Event()
        self.next_attempt, self.backoff = 0, 1

    def poll(self):
        if self.endpoint is None:
            return None
        while not self.events.empty():
            token, endpoint = self.events.get_nowait()
            if token is self.token:
                self.next_attempt = time.monotonic() + self.backoff
                self.backoff = min(10, self.backoff * 2)
                if endpoint:
                    self.endpoint = endpoint
                    return endpoint
        if time.monotonic() >= self.next_attempt and (self.worker is None or not self.worker.is_alive()):
            self.worker = threading.Thread(target=_search, args=(self.events, self.token, self.stop,
                self.endpoint, self.mac), name='MKS-reconnect', daemon=True)
            self.worker.start()
        return None
