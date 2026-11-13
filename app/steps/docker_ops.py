"""Docker side: pull, render the fixed compose template, up/down, wait until the app answers."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from ..config import Settings


class DockerError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 600, cwd: str | None = None) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        raise DockerError(f"{' '.join(args[:3])} failed: {' | '.join(msg)}")
    return proc.stdout


def container_name(name: str) -> str:
    return f"app-{name}"


def project_name(name: str) -> str:
    return f"app-{name}"


def pull(image: str) -> None:
    _run(["docker", "pull", "--quiet", image])


def inspect_image(image: str) -> dict:
    data = json.loads(_run(["docker", "image", "inspect", image]))
    cfg = data[0].get("Config") or {}
    return {"exposed_ports": cfg.get("ExposedPorts") or {}, "labels": cfg.get("Labels") or {}}


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
        "env_file": [{"path": "secrets.env", "required": False}],
    }
    doc: dict = {
        "name": project_name(name),
        "services": {"app": service},
        "networks": {s.apps_network: {"external": True}},
    }
    if volume_path:
        vol = f"app-{name}-data"
        service["volumes"] = [f"{vol}:{volume_path}"]
        doc["volumes"] = {vol: {"name": vol}}
    return doc


def write_compose(s: Settings, name: str, doc: dict) -> Path:
    app_dir = Path(s.apps_dir) / name
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / "compose.yml"
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text("# Managed by vps-deployer, manual changes are overwritten.\n"
                   + yaml.safe_dump(doc, sort_keys=False))
    os.replace(tmp, path)
    return path


def up(s: Settings, name: str) -> None:
    app_dir = str(Path(s.apps_dir) / name)
    _run(["docker", "compose", "-f", "compose.yml", "up", "-d", "--remove-orphans", "--pull", "never"],
         cwd=app_dir)


def down(s: Settings, name: str, *, remove_volume: bool = False) -> None:
    app_dir = Path(s.apps_dir) / name
    if (app_dir / "compose.yml").exists():
        args = ["docker", "compose", "-f", "compose.yml", "down", "--remove-orphans"]
        if remove_volume:
            args.append("--volumes")
        _run(args, cwd=str(app_dir))
    else:  # compose file gone: remove container directly, ignore if absent
        subprocess.run(["docker", "rm", "-f", container_name(name)], capture_output=True)


def logs_tail(name: str, lines: int = 15) -> str:
    proc = subprocess.run(["docker", "logs", "--tail", str(lines), container_name(name)],
                          capture_output=True, text=True)
    return (proc.stdout + proc.stderr).strip()


def wait_ready(s: Settings, name: str, port: int) -> None:
    """Container must be running (and healthy if it has a healthcheck) and answer HTTP on its port."""
    deadline = time.monotonic() + s.health_timeout
    url = f"http://{container_name(name)}:{port}/"
    last = "not started"
    while time.monotonic() < deadline:
        try:
            state = json.loads(_run(["docker", "inspect", "--format", "{{json .State}}", container_name(name)]))
        except DockerError as exc:
            last = str(exc)
            time.sleep(2)
            continue
        if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
            raise DockerError(f"container {state.get('Status')} (exit {state.get('ExitCode')}):\n"
                              + logs_tail(name))
        health = (state.get("Health") or {}).get("Status")
        if health == "unhealthy":
            raise DockerError("container healthcheck reports unhealthy:\n" + logs_tail(name))
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
    raise DockerError(f"app not ready after {s.health_timeout}s ({last}):\n" + logs_tail(name))


def remove_app_dir(s: Settings, name: str) -> None:
    app_dir = Path(s.apps_dir) / name
    for f in ("compose.yml", "compose.yml.tmp"):
        (app_dir / f).unlink(missing_ok=True)
    # secrets.env is kept on purpose (hand-maintained); remove dir only if empty
    try:
        app_dir.rmdir()
    except OSError:
        pass
