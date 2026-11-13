import dataclasses
import os
from datetime import timedelta

import pytest
import yaml

os.environ.setdefault("DEPLOYER_API_TOKEN", "t")
os.environ.setdefault("CF_API_TOKEN", "x")
os.environ.setdefault("PUBLIC_IPV4", "203.0.113.1")
os.environ.setdefault("NPM_IDENTITY", "a")
os.environ.setdefault("NPM_SECRET", "b")

from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.steps import docker_ops  # noqa: E402
from app.validation import (ValidationError, port_from_image, slugify, validate_env,  # noqa: E402
                            validate_image, validate_subdomain, validate_volume_path)

DIGEST = "@sha256:" + "a" * 64
PREFIX = "ghcr.io/tk-05/"
RESERVED = Settings().reserved_names


@pytest.mark.parametrize("name", ["foo", "my-app", "a", "a1-b2", "x" * 40])
def test_subdomain_ok(name):
    assert validate_subdomain(name, RESERVED) == name


@pytest.mark.parametrize("name", ["", "-foo", "foo-", "Foo", "foo.bar", "foo_bar", "x" * 41, "a b",
                                  "nextcloud", "map", "www", "npm", "jellyfin"])
def test_subdomain_rejected(name):
    with pytest.raises(ValidationError):
        validate_subdomain(name, RESERVED)


@pytest.mark.parametrize("image", [PREFIX + "app" + DIGEST, PREFIX + "group/app:1.2.3" + DIGEST,
                                   PREFIX + "my-app_x.y:latest" + DIGEST])
def test_image_ok(image):
    assert validate_image(image, PREFIX) == image


@pytest.mark.parametrize("image", [
    PREFIX + "app:latest",                     # no digest
    PREFIX + "app",                            # nothing pinned
    "ghcr.io/evil/app" + DIGEST,               # foreign namespace
    "docker.io/tk-05/app" + DIGEST,            # foreign registry
    "ghcr.io/tk-05.evil/app" + DIGEST,         # prefix trick
    PREFIX + "App" + DIGEST,                   # uppercase repo
    PREFIX + "app@sha256:short",
    PREFIX + "app" + DIGEST + " --privileged",
])
def test_image_rejected(image):
    with pytest.raises(ValidationError):
        validate_image(image, PREFIX)


def test_slugify():
    assert slugify("My Cool_Project!") == "my-cool-project"
    assert slugify("--A--B--") == "a-b"


def test_env():
    assert validate_env({"A": "1", "B_C": "x$y"}) == {"A": "1", "B_C": "x$y"}
    for bad in ({"a": "1"}, {"1A": "x"}, {"A": "x\ny"}, {"A-B": "x"}):
        with pytest.raises(ValidationError):
            validate_env(bad)


def test_volume_path():
    assert validate_volume_path("/data") == "/data"
    for bad in ("data", "/", "/etc", "/data/../etc", "/a b"):
        with pytest.raises(ValidationError):
            validate_volume_path(bad)


def test_port_from_image():
    assert port_from_image({"8080/tcp": {}}, {}) == 8080
    assert port_from_image({"80/tcp": {}, "443/tcp": {}}, {}) is None
    assert port_from_image({"80/tcp": {}, "443/tcp": {}}, {"de.tk05.port": "443"}) == 443
    assert port_from_image({}, {}) is None
    with pytest.raises(ValidationError):
        port_from_image({}, {"de.tk05.port": "abc"})


def test_compose_template_is_locked_down():
    s = Settings()
    doc = docker_ops.render_compose(s, name="foo", project="Foo", image=PREFIX + "foo" + DIGEST,
                                    env={"PASS": "a$b"}, volume_path="/data")
    text = yaml.safe_dump(doc)
    svc = doc["services"]["app"]
    assert svc["container_name"] == "app-foo"
    assert "ports" not in svc and "privileged" not in svc and "cap_add" not in svc
    assert svc["security_opt"] == ["no-new-privileges:true"]
    assert svc["environment"]["PASS"] == "a$$b"
    assert svc["volumes"] == ["app-foo-data:/data"]
    assert doc["networks"] == {s.apps_network: {"external": True}}
    assert "/var/run/docker.sock" not in text


def test_wildcard_dns_mode(monkeypatch):
    from app import engine
    from app.steps import cloudflare as cfmod

    s = Settings()
    assert engine.cloudflare(dataclasses.replace(s, cf_api_token="")) is None
    assert engine.cloudflare(s) is not None  # token set in test env

    monkeypatch.setattr(cfmod, "resolve", lambda fqdn: {"203.0.113.1"})
    assert cfmod.check_wildcard(s, "foo.tk05.org") == ({"203.0.113.1"}, True)
    monkeypatch.setattr(cfmod, "resolve", lambda fqdn: {"104.16.0.1"})  # proxied by Cloudflare
    assert cfmod.check_wildcard(s, "foo.tk05.org") == ({"104.16.0.1"}, False)
    monkeypatch.setattr(cfmod, "resolve", lambda fqdn: set())
    with pytest.raises(cfmod.CloudflareError):
        cfmod.check_wildcard(s, "foo.tk05.org")


def test_pending_lifecycle(tmp_path):
    db.init(str(tmp_path / "t.sqlite"))
    dep = db.create_deployment(action="deploy", project="p", name="foo", image="i", port=80, env={},
                               volume_path=None, summary={}, ttl_hours=24)
    assert dep["status"] == "pending"
    assert db.claim_for_run(dep["id"]) is True
    assert db.claim_for_run(dep["id"]) is False        # cannot approve twice
    assert db.running_for_name("foo") is True
    db.finish(dep["id"], "succeeded")

    old = db.create_deployment(action="deploy", project="p", name="bar", image="i", port=80, env={},
                               volume_path=None, summary={}, ttl_hours=24)
    db._db().execute("update deployments set expires_at=? where id=?",
                     (db.iso(db.now() - timedelta(minutes=1)), old["id"]))
    assert db.claim_for_run(old["id"]) is False        # expired
    assert db.get_deployment(old["id"])["status"] == "expired"
