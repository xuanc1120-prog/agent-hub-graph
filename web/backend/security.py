"""ASGI boundary checks for the loopback HTTP surface."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import parse_qsl, urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ApiBoundaryMiddleware:
    """Enforce loopback Host/Origin and count bytes actually received."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        host: str,
        port: int,
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.host = host.casefold()
        self.port = port
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        host = headers.get(b"host", b"").decode("latin-1")
        parsed_host = _parse_authority(host, scope.get("scheme", "http"))
        if parsed_host is None or parsed_host != (self.host, self.port):
            await _json_error(send, 421, "invalid_host", "Request host is not allowed.")
            return

        origin_header = headers.get(b"origin")
        if origin_header is not None:
            origin = origin_header.decode("latin-1")
            try:
                parsed_origin = urlsplit(origin)
            except ValueError:
                parsed_origin = urlsplit("")
            origin_host = _parse_authority(parsed_origin.netloc, parsed_origin.scheme)
            if (
                parsed_origin.scheme != scope.get("scheme")
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
                or origin_host != parsed_host
            ):
                await _json_error(send, 403, "invalid_origin", "Request origin is not allowed.")
                return

        query_string = scope.get("query_string", b"")
        if len(query_string) > 4_096:
            await _json_error(send, 414, "query_too_large", "Request query exceeds the limit.")
            return
        try:
            query = parse_qsl(
                query_string.decode("latin-1"),
                keep_blank_values=True,
                max_num_fields=128,
            )
        except ValueError:
            await _json_error(send, 400, "invalid_query", "Request query is invalid.")
            return
        if any(key.casefold() in {"token", "access_token"} for key, _value in query):
            await _json_error(
                send, 400, "token_in_url", "Credentials must use the Authorization header."
            )
            return

        if scope.get("path", "").startswith("/ws/") or scope.get("path") == "/ws":
            await _json_error(send, 404, "not_found", "Endpoint not found.")
            return

        content_lengths = [
            value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _json_error(400, "invalid_content_length", "Invalid request framing.")
            return
        if content_lengths:
            try:
                if not content_lengths[0].isdigit():
                    raise ValueError
                declared_length = int(content_lengths[0])
            except ValueError:
                await _json_error(400, "invalid_content_length", "Invalid request framing.")
                return
            if declared_length < 0:
                await _json_error(400, "invalid_content_length", "Invalid request framing.")
                return
            if declared_length > self.max_body_bytes:
                await _json_error(413, "request_body_too_large", "Request body exceeds the limit.")
                return

        buffered: list[Message] = []
        received = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    await _json_error(
                        send, 413, "request_body_too_large", "Request body exceeds the limit."
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        buffered_index = 0

        async def replay_receive() -> Message:
            nonlocal buffered_index
            if buffered_index < len(buffered):
                message = buffered[buffered_index]
                buffered_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


def _headers(scope: Scope) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    for key, value in scope.get("headers", ()):
        normalized = key.lower()
        if normalized in result and normalized in {b"host", b"origin"}:
            result[normalized] = b""
        else:
            result[normalized] = value
    return result


def _parse_authority(value: str, scheme: str) -> tuple[str, int] | None:
    if not value or "@" in value or "%" in value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(f"{scheme}://{value}")
        hostname = parsed.hostname
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if (
        hostname is None
        or port is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not _is_loopback(hostname)
    ):
        return None
    return (hostname.casefold(), port)


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def _json_error(
    send: Send,
    status: int,
    code: str,
    message: str,
) -> None:
    body = json.dumps(
        {"error": {"code": code, "message": message}},
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = ["ApiBoundaryMiddleware"]
