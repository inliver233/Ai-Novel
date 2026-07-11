from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address

from fastapi import Request

from app.core.config import settings


def _normalized_ip(value: str) -> IPv4Address | IPv6Address:
    parsed = ip_address(value.strip())
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def client_ip(request: Request) -> str:
    """Return the client address, trusting XFF only behind configured direct proxies."""

    client = request.scope.get("client")
    direct_raw = str(client[0] if client else "0.0.0.0")
    try:
        direct = _normalized_ip(direct_raw)
    except ValueError:
        return direct_raw

    trusted = settings.auth_trusted_proxy_networks()
    if not trusted or not any(direct in network for network in trusted):
        return direct.compressed

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return direct.compressed
    try:
        chain = [_normalized_ip(part) for part in forwarded.split(",")]
    except ValueError:
        return direct.compressed
    if not chain:
        return direct.compressed
    for address in reversed(chain):
        if not any(address in network for network in trusted):
            return address.compressed
    return direct.compressed
