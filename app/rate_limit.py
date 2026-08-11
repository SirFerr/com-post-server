import threading
import time
import ipaddress
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import get_settings


class RateLimitMiddleware:
    """Small single-instance guard for abuse-prone endpoints.

    The reverse proxy remains the primary perimeter. This guard also protects
    local and accidentally exposed deployments where no proxy is present.
    """

    def __init__(self, app):
        self.app = app
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()
        self._trusted_proxies = tuple(
            ipaddress.ip_network(value.strip())
            for value in get_settings().trusted_proxy_cidrs.split(",")
            if value.strip()
        )

    def _client_ip(self, request: Request) -> str:
        direct = request.client.host if request.client else "unknown"
        try:
            trusted = any(ipaddress.ip_address(direct) in network for network in self._trusted_proxies)
        except ValueError:
            trusted = False
        if trusted:
            forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
            if forwarded:
                try:
                    return str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
        return direct

    @staticmethod
    def _limit(request: Request) -> tuple[int, int] | None:
        path = request.url.path
        if request.method == "POST" and path in {"/auth/login", "/auth/register", "/auth/refresh", "/web/login"}:
            return 60, 60
        if request.method == "POST" and ("photo" in path or path.startswith("/incidents")):
            return 30, 60
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        limit = self._limit(request)
        if limit:
            maximum, window = limit
            client = self._client_ip(request)
            key = f"{client}:{request.method}:{request.url.path}"
            now = time.monotonic()
            with self._lock:
                if now - self._last_sweep >= 60:
                    for stale_key, stale_events in list(self._events.items()):
                        while stale_events and stale_events[0] <= now - window:
                            stale_events.popleft()
                        if not stale_events:
                            self._events.pop(stale_key, None)
                    self._last_sweep = now
                events = self._events[key]
                while events and events[0] <= now - window:
                    events.popleft()
                if len(events) >= maximum:
                    response = JSONResponse(
                        {"detail": "Too many requests"},
                        status_code=429,
                        headers={"Retry-After": str(max(1, int(window - (now - events[0]))))},
                    )
                else:
                    events.append(now)
                    response = None
            if response:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
