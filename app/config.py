"""Settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _csv(name: str, default: str) -> list[str]:
    return [v.strip().lower() for v in os.environ.get(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    api_token: str = field(default_factory=lambda: _env("DEPLOYER_API_TOKEN"))
    base_domain: str = field(default_factory=lambda: _env("BASE_DOMAIN", "tk05.org"))
    image_prefix: str = field(default_factory=lambda: _env("IMAGE_PREFIX", "ghcr.io/tk-05/"))
    reserved_names: list[str] = field(
        default_factory=lambda: _csv(
            "RESERVED_NAMES",
            "www,mail,smtp,imap,pop,ftp,npm,proxy,admin,api,vpn,ns1,ns2,"
            "nextcloud,office,status,map,jellyfin,portainer,crowdsec,deployer",
        )
    )

    # Paths
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data"))
    apps_dir: str = field(default_factory=lambda: _env("APPS_DIR", "/opt/vps-deployer/apps"))
    letsencrypt_dir: str = field(default_factory=lambda: _env("LETSENCRYPT_DIR", "/etc/letsencrypt"))

    # Docker
    apps_network: str = field(default_factory=lambda: _env("APPS_NETWORK", "apps"))
    mem_limit: str = field(default_factory=lambda: _env("APP_MEM_LIMIT", "512m"))
    cpu_limit: str = field(default_factory=lambda: _env("APP_CPU_LIMIT", "1.0"))
    health_timeout: int = field(default_factory=lambda: int(_env("HEALTH_TIMEOUT", "120")))

    # Cloudflare (optional). Empty token = rely on a wildcard DNS record, only verify resolution.
    cf_api_token: str = field(default_factory=lambda: _env("CF_API_TOKEN", ""))
    cf_zone_name: str = field(default_factory=lambda: _env("CF_ZONE_NAME", "tk05.org"))
    public_ipv4: str = field(default_factory=lambda: _env("PUBLIC_IPV4"))
    public_ipv6: str = field(default_factory=lambda: _env("PUBLIC_IPV6", ""))

    # Nginx Proxy Manager
    npm_url: str = field(default_factory=lambda: _env("NPM_URL", "http://npm:81"))
    npm_https_host: str = field(default_factory=lambda: _env("NPM_HTTPS_HOST", "npm"))
    npm_identity: str = field(default_factory=lambda: _env("NPM_IDENTITY"))
    npm_secret: str = field(default_factory=lambda: _env("NPM_SECRET"))

    pending_ttl_hours: int = field(default_factory=lambda: int(_env("PENDING_TTL_HOURS", "24")))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
