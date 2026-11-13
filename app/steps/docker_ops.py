"""Docker side via the Portainer API: pull, render the fixed compose template, deploy it as a Portainer stack,
remove it, wait until the app answers.

The deployer has no Docker socket. Image and container calls go through Portainer's Docker proxy
(/api/endpoints/{id}/docker/...), compose projects are Portainer stacks (one stack "app-<name>" per app).
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import httpx
import yaml

from ..config import Settings


class DockerError(RuntimeError):
    pass


SECRET_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MANAGED_HEADER = "# Managed by vps-deployer, manual changes (also in Portainer) are overwritten.\n"


def container_name(name: str) -> str:
    return f"app-{name}"


def project_name(name: str) -> str:
    return f"app-{name}"


def volume_name(name: str) -> str:
    return f"app-{name}-data"


# --- Portainer client ----------------------------------------------------------------------------

class Portainer:
    transport: httpx.BaseTransport | None = None  # tests inject a MockTransport here

    def __init__(self, s: Settings):
        self.s = s
        self.endpoint = s.portainer_endpoint_id
        self.client = httpx.Client(base_url=s.portainer_url.rstrip("/") + "/api",
                                   headers={"X-API-Key": s.portainer_api_key},
                                   verify=s.portainer_verify_tls, timeout=60, transport=self.transport)

    def __enter__(self) -> "Portainer":
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def call(self, method: str, path: str, *, allow_404: bool = False, **kw) -> httpx.Response | None:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise DockerError(f"Portainer {method} {path}: {type(exc).__name__}: {exc}") from exc
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = " - ".join(str(v) for v in (body.get("message"), body.get("details")) if v)
            except (ValueError, AttributeError):
                msg = r.text
            raise DockerError(f"Portainer {method} {path}: HTTP {r.status_code} {(msg or '')[:300]}")
        return r

    def docker(self, method: str, path: str, **kw) -> httpx.Response | None:
        """Docker Engine API through Portainer's proxy for the configured environment."""
        return self.call(method, f"/endpoints/{self.endpoint}/docker{path}", **kw)

    # --- stacks ---

    def find_stack(self, stack: str) -> dict | None:
        r = self.call("GET", "/stacks", params={"filters": json.dumps({"EndpointID": self.endpoint})})
        for st in r.json() or []:
            if st.get("Name") == stack and st.get("EndpointId") == self.endpoint:
                return st
        return None

    def create_stack(self, stack: str, content: str, env: list[dict]) -> None:
        self.call("POST", "/stacks/create/standalone/string", params={"endpointId": self.endpoint}, timeout=600,
                  json={"name": stack, "stackFileContent": content, "env": env})

    def update_stack(self, stack_id: int, content: str, env: list[dict]) -> None:
        self.call("PUT", f"/stacks/{stack_id}", params={"endpointId": self.endpoint}, timeout=600,
                  json={"stackFileContent": content, "env": env, "prune": True, "pullImage": False})

    def delete_stack(self, stack_id: int) -> None:
        self.call("DELETE", f"/stacks/{stack_id}", params={"endpointId": self.endpoint}, timeout=600,
                  allow_404=True)


# --- images --------------------------------------------------------------------------------------

def _split_image(image: str) -> tuple[str, str]:
    """'repo[:tag]@sha256:…' -> ('repo', 'sha256:…'). The digest is what gets pulled and run."""
    name, _, digest = image.partition("@")
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: -(len(last) - last.index(":"))]
    return name, digest


def pull(s: Settings, image: str) -> None:
    repo, digest = _split_image(image)
    headers = {}
    if s.portainer_registry_id:
        # Portainer replaces this with the credentials of the registry configured in Portainer.
        headers["X-Registry-Auth"] = base64.b64encode(
            json.dumps({"registryId": s.portainer_registry_id}).encode()).decode()
    with Portainer(s) as p:
        r = p.docker("POST", "/images/create", params={"fromImage": repo, "tag": digest}, headers=headers,
                     timeout=600)
    # Docker reports pull failures inside the 200 JSON stream.
    for line in r.text.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("error"):
            raise DockerError(f"pull {image} failed: {msg['error']}")


def inspect_image(s: Settings, image: str) -> dict:
    repo, digest = _split_image(image)
    with Portainer(s) as p:
        r = p.docker("GET", f"/images/{repo}@{digest}/json", allow_404=True)
    if r is None:
        raise DockerError(f"image {image} not found after pull")
    cfg = r.json().get("Config") or {}
    return {"exposed_ports": cfg.get("ExposedPorts") or {}, "labels": cfg.get("Labels") or {}}


# --- compose template / stack --------------------------------------------------------------------

def render_compose(s: Settings, *, name: str, project: str, image: str, env: dict, volume_path: str | None) -> dict:
    """Fixed template. Nothing from the request ends up anywhere except image, env values and volume path."""
    service: dict = {
        "image": image,
        "container_name": container_name(name),
        "restart": "unless-stopped",
        "networks": [s.apps_network],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": s.mem_limit,
        "cpus": float(s.cpu_limit),
        "pids_limit": 512,
        "labels": {
            "de.tk05.managed": "true",
            "de.tk05.app": name,
            "de.tk05.project": project,
        },
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        # Compose interpolates "$" in the file, escape so values are passed literally.
        "environment": {k: v.replace("$", "$$") for k, v in env.items()},
        # Portainer writes the stack's env variables (= secrets.env, see read_secrets) to stack.env.
        "env_file": [{"path": "stack.env", "required": False}],
    }
    doc: dict = {
        "name": project_name(name),
        "services": {"app": service},
        "networks": {s.apps_network: {"external": True}},
    }
    if volume_path:
        vol = volume_name(name)
        service["volumes"] = [f"{vol}:{volume_path}"]
        doc["volumes"] = {vol: {"name": vol}}
    return doc


