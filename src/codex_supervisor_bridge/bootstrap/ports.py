from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass
class PortLease:
    host: str
    port: int
    _socket: socket.socket | None = None

    def release(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "PortLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class PortAllocator:
    """Reserve loopback ports while keeping the socket open until launch."""

    def __init__(self, *, host: str = "127.0.0.1", start: int = 8765, end: int = 8795) -> None:
        if not 1 <= start <= end <= 65535:
            raise ValueError("port range must be within 1..65535")
        self.host = host
        self.start = start
        self.end = end

    def reserve(self, preferred: int | None = None, *, excluded: set[int] | None = None) -> PortLease:
        excluded = excluded or set()
        candidates: list[int] = []
        if preferred is not None and preferred not in excluded:
            candidates.append(preferred)
        candidates.extend(
            port
            for port in range(self.start, self.end + 1)
            if port != preferred and port not in excluded
        )
        for port in candidates:
            lease = self._try_reserve(port)
            if lease is not None:
                return lease
        raise OSError(f"no available loopback port in {self.start}-{self.end}")

    def _try_reserve(self, port: int) -> PortLease | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind((self.host, port))
            sock.listen(1)
        except OSError:
            sock.close()
            return None
        return PortLease(self.host, port, sock)
