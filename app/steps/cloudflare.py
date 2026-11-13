"""DNS: either explicit proxied A/AAAA records via the Cloudflare API, or (no token) a wildcard record
that is only verified by resolving the name."""
from __future__ import annotations

import socket

import httpx

from ..config import Settings

API = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


def resolve(fqdn: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(fqdn, 443, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return set()


def check_wildcard(s: Settings, fqdn: str) -> tuple[set[str], bool]:
    """Wildcard mode: the name must resolve. Returns (addresses, points_directly_at_this_vps).

    A proxied wildcard resolves to Cloudflare addresses, so not pointing at the VPS is not an error by itself.
    """
    addrs = resolve(fqdn)
    if not addrs:
        raise CloudflareError(f"{fqdn} does not resolve - is the wildcard DNS record *.{s.base_domain} set?")
    direct = bool(addrs & {s.public_ipv4, s.public_ipv6} - {""})
    return addrs, direct


class Cloudflare:
    def __init__(self, s: Settings):
        self.s = s
        self.client = httpx.Client(base_url=API, timeout=20,
                                   headers={"Authorization": f"Bearer {s.cf_api_token}"})
        self._zone_id: str | None = None

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            raise CloudflareError(f"{method} {path}: HTTP {r.status_code}")
        if not data.get("success"):
            errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            raise CloudflareError(f"{method} {path}: {errs or r.status_code}")
        return data

    @property
    def zone_id(self) -> str:
        if self._zone_id is None:
            res = self._call("GET", "/zones", params={"name": self.s.cf_zone_name})["result"]
            if not res:
                raise CloudflareError(f"zone {self.s.cf_zone_name} not found / no access")
            self._zone_id = res[0]["id"]
        return self._zone_id

    def records(self, fqdn: str) -> list[dict]:
        return self._call("GET", f"/zones/{self.zone_id}/dns_records", params={"name": fqdn})["result"]

    def wanted(self) -> dict[str, str]:
        out = {"A": self.s.public_ipv4}
        if self.s.public_ipv6:
            out["AAAA"] = self.s.public_ipv6
        return out

    def check_free(self, fqdn: str) -> None:
        """Existing records are only OK if they already point at this VPS (A/AAAA)."""
        wanted = self.wanted()
        for rec in self.records(fqdn):
            if rec["type"] not in wanted or rec["content"] != wanted[rec["type"]]:
                raise CloudflareError(
                    f"{fqdn} already has a DNS record {rec['type']} {rec['content']} that does not point to this VPS"
                )

    def ensure(self, fqdn: str, comment: str) -> list[str]:
        """Create missing A/AAAA records. Returns ids of records *created* by this call."""
        self.check_free(fqdn)
        existing = {r["type"] for r in self.records(fqdn)}
        created: list[str] = []
        for rtype, content in self.wanted().items():
            if rtype in existing:
                continue
            rec = self._call("POST", f"/zones/{self.zone_id}/dns_records", json={
                "type": rtype, "name": fqdn, "content": content, "proxied": True, "ttl": 1,
                "comment": comment[:100],
            })["result"]
            created.append(rec["id"])
        return created

    def delete(self, record_ids: list[str]) -> None:
        for rid in record_ids:
            try:
                self._call("DELETE", f"/zones/{self.zone_id}/dns_records/{rid}")
            except CloudflareError as exc:
                if "not found" not in str(exc).lower():
                    raise
