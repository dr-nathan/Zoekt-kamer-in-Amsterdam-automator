"""Download public Facebook CDN images without sharing the browser session."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import SplitResult, urlsplit, urlunsplit

IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
_ALLOWED_DOMAINS = ("facebook.com", "fbcdn.net")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _parse_image_url(value: str) -> SplitResult | None:
    # urlsplit silently removes some control characters; reject them first.
    if not value or any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or len(host) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        ):
            return None
    except ValueError:
        return None
    if not any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_DOMAINS):
        return None
    return parsed


def is_allowed_image_url(value: str) -> bool:
    """Check URL syntax and hostname boundaries; fetching also validates DNS."""
    return _parse_image_url(value) is not None


def _public_addresses(host: str) -> list[tuple[int, tuple]]:
    addresses: list[tuple[int, tuple]] = []
    for family, _, _, _, address in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    ):
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("Unsupported image destination")
        parsed = ipaddress.ip_address(address[0])
        if isinstance(parsed, ipaddress.IPv6Address):
            if parsed.sixtofour is not None or parsed.teredo is not None:
                raise ValueError("Image destination uses an IP transition tunnel")
            if parsed.ipv4_mapped:
                parsed = parsed.ipv4_mapped
        if (
            not parsed.is_global
            or parsed.is_multicast
            or parsed.is_reserved
            or (family == socket.AF_INET6 and address[3] != 0)
        ):
            # Reject mixed public/private DNS answers instead of falling back to a
            # private address when a public endpoint cannot be reached.
            raise ValueError("Image destination is not public")
        candidate = (family, address)
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise ValueError("Image destination has no addresses")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, destination: tuple[int, tuple]):
        super().__init__(host, port=443, timeout=DOWNLOAD_TIMEOUT, context=ssl.create_default_context())
        self._destination = destination

    def connect(self) -> None:
        # Connect to the exact validated address, avoiding a second DNS lookup
        # between validation and use. TLS still verifies the original hostname.
        family, address = self._destination
        connection = socket.socket(family, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(address)
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def _read_image(response: http.client.HTTPResponse) -> tuple[bytes, str] | None:
    # http.client does not follow redirects. Only complete successful responses
    # are accepted, including when a redirect points at another allowed host.
    if response.status != 200:
        return None
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        return None
    if response.getheader("Content-Encoding", "identity").strip().lower() != "identity":
        return None
    content_length = response.getheader("Content-Length")
    expected_length = None
    if content_length is not None:
        try:
            expected_length = int(content_length)
            if not 0 < expected_length <= MAX_IMAGE_BYTES:
                return None
        except ValueError:
            return None
    body = bytearray()
    while True:
        # Read only one overflow-detection byte beyond the remaining allowance;
        # never buffer an arbitrarily large response before applying the limit.
        chunk = response.read(min(64 * 1024, MAX_IMAGE_BYTES - len(body) + 1))
        if not chunk:
            break
        if len(chunk) > MAX_IMAGE_BYTES - len(body):
            return None
        body.extend(chunk)
    if not body or (expected_length is not None and len(body) != expected_length):
        return None
    return bytes(body), content_type


def download_image(source_url: str, *, referer: str = "") -> tuple[bytes, str] | None:
    """Fetch one allowlisted image, with pinned public DNS and a bounded body.

    This independent connection has no cookie jar, browser credentials, proxy
    environment settings, or redirect handling. Signed CDN query strings are
    preserved exactly.
    """
    parsed = _parse_image_url(source_url)
    if parsed is None:
        return None
    try:
        addresses = _public_addresses(parsed.hostname)
    except (OSError, ValueError):
        return None
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = {
        "Accept": "image/jpeg,image/png,image/webp",
        "Accept-Encoding": "identity",
        "User-Agent": "Chineur2000/1.0",
    }
    if is_allowed_image_url(referer):
        headers["Referer"] = referer
    for address in addresses:
        connection = _PinnedHTTPSConnection(parsed.hostname, address)
        try:
            connection.request("GET", target, headers=headers)
            with connection.getresponse() as response:
                return _read_image(response)
        except (OSError, ValueError, http.client.HTTPException):
            continue
        finally:
            connection.close()
    return None
