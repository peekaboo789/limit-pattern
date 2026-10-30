"""行情源共用的 HTTP 客户端：严格超时，按线程复用连接（不必每只股票重新握手 TLS）。"""

from __future__ import annotations

import threading
from contextlib import contextmanager

import httpx

TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)
_LOCAL = threading.local()


def _clients() -> dict:
    found = getattr(_LOCAL, "clients", None)
    if found is None:
        found = {}
        _LOCAL.clients = found
    return found


@contextmanager
def shared_client(name: str, headers: dict, timeout: httpx.Timeout | None = None):
    clients = _clients()
    client = clients.get(name)
    if client is None or client.is_closed:
        client = httpx.Client(timeout=timeout or TIMEOUT, headers=headers, follow_redirects=True, limits=LIMITS)
        clients[name] = client
    try:
        yield client
    except (httpx.TransportError, httpx.TimeoutException):
        # 连接坏了就丢掉，下一次重新建
        clients.pop(name, None)
        try:
            client.close()
        except Exception:
            pass
        raise
