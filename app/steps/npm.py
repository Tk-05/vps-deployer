"""Nginx Proxy Manager API: certificate lookup, sanity check, proxy host create/update/delete."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import Settings


class NpmError(RuntimeError):
    pass


class Npm:
    def __init__(self, s: Settings):
        self.s = s
        self.client = httpx.Client(base_url=s.npm_url.rstrip("/") + "/api", timeout=30)
        self._token: str | None = None
        self._token_until = 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_until:
            r = self.client.post("/tokens", json={"identity": self.s.npm_identity, "secret": self.s.npm_secret})
            if r.status_code != 200:
                raise NpmError(f"NPM login failed: HTTP {r.status_code}")
            data = r.json()
            data = data.get("result", data)
            self._token = data["token"]
            self._token_until = time.time() + 600
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, **kw):
        r = self.client.request(method, path, headers=self._auth(), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise NpmError(f"NPM {method} {path}: HTTP {r.status_code} {msg[:300]}")
        return r.json() if r.content else None

    # --- certificates -------------------------------------------------------------------------

    def certificates(self) -> list[dict]:
        return self._call("GET", "/nginx/certificates")

    def wildcard_certificate_id(self) -> int:
        """Find the valid wildcard certificate for the base domain instead of hard-coding an id."""
        want = f"*.{self.s.base_domain}"
        now = datetime.now(timezone.utc)
        best: tuple[datetime, int] | None = None
        for cert in self.certificates():
            if want not in cert.get("domain_names", []):
                continue
            try:
                expires = datetime.fromisoformat(str(cert["expires_on"]).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if expires <= now:
                continue
            if cert.get("provider") == "letsencrypt" and not self._le_files_exist(cert["id"]):
                continue
            if best is None or expires > best[0]:
                best = (expires, cert["id"])
        if best is None:
            raise NpmError(f"no valid certificate for {want} found in NPM")
        return best[1]

    def _le_files_exist(self, cert_id: int) -> bool:
        live = Path(self.s.letsencrypt_dir) / "live" / f"npm-{cert_id}"
        return (live / "fullchain.pem").exists() and (live / "privkey.pem").exists()

    def preflight(self) -> None:
        """Refuse to touch NPM if any enabled proxy host references a certificate whose files are missing.

        NPM regenerates and reloads nginx on every change; one broken reference makes nginx fail to start
        and takes down every host (this is exactly the outage of 2026-11-13).
        """
        certs = {c["id"]: c for c in self.certificates()}
        problems = []
        for host in self.proxy_hosts():
            cid = host.get("certificate_id") or 0
            if not cid or not host.get("enabled", True):
                continue
            cert = certs.get(cid)
            label = ",".join(host.get("domain_names", []))
            if cert is None:
                problems.append(f"{label} -> certificate {cid} (deleted)")
            elif cert.get("provider") == "letsencrypt" and not self._le_files_exist(cid):
                problems.append(f"{label} -> npm-{cid} (files missing)")
        if problems:
            raise NpmError("NPM has proxy hosts with broken certificates, fix these first: " + "; ".join(problems))

    # --- proxy hosts --------------------------------------------------------------------------

    def proxy_hosts(self) -> list[dict]:
        return self._call("GET", "/nginx/proxy-hosts")

    def find_host(self, fqdn: str) -> dict | None:
        for host in self.proxy_hosts():
            if fqdn in host.get("domain_names", []):
                return host
        return None

    def _payload(self, fqdn: str, forward_host: str, port: int, cert_id: int) -> dict:
        return {
            "domain_names": [fqdn],
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": port,
            "certificate_id": cert_id,
            "ssl_forced": True,
            "http2_support": True,
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "block_exploits": True,
            "caching_enabled": False,
            "allow_websocket_upgrade": True,
            "access_list_id": 0,
            "advanced_config": "",
            "enabled": True,
            "meta": {"letsencrypt_agree": False, "dns_challenge": False},
            "locations": [],
        }

    def create_host(self, fqdn: str, forward_host: str, port: int, cert_id: int) -> int:
        return self._call("POST", "/nginx/proxy-hosts", json=self._payload(fqdn, forward_host, port, cert_id))["id"]

    def update_host(self, host_id: int, fqdn: str, forward_host: str, port: int, cert_id: int) -> None:
        self._call("PUT", f"/nginx/proxy-hosts/{host_id}", json=self._payload(fqdn, forward_host, port, cert_id))

    def delete_host(self, host_id: int) -> None:
        try:
            self._call("DELETE", f"/nginx/proxy-hosts/{host_id}")
        except NpmError as exc:
            if "HTTP 404" not in str(exc):
                raise
