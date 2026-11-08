"""Per-run egress proxy: the only way out of a proxy-mode jail.

An HTTP proxy (CONNECT for HTTPS, absolute-URI requests for plain HTTP) on a
unix socket that is bound into the jail. Policy:

- The model endpoint (the credential set's base URL, or the Anthropic API)
  is always reachable.
- With an allowlist, only matching hosts are reachable.
- Without one (open internet, decision 14), any public address is reachable;
  loopback, private, link-local and other reserved addresses are refused, so
  agents can't reach Redis, Postgres, the API or cloud metadata.

Connections go direct, or through the worker's own upstream proxy
(``HTTPS_PROXY``/``HTTP_PROXY``, honouring ``NO_PROXY``). Through an upstream
proxy the worker may not be able to resolve names itself; then only literal
addresses are checked and the upstream proxy is the egress control.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_HEADER_BYTES = 64 * 1024
CONNECT_TIMEOUT = 30.0
BUFFER = 64 * 1024

DenyCallback = Callable[[str, int, str], Awaitable[None]]


class Denied(Exception):
    pass


def host_matches(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    pattern = pattern.lower().strip().rstrip(".")
    if not pattern:
        return False
    if pattern.startswith("*."):
        pattern = pattern[1:]
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def endpoint_of(url: str) -> tuple[str, int] | None:
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname.lower(), port


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    # Always reachable (host, port) pairs, whatever the address: the model API.
    endpoints: frozenset[tuple[str, int]]
    # Empty = open internet (public addresses only).
    allowlist: tuple[str, ...] = ()

    def is_endpoint(self, host: str, port: int) -> bool:
        return (host.lower().rstrip("."), port) in self.endpoints

    def check_name(self, host: str, port: int) -> None:
        if self.is_endpoint(host, port):
            return
        if self.allowlist and not any(host_matches(host, p) for p in self.allowlist):
            raise Denied(f"{host} is not in this agent's network allowlist")
        ip = _literal_ip(host)
        if ip is not None and not _is_public(ip):
            raise Denied(f"{host} is a private or reserved address")
        if host.lower().rstrip(".") in ("localhost", "localhost.localdomain"):
            raise Denied(f"{host} is a private or reserved address")

    def check_addresses(self, host: str, port: int, addresses: Sequence[str]) -> None:
        if self.is_endpoint(host, port):
            return
        for ip in addresses:
            if not _is_public(ip):
                raise Denied(f"{host} resolves to a private or reserved address")


class Upstream:
    """The worker's own proxy settings (env), used to reach the internet."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        def get(name: str) -> str:
            return environ.get(name.lower()) or environ.get(name.upper()) or ""

        self._proxies = {k: v for k, v in (("http", get("http_proxy")),) if v}
        https = get("https_proxy")
        if https:
            self._proxies["https"] = https
        self._no_proxy = get("no_proxy")

    def _bypass(self, host: str) -> bool:
        """NO_PROXY match: "*", host names and suffixes, IPs and CIDR ranges."""
        host = host.lower().rstrip(".")
        ip = _literal_ip(host)
        for raw in self._no_proxy.split(","):
            entry = raw.strip().lower()
            if not entry:
                continue
            if entry == "*":
                return True
            if ip is not None:
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(entry, strict=False):
                        return True
                except ValueError:
                    pass
                continue
            if host_matches(host, entry) or host_matches(host, "." + entry.lstrip("*.")):
                return True
        return False

    def proxy_for(self, host: str) -> tuple[str, int] | None:
        url = self._proxies.get("https") or self._proxies.get("http")
        if not url:
            return None
        if self._bypass(host):
            return None
        return endpoint_of(url if "://" in url else f"http://{url}")


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise Denied("request headers too large") from exc


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


def _response(status: str, reason: str) -> bytes:
    body = reason.encode() + b"\n"
    return (
        f"HTTP/1.1 {status}\r\ncontent-type: text/plain\r\ncontent-length: {len(body)}\r\n"
        "connection: close\r\nx-playground-egress: denied\r\n\r\n"
    ).encode() + body


class EgressProxy:
    """One proxy per run, listening on a unix socket in a worker-owned dir."""

    def __init__(
        self,
        socket_path: Path,
        policy: EgressPolicy,
        upstream: Upstream,
        on_deny: DenyCallback | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._policy = policy
        self._upstream = upstream
        self._on_deny = on_deny
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> EgressProxy:
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=MAX_HEADER_BYTES
        )
        # The jail may map the worker to another uid; let it connect.
        self.socket_path.chmod(0o666)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(BaseException):
                await t
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)  # pyright: ignore[reportArgumentType]
        try:
            await self._serve(reader, writer)
        except Exception:
            log.debug("egress connection failed", exc_info=True)
        finally:
            if task is not None:
                self._tasks.discard(task)  # pyright: ignore[reportArgumentType]
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
        except (asyncio.IncompleteReadError, Denied):
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            writer.write(_response("400 Bad Request", "malformed request line"))
            return
        connect = method.upper() == "CONNECT"
        if connect:
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]")
            try:
                port = int(port_s)
            except ValueError:
                writer.write(_response("400 Bad Request", "bad CONNECT target"))
                return
        else:
            ep = endpoint_of(target)
            if ep is None or urlsplit(target).scheme != "http":
                writer.write(_response("400 Bad Request", "only absolute http:// URLs"))
                return
            host, port = ep

        try:
            up_reader, up_writer = await self._open(host, port)
        except Denied as exc:
            reason = str(exc)
            if self._on_deny is not None:
                with contextlib.suppress(Exception):
                    await self._on_deny(host, port, reason)
            writer.write(_response("403 Forbidden", f"blocked by the platform: {reason}"))
            return
        except (OSError, TimeoutError) as exc:
            writer.write(_response("502 Bad Gateway", f"cannot reach {host}:{port}: {exc}"))
            return

        try:
            if connect:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(_origin_form(method, target, version, lines[1:]))
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _open(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self._policy.check_name(host, port)
        upstream = self._upstream.proxy_for(host)
        loop = asyncio.get_running_loop()
        target_ip: str | None = None
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_TIMEOUT
            )
            addresses = [str(info[4][0]) for info in infos]
        except (OSError, TimeoutError):
            if upstream is None:
                raise
            addresses = []  # only the upstream proxy can resolve it
        self._policy.check_addresses(host, port, addresses)
        if addresses:
            target_ip = addresses[0]

        if upstream is None:
            assert target_ip is not None
            # Connect to the address we checked, not a fresh lookup.
            return await asyncio.wait_for(
                asyncio.open_connection(target_ip, port, limit=BUFFER), CONNECT_TIMEOUT
            )
        r, w = await asyncio.wait_for(
            asyncio.open_connection(upstream[0], upstream[1], limit=BUFFER), CONNECT_TIMEOUT
        )
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        w.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await w.drain()
        head = await asyncio.wait_for(_read_head(r), CONNECT_TIMEOUT)
        status = head.split(b"\r\n", 1)[0].split(b" ")
        if len(status) < 2 or status[1] != b"200":
            w.close()
            raise OSError(f"upstream proxy refused: {head.split(b'\r\n', 1)[0].decode()}")
        return r, w


def _origin_form(method: str, target: str, version: str, headers: list[str]) -> bytes:
    parts = urlsplit(target)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    kept = [
        h
        for h in headers
        if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))
    ]
    kept.append("Connection: close")
    return (f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n").encode("latin-1")
