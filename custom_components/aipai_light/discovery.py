"""Find AIPAI lights on the local network.

Every light runs a tiny HTTP server that answers ``GET /?read=config`` with its
pipe-delimited state string - the same endpoint the provisioner uses (see
``provision/aipai_provision.py``). That string carries the serial and model, so
we can discover lights by sweeping the local subnet and asking each address.

This is LOCAL only: it talks to hosts on the HA machine's own network (or a
subnet you name), never the shared cloud broker - so it finds *your* lights and
no one else's. It only ever sends a harmless read GET.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_READ_PATH = "/?read=config"
# Match the core "on|...|MODEL" body even if the device wraps it in JSON/callback.
_BODY_RE = re.compile(r"(?:on|off)\|[^\"']+")
_MAX_CONCURRENCY = 60
_PER_HOST_TIMEOUT = 1.5      # seconds; a light on the LAN answers well within this
_MAX_HOSTS = 1024           # guard against someone passing a huge CIDR


@dataclass
class FoundLight:
    serial: str
    model: str | None
    roads: int
    ip: str


def parse_read_config(raw: str) -> tuple[str | None, str | None, int]:
    """Pull (serial, model, roads) out of a /?read=config response body."""
    m = _BODY_RE.search(raw)
    body = m.group(0) if m else raw
    fields = [p.strip() for p in body.split("|")]
    n = 8 if len(fields) > 28 else 6
    d2 = 2 * (n - 6)
    serial = fields[21 + d2] if len(fields) > 21 + d2 else None
    model = fields[24 + d2] if len(fields) > 24 + d2 else None
    if serial and not serial.isdigit():
        serial = None
    return serial, model, n


async def async_local_subnets(hass: HomeAssistant) -> list[str]:
    """Best-effort list of the HA host's own IPv4 subnets, as CIDR strings.

    Uses HA's network helper (respects the user's configured adapters) and falls
    back to the primary source IP as a /24. Private ranges only.
    """
    subnets: list[str] = []
    try:
        from homeassistant.components import network

        adapters = await network.async_get_adapters(hass)
        for adapter in adapters:
            if not adapter.get("enabled"):
                continue
            for ip4 in adapter.get("ipv4", []):
                addr = ip4.get("address")
                prefix = ip4.get("network_prefix")
                if not addr or prefix is None:
                    continue
                try:
                    net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
                except ValueError:
                    continue
                if net.version == 4 and net.is_private and not net.is_loopback:
                    cidr = str(net)
                    if cidr not in subnets:
                        subnets.append(cidr)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("adapter enumeration failed (%s); falling back to source IP", err)

    if not subnets:
        try:
            from homeassistant.components import network

            src = await network.async_get_source_ip(hass, network.PUBLIC_TARGET_IP)
            if src:
                net = ipaddress.ip_network(f"{src}/24", strict=False)
                subnets.append(str(net))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("source-IP fallback failed: %s", err)
    return subnets


async def _probe_host(
    session: aiohttp.ClientSession, ip: str, sem: asyncio.Semaphore
) -> FoundLight | None:
    url = f"http://{ip}{_READ_PATH}"
    try:
        async with sem:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_PER_HOST_TIMEOUT)) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return None
    except Exception:  # noqa: BLE001
        return None
    serial, model, roads = parse_read_config(raw)
    if not serial:
        return None
    return FoundLight(serial=serial, model=model, roads=roads, ip=ip)


async def async_scan(hass: HomeAssistant, cidrs: list[str]) -> list[FoundLight]:
    """Scan the given subnets and return the lights that answered, de-duped."""
    hosts: list[str] = []
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            _LOGGER.warning("Skipping invalid subnet %r", cidr)
            continue
        addrs = list(net.hosts()) if net.num_addresses > 2 else list(net)
        for host in addrs:
            hosts.append(str(host))
            if len(hosts) >= _MAX_HOSTS:
                _LOGGER.warning("Subnet scan capped at %d hosts", _MAX_HOSTS)
                break
        if len(hosts) >= _MAX_HOSTS:
            break

    if not hosts:
        return []

    session = async_get_clientsession(hass)
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    results = await asyncio.gather(*(_probe_host(session, ip, sem) for ip in hosts))

    found: dict[str, FoundLight] = {}
    for r in results:
        if r and r.serial not in found:
            found[r.serial] = r
    return list(found.values())
