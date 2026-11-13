"""Input validation. Pure functions so they can be unit-tested without Docker/NPM/Cloudflare."""
from __future__ import annotations

import re

SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
DIGEST_RE = re.compile(r"@sha256:[a-f0-9]{64}$")
REPO_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
VOLUME_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")

MAX_ENV_VARS = 50
MAX_ENV_VALUE = 4096


class ValidationError(ValueError):
    pass


def slugify(project: str) -> str:
    """Turn a project name into a subdomain candidate."""
    slug = re.sub(r"[^a-z0-9-]+", "-", project.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:40].strip("-")


def validate_subdomain(name: str, reserved: list[str]) -> str:
    if not name or not SUBDOMAIN_RE.match(name):
        raise ValidationError(
            f"invalid subdomain {name!r}: only a-z, 0-9 and '-', 1-40 chars, no leading/trailing '-'"
        )
    if name in reserved:
        raise ValidationError(f"subdomain {name!r} is reserved")
    return name


def validate_image(image: str, prefix: str) -> str:
    """Image must come from our registry namespace and be pinned by digest."""
    if not image.startswith(prefix):
        raise ValidationError(f"image must start with {prefix!r}")
    if not DIGEST_RE.search(image):
        raise ValidationError("image must be pinned by digest (…@sha256:<64 hex>), tags alone are not accepted")
    ref = image[len(prefix):]
    name_tag = ref.split("@", 1)[0]
    if ":" in name_tag:
        repo, tag = name_tag.rsplit(":", 1)
        if not TAG_RE.match(tag):
            raise ValidationError(f"invalid tag {tag!r}")
    else:
        repo = name_tag
    if not REPO_RE.match(repo):
        raise ValidationError(f"invalid repository name {repo!r}")
    return image


def validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValidationError(f"invalid port {port!r}")
    return port


def validate_env(env: dict[str, str] | None) -> dict[str, str]:
    if not env:
        return {}
    if len(env) > MAX_ENV_VARS:
        raise ValidationError(f"at most {MAX_ENV_VARS} environment variables allowed")
    out: dict[str, str] = {}
    for key, value in env.items():
        if not ENV_KEY_RE.match(key):
            raise ValidationError(f"invalid env key {key!r}")
        value = str(value)
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValidationError(f"env value for {key} must be a single line")
        if len(value) > MAX_ENV_VALUE:
            raise ValidationError(f"env value for {key} too long")
        out[key] = value
    return out


def validate_volume_path(path: str | None) -> str | None:
    if path is None:
        return None
    if not VOLUME_PATH_RE.match(path) or ".." in path.split("/"):
        raise ValidationError(f"invalid volume path {path!r} (absolute container path expected)")
    if path in ("/", "/etc", "/proc", "/sys", "/dev", "/bin", "/usr", "/lib"):
        raise ValidationError(f"volume path {path!r} not allowed")
    return path


def port_from_image(exposed_ports: dict | None, labels: dict | None) -> int | None:
    """Pick the app port from the image: label de.tk05.port wins, else a single EXPOSE'd TCP port."""
    labels = labels or {}
    if "de.tk05.port" in labels:
        try:
            return validate_port(int(labels["de.tk05.port"]))
        except (ValueError, ValidationError) as exc:
            raise ValidationError(f"image label de.tk05.port is invalid: {labels['de.tk05.port']!r}") from exc
    tcp = sorted(int(p.split("/")[0]) for p in (exposed_ports or {}) if p.endswith("/tcp"))
    if len(tcp) == 1:
        return tcp[0]
    return None
