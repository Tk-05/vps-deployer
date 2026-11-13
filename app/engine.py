"""Deployment orchestration: prepare (validate, no changes) and run (with rollback)."""
from __future__ import annotations

import logging
import subprocess

from . import db
from .config import Settings
from .steps import docker_ops, smoke
from .steps.cloudflare import Cloudflare, check_wildcard
from .steps.npm import Npm
from .validation import (ValidationError, port_from_image, slugify, validate_env, validate_image,
                         validate_port, validate_subdomain, validate_volume_path)

log = logging.getLogger("vps-deployer")


class Conflict(RuntimeError):
    pass


def fqdn_for(s: Settings, name: str) -> str:
    return f"{name}.{s.base_domain}"


def cloudflare(s: Settings) -> Cloudflare | None:
    """Cloudflare API only when a token is configured; otherwise wildcard-DNS mode."""
    return Cloudflare(s) if s.cf_api_token else None


def _container_exists(name: str) -> bool:
    proc = subprocess.run(["docker", "inspect", docker_ops.container_name(name)], capture_output=True)
    return proc.returncode == 0


def _check_name_free(s: Settings, name: str) -> None:
    """For a new app: nothing else may already use the name (NPM host, foreign DNS record, container)."""
    fqdn = fqdn_for(s, name)
    if Npm(s).find_host(fqdn):
        raise Conflict(f"{fqdn} already exists in NPM and is not managed by vps-deployer")
    cf = cloudflare(s)
    if cf:
        try:
            cf.check_free(fqdn)
        except RuntimeError as exc:
            raise Conflict(str(exc)) from exc
    if _container_exists(name):
        raise Conflict(f"a container {docker_ops.container_name(name)} already exists")


def prepare_deploy(s: Settings, *, project: str, image: str, subdomain: str | None, port: int | None,
                   env: dict | None, volume_path: str | None) -> dict:
    if not project or len(project) > 100:
        raise ValidationError("project is required (max 100 chars)")
    name = validate_subdomain(subdomain or slugify(project), s.reserved_names)
    image = validate_image(image, s.image_prefix)
    port = validate_port(port)
    env = validate_env(env)
    volume_path = validate_volume_path(volume_path)

    if db.running_for_name(name):
        raise Conflict(f"a deployment for {name} is currently running")

    app = db.get_app(name)
    if app:
        if app["project"] != project:
            raise Conflict(f"{name} belongs to project {app['project']!r}")
        if volume_path is None:
            volume_path = app["volume_path"]
    else:
        _check_name_free(s, name)
    if not cloudflare(s):
        try:
            check_wildcard(s, fqdn_for(s, name))
        except RuntimeError as exc:
            raise ValidationError(str(exc)) from exc

    # Pulling only downloads the image (no running change); needed to verify it exists and to read EXPOSE.
    docker_ops.pull(image)
    meta = docker_ops.inspect_image(image)
    if port is None:
        port = port_from_image(meta["exposed_ports"], meta["labels"]) or (app["port"] if app else None)
    if port is None:
        raise ValidationError("port could not be determined (no single EXPOSE / de.tk05.port label); pass 'port'")

    summary = {
        "name": name,
        "fqdn": fqdn_for(s, name),
        "url": f"https://{fqdn_for(s, name)}",
        "image": image,
        "port": port,
        "volume_path": volume_path,
        "env_keys": sorted(env),
        "is_update": app is not None,
        "previous_image": app["image"] if app else None,
    }
    return db.create_deployment(action="deploy", project=project, name=name, image=image, port=port, env=env,
                                volume_path=volume_path, summary=summary, ttl_hours=s.pending_ttl_hours)


def prepare_remove(s: Settings, name: str, remove_volume: bool) -> dict:
    app = db.get_app(name)
    if not app:
        raise LookupError(f"app {name} is not managed by vps-deployer")
    if db.running_for_name(name):
        raise Conflict(f"a deployment for {name} is currently running")
    summary = {"name": name, "fqdn": fqdn_for(s, name), "image": app["image"], "remove_volume": remove_volume,
               "volume_path": app["volume_path"]}
    return db.create_deployment(action="remove", project=app["project"], name=name, image=None, port=None,
                                env={}, volume_path=app["volume_path"], summary=summary,
                                ttl_hours=s.pending_ttl_hours)


# --- execution ---------------------------------------------------------------------------------

def run(s: Settings, dep_id: str) -> None:
    dep = db.get_deployment(dep_id)
    say = lambda msg: (db.append_log(dep_id, msg), log.info("[%s] %s", dep_id, msg))  # noqa: E731
    try:
        if dep["action"] == "deploy":
            status = _run_deploy(s, dep, say)
        else:
            _run_remove(s, dep, say)
            status = "succeeded"
    except Exception as exc:  # last-resort guard: never leave a deployment in "running"
        log.exception("deployment %s crashed", dep_id)
        say(f"ERROR: {exc}")
        status = "failed"
    db.finish(dep_id, status)


