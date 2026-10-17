"""Source allow-list for a worker (docs/design/09): stock `--multi-user` ComfyUI has no authentication, so on a farm
only the router and the gateway may talk to a worker. Anyone else on the network could otherwise list users, read
their outputs and submit or interrupt jobs by going to the worker's port directly.

Configuration: `COMFYFARM_ALLOW` — comma-separated addresses or CIDR blocks of the router(s) and gateway(s),
e.g. `COMFYFARM_ALLOW=10.0.0.5,10.0.0.6/32`. Loopback is always allowed (local administration). Unset = open, and
the worker reports itself as unsecured (the dashboard shows it).

The decision uses the TCP peer address, never a header (X-Forwarded-For is whatever the client wrote). No ComfyUI
imports here, so the policy can be tested on its own."""
from __future__ import annotations

import ipaddress
import os

from aiohttp import web

ENV = "COMFYFARM_ALLOW"
LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


class Policy:
    def __init__(self, spec: str | None):
        self.networks: list = []
        self.bad: list[str] = []
        for item in (spec or "").replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError:
                self.bad.append(item)
                continue
            mapped = net.version == 6 and net.network_address.ipv4_mapped
            if mapped and net.prefixlen >= 96:            # ::ffff:a.b.c.d written in the list: compare as IPv4
                net = ipaddress.ip_network(f"{mapped}/{net.prefixlen - 96}", strict=False)
            self.networks.append(net)

    @property
    def secured(self) -> bool:
        """Configured and every entry parsed: a typo must not silently open the worker."""
        return bool(self.networks) and not self.bad

    def allows(self, peer: str | None) -> bool:
        if not self.secured:
            return True                                   # open: nothing to enforce (reported, not hidden)
        if peer is None:
            return False
        try:
            ip = ipaddress.ip_address(peer.split("%")[0])     # strip a scope id (fe80::1%eth0)
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in n for n in LOOPBACK) or any(ip in n for n in self.networks)

    def describe(self) -> dict:
        return {"secured": self.secured, "allow": [str(n) for n in self.networks], "invalid": self.bad}


def peer_address(request: web.Request) -> str | None:
    info = request.transport.get_extra_info("peername") if request.transport else None
    return info[0] if info else None


def make_middleware(policy: Policy):
    @web.middleware
    async def allow_middleware(request: web.Request, handler):
        if not policy.allows(peer_address(request)):
            return web.json_response({"error": "comfyfarm: this worker accepts requests only from the farm router "
                                               "and gateway"}, status=403)
        return await handler(request)
    return allow_middleware


def from_env(environ=None) -> Policy:
    return Policy((environ or os.environ).get(ENV))
