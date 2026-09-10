"""Local API Interface Protection and Outbound SSRF Guard.

Fulfills R04 requirements:
1. Loopback origin and host validation (block arbitrary external web pages from accessing local endpoints).
2. Outbound URL safety verification (block loopback, private IP ranges, link-local, cloud metadata SSRF).
"""

from __future__ import annotations
import ipaddress
import re
import socket
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlsplit

ALLOWED_ORIGIN_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "https://localhost",
    "https://127.0.0.1",
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",
)

ALLOWED_HOST_PATTERNS = (
    r"^localhost(:\d+)?$",
    r"^127\.0\.0\.1(:\d+)?$",
    r"^\[::1\](:\d+)?$",
    r"^testserver(:\d+)?$",
)


def is_allowed_host(host: Optional[str]) -> bool:
    """Verify Host header points strictly to loopback."""
    if not host:
        return True  # Native desktop / CLI requests may omit Host
    clean = host.strip().lower()
    return any(re.match(pattern, clean) for pattern in ALLOWED_HOST_PATTERNS)


def is_allowed_origin(origin: Optional[str]) -> bool:
    """Verify Origin header comes from approved local desktop UI or frontend."""
    if not origin:
        return True  # Desktop Tauri shell or curl often have no Origin header
    clean = origin.strip()
    try:
        parsed = urlsplit(clean)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    allowed = {
        ("http", "localhost"),
        ("https", "localhost"),
        ("http", "127.0.0.1"),
        ("https", "127.0.0.1"),
        ("http", "::1"),
        ("https", "::1"),
        ("http", "tauri.localhost"),
        ("https", "tauri.localhost"),
        ("tauri", "localhost"),
    }
    return (parsed.scheme.lower(), hostname) in allowed


def validate_local_request(host: Optional[str], origin: Optional[str]) -> Tuple[bool, str]:
    """Validate incoming HTTP request headers for local security boundary."""
    if not is_allowed_host(host):
        return False, f"Forbidden Host header: '{host}'. Only loopback addresses are allowed."
    if not is_allowed_origin(origin):
        return False, f"Forbidden Origin: '{origin}'. Cross-site local API calls are rejected."
    return True, "OK"


def is_safe_outbound_url(url: str) -> Tuple[bool, str]:
    """Validate external news or scraping URL against SSRF and private network attacks.
    
    R04 rules:
    - Must use HTTPS scheme.
    - Cannot target loopback, private ranges, link-local, or cloud metadata IPs.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Invalid URL format: {exc}"

    if parsed.scheme.lower() != "https":
        return False, f"Protocol '{parsed.scheme}' not allowed. Only HTTPS is supported."

    if parsed.username or parsed.password:
        return False, "Credentials in outbound URLs are not allowed."
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False, "Outbound URL port is outside the valid range."
    except ValueError:
        return False, "Outbound URL port is invalid."

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname in URL."

    clean_host = hostname.strip().lower()

    # Block direct localhost strings
    if clean_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, f"Access to loopback target '{clean_host}' is blocked."

    # DNS Resolution check
    try:
        addr_info = socket.getaddrinfo(clean_host, None)
        ips = [info[4][0] for info in addr_info]
    except socket.gaierror as exc:
        return False, f"DNS resolution failed for '{clean_host}': {exc}"
    except Exception as exc:
        return False, f"Failed to verify address for '{clean_host}': {exc}"

    BLOCKED_NETWORKS = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
        ipaddress.ip_network("::1/128"),
    ]

    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if any(ip_obj in net for net in BLOCKED_NETWORKS) or ip_obj.is_loopback:
                return False, f"Target IP '{ip_str}' belongs to private/restricted network range."
            # Specific Cloud Metadata address (AWS/GCP/Azure)
            if str(ip_obj) == "169.254.169.254":
                return False, "Access to cloud instance metadata service is blocked."
        except ValueError:
            return False, f"Invalid IP representation: '{ip_str}'"

    return True, "OK"


def validate_redirect_chain(urls: list[str] | tuple[str, ...]) -> Tuple[bool, str]:
    """Validate every URL in a redirect chain, including the initial URL."""

    if not urls:
        return False, "Redirect chain is empty."
    previous = str(urls[0])
    allowed, reason = is_safe_outbound_url(previous)
    if not allowed:
        return False, reason
    for raw in urls[1:]:
        target = urljoin(previous, str(raw))
        allowed, reason = is_safe_outbound_url(target)
        if not allowed:
            return False, reason
        previous = target
    return True, "OK"