def _run_deploy(s: Settings, dep: dict, say) -> str:
    name, fqdn, image, port = dep["name"], fqdn_for(s, dep["name"]), dep["image"], dep["port"]
    previous = db.get_app(name)
    npm, cf = Npm(s), cloudflare(s)
    undo: list[tuple[str, object]] = []
    npm_host_id = previous["npm_host_id"] if previous else None
    dns_ids = list(previous["dns_record_ids"]) if previous else []

    try:
        if not previous:  # state may have changed since prepare
            _check_name_free(s, name)
        say("NPM preflight: checking certificate references")
        npm.preflight()
        cert_id = npm.wildcard_certificate_id()

        say(f"pulling {image}")
        docker_ops.pull(image)
        doc = docker_ops.render_compose(s, name=name, project=dep["project"], image=image, env=dep["env"],
                                        volume_path=dep["volume_path"])
        docker_ops.write_compose(s, name, doc)
        undo.append(("container", None))
        say("starting container")
        docker_ops.up(s, name)
        say(f"waiting for app on port {port}")
        docker_ops.wait_ready(s, name, port)

        if cf:
            say(f"DNS: ensuring proxied A/AAAA for {fqdn}")
            created = cf.ensure(fqdn, comment=f"vps-deployer: {dep['project']}")
            if created:
                undo.append(("dns", created))
                dns_ids += created
        else:
            addrs, direct = check_wildcard(s, fqdn)
            say(f"DNS: {fqdn} resolves to {', '.join(sorted(addrs))} (wildcard"
                + ("" if direct else ", not this VPS directly - fine if proxied by Cloudflare") + ")")

        host = npm.find_host(fqdn)
        if host and host["id"] != npm_host_id:
            raise Conflict(f"{fqdn} exists in NPM (id {host['id']}) but is not the managed host")
        if host:
            say(f"NPM: updating proxy host {host['id']} (certificate {cert_id})")
            undo.append(("npm_update", host))
            npm.update_host(host["id"], fqdn, docker_ops.container_name(name), port, cert_id)
        else:
            say(f"NPM: creating proxy host (certificate {cert_id})")
            npm_host_id = npm.create_host(fqdn, docker_ops.container_name(name), port, cert_id)
            undo.append(("npm_create", npm_host_id))

        say("smoke test via NPM")
        code = smoke.check(s, fqdn)
        say(f"https://{fqdn} answers HTTP {code}")

        db.upsert_app(name=name, project=dep["project"], image=image, port=port, env=dep["env"],
                      volume_path=dep["volume_path"], npm_host_id=npm_host_id, dns_record_ids=dns_ids)
        say("done")
        return "succeeded"

    except Exception as exc:
        say(f"FAILED: {exc}")
        if not undo:
            return "failed"
        say("rolling back")
        try:
            _rollback(s, name, previous, undo, npm, cf, say)
            say("rollback complete")
            return "rolled_back"
        except Exception as rb_exc:
            say(f"ROLLBACK FAILED: {rb_exc} - manual check needed")
            return "failed"


def _rollback(s: Settings, name: str, previous: dict | None, undo: list, npm: Npm, cf: Cloudflare | None,
              say) -> None:
    for kind, data in reversed(undo):
        if kind == "npm_create":
            say(f"rollback: deleting NPM host {data}")
            npm.delete_host(data)
        elif kind == "npm_update":
            say("rollback: restoring NPM host")
            npm.update_host(data["id"], data["domain_names"][0], data["forward_host"], data["forward_port"],
                            data["certificate_id"])
        elif kind == "dns":
            say(f"rollback: deleting DNS records {data}")
            cf.delete(data)
        elif kind == "container":
            if previous:
                say(f"rollback: restoring previous image {previous['image']}")
                doc = docker_ops.render_compose(s, name=name, project=previous["project"], image=previous["image"],
                                                env=previous["env"], volume_path=previous["volume_path"])
                docker_ops.write_compose(s, name, doc)
                docker_ops.up(s, name)
                docker_ops.wait_ready(s, name, previous["port"])
            else:
                say("rollback: removing container")
                docker_ops.down(s, name)
                docker_ops.remove_app_dir(s, name)


def _run_remove(s: Settings, dep: dict, say) -> None:
    name, fqdn = dep["name"], fqdn_for(s, dep["name"])
    app = db.get_app(name)
    if not app:
        raise LookupError(f"app {name} no longer managed")
    npm, cf = Npm(s), cloudflare(s)
    if app["npm_host_id"]:
        say("NPM preflight: checking certificate references")
        npm.preflight()
        say(f"NPM: deleting proxy host {app['npm_host_id']}")
        npm.delete_host(app["npm_host_id"])
    if app["dns_record_ids"]:
        if cf:
            say(f"DNS: deleting records for {fqdn}")
            cf.delete(app["dns_record_ids"])
        else:
            say(f"DNS: records {app['dns_record_ids']} were created via API but no CF_API_TOKEN is set - "
                "delete them in Cloudflare by hand")
    remove_volume = bool(dep["summary"].get("remove_volume"))
    say("stopping container" + (" and deleting volume" if remove_volume else " (volume kept)"))
    docker_ops.down(s, name, remove_volume=remove_volume)
    docker_ops.remove_app_dir(s, name)
    db.delete_app(name)
    say("done")