def read_secrets(s: Settings, name: str) -> list[dict]:
    """Hand-maintained /opt/vps-deployer/apps/<name>/secrets.env -> Portainer stack env (stack.env)."""
    path = Path(s.apps_dir) / name / "secrets.env"
    if not path.exists():
        return []
    out: list[dict] = []
    for no, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not SECRET_KEY_RE.match(key):
            raise DockerError(f"{path} line {no}: expected KEY=VALUE")  # never echo the line (secret)
        out.append({"name": key, "value": value})
    return out


def find_stack(s: Settings, name: str) -> dict | None:
    with Portainer(s) as p:
        return p.find_stack(project_name(name))


def up(s: Settings, name: str, doc: dict) -> None:
    """Create or update the Portainer stack app-<name> and bring it up."""
    content = MANAGED_HEADER + yaml.safe_dump(doc, sort_keys=False)
    env = read_secrets(s, name)
    with Portainer(s) as p:
        stack = p.find_stack(project_name(name))
        if stack:
            p.update_stack(stack["Id"], content, env)
            return
        # App deployed before the switch to Portainer (plain docker compose): Portainer refuses a stack whose
        # project/container name is already taken, so remove the old managed container first. The named volume
        # is kept and re-used by the stack.
        old = _inspect_container(p, name)
        if old:
            if (old.get("Config", {}).get("Labels") or {}).get("de.tk05.managed") != "true":
                raise DockerError(f"container {container_name(name)} exists and is not managed by vps-deployer")
            p.docker("DELETE", f"/containers/{container_name(name)}", params={"force": "true"}, allow_404=True)
        p.create_stack(project_name(name), content, env)


def down(s: Settings, name: str, *, remove_volume: bool = False) -> None:
    with Portainer(s) as p:
        stack = p.find_stack(project_name(name))
        if stack:
            p.delete_stack(stack["Id"])
        else:  # no stack (legacy deployment or never created): remove container directly, ignore if absent
            p.docker("DELETE", f"/containers/{container_name(name)}", params={"force": "true"}, allow_404=True)
        if remove_volume:
            p.docker("DELETE", f"/volumes/{volume_name(name)}", allow_404=True)


# --- containers ----------------------------------------------------------------------------------

def _inspect_container(p: Portainer, name: str) -> dict | None:
    r = p.docker("GET", f"/containers/{container_name(name)}/json", allow_404=True)
    return r.json() if r is not None else None


def container_exists(s: Settings, name: str) -> bool:
    with Portainer(s) as p:
        return _inspect_container(p, name) is not None


def _demux_logs(raw: bytes) -> str:
    """Docker log stream without TTY: 8-byte frame header (stream, 0, 0, 0, size BE32) + payload."""
    out, i = [], 0
    while i + 8 <= len(raw):
        stream, size = raw[i], int.from_bytes(raw[i + 4:i + 8], "big")
        if stream not in (0, 1, 2) or raw[i + 1:i + 4] != b"\0\0\0":
            return raw.decode(errors="replace")  # TTY container: plain stream
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode(errors="replace")


def logs_tail(s: Settings, name: str, lines: int = 15) -> str:
    try:
        with Portainer(s) as p:
            r = p.docker("GET", f"/containers/{container_name(name)}/logs",
                         params={"stdout": "1", "stderr": "1", "tail": str(lines)}, allow_404=True)
    except DockerError as exc:
        return f"(logs unavailable: {exc})"
    return _demux_logs(r.content).strip() if r is not None else "(container not found)"


def wait_ready(s: Settings, name: str, port: int) -> None:
    """Container must be running (and healthy if it has a healthcheck) and answer HTTP on its port."""
    deadline = time.monotonic() + s.health_timeout
    url = f"http://{container_name(name)}:{port}/"
    last = "not started"
    with Portainer(s) as p:
        while time.monotonic() < deadline:
            try:
                info = _inspect_container(p, name)
                if info is None:
                    last = "container not found"
            except DockerError as exc:
                info, last = None, str(exc)
            if info is None:
                time.sleep(2)
                continue
            state = info.get("State") or {}
            if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
                raise DockerError(f"container {state.get('Status')} (exit {state.get('ExitCode')}):\n"
                                  + logs_tail(s, name))
            health = (state.get("Health") or {}).get("Status")
            if health == "unhealthy":
                raise DockerError("container healthcheck reports unhealthy:\n" + logs_tail(s, name))
            if state.get("Running") and health in (None, "healthy"):
                try:
                    r = httpx.get(url, timeout=5, follow_redirects=False)
                    if r.status_code < 500:
                        return
                    last = f"HTTP {r.status_code}"
                except httpx.HTTPError as exc:
                    last = type(exc).__name__
            else:
                last = f"state={state.get('Status')} health={health}"
            time.sleep(2)
    raise DockerError(f"app not ready after {s.health_timeout}s ({last}):\n" + logs_tail(s, name))


def remove_app_dir(s: Settings, name: str) -> None:
    app_dir = Path(s.apps_dir) / name
    # compose.yml only exists from deployments before the switch to Portainer stacks
    for f in ("compose.yml", "compose.yml.tmp"):
        (app_dir / f).unlink(missing_ok=True)
    # secrets.env is kept on purpose (hand-maintained); remove dir only if empty
    try:
        app_dir.rmdir()
    except OSError:
        pass
