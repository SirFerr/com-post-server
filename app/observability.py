import json
import logging
import re
import threading
import time
import uuid
from collections import Counter


logger = logging.getLogger("compost.http")
_metrics: Counter[tuple[str, str, int]] = Counter()
_lock = threading.Lock()
_identifier = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_path_id = re.compile(r"/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?=/|$)")


def metrics_text() -> str:
    lines = ["# HELP compost_http_requests_total HTTP requests", "# TYPE compost_http_requests_total counter"]
    with _lock:
        values = sorted(_metrics.items())
    for (method, path, status), count in values:
        lines.append(f'compost_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}')
    return "\n".join(lines) + "\n"


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode(errors="replace") for key, value in scope.get("headers", [])}
        supplied = headers.get("x-request-id", "")
        request_id = supplied if _identifier.fullmatch(supplied) else uuid.uuid4().hex
        started = time.perf_counter()
        status_code = 500

        async def observed_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            method = scope.get("method", "")
            route = scope.get("route")
            path = getattr(route, "path", None) or _path_id.sub("/{id}", scope.get("path", ""))
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            with _lock:
                _metrics[(method, path, status_code)] += 1
            logger.info(json.dumps({
                "event": "http_request",
                "request_id": request_id,
                "method": method,
                "path": path,
                "status": status_code,
                "duration_ms": elapsed_ms,
            }, ensure_ascii=False, separators=(",", ":")))
