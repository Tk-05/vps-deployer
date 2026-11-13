"""Smoke test through NPM itself (inside the docker network, SNI = public hostname)."""
from __future__ import annotations

import socket
import ssl
import time

import httpx

from ..config import Settings


class SmokeError(RuntimeError):
    pass


def _cert_names(host: str, sni: str) -> list[str]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni) as tls:
            der = tls.getpeercert(binary_form=True)
    # Verification is off (we connect by container name), so read the names from the raw certificate.
    from cryptography import x509  # local import keeps module import cheap
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        return san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return [a.value for a in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)]


def check(s: Settings, fqdn: str, attempts: int = 10) -> int:
    """Return HTTP status served by NPM for fqdn; must be < 500 with a certificate covering fqdn."""
    wildcard = f"*.{s.base_domain}"
    last = ""
    for _ in range(attempts):
        try:
            names = _cert_names(s.npm_https_host, fqdn)
            if fqdn not in names and wildcard not in names:
                raise SmokeError(f"NPM serves a certificate for {names}, not for {fqdn}")
            with httpx.Client(verify=False, timeout=15, follow_redirects=False) as client:
                r = client.get(f"https://{s.npm_https_host}/", headers={"Host": fqdn},
                               extensions={"sni_hostname": fqdn})
            if r.status_code < 500:
                return r.status_code
            last = f"HTTP {r.status_code}"
        except (OSError, httpx.HTTPError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(3)  # NPM reloads nginx asynchronously after a change
    raise SmokeError(f"https://{fqdn} not OK via NPM: {last}")
