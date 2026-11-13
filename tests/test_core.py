import dataclasses
import os
from datetime import timedelta

import base64
import json

import httpx
import pytest
import yaml

os.environ.setdefault("DEPLOYER_API_TOKEN", "t")
os.environ.setdefault("CF_API_TOKEN", "x")
os.environ.setdefault("PUBLIC_IPV4", "203.0.113.1")
os.environ.setdefault("NPM_IDENTITY", "a")
os.environ.setdefault("NPM_SECRET", "b")
os.environ.setdefault("PORTAINER_API_KEY", "ptr_test")
os.environ.setdefault("PORTAINER_ENDPOINT_ID", "3")
os.environ.setdefault("PORTAINER_REGISTRY_ID", "7")

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
    assert svc["env_file"] == [{"path": "stack.env", "required": False}]


def test_read_secrets(tmp_path):
    s = dataclasses.replace(Settings(), apps_dir=str(tmp_path))
    assert docker_ops.read_secrets(s, "foo") == []
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "secrets.env").write_text("# comment\n\nDB_PASS=a=b$c\nexport TOKEN=xyz\n")
    assert docker_ops.read_secrets(s, "foo") == [{"name": "DB_PASS", "value": "a=b$c"},
                                                  {"name": "TOKEN", "value": "xyz"}]
    (tmp_path / "foo" / "secrets.env").write_text("NOT A VALID LINE hunter2\n")
    with pytest.raises(docker_ops.DockerError) as exc:
        docker_ops.read_secrets(s, "foo")
    assert "hunter2" not in str(exc.value)


def test_split_image():
    assert docker_ops._split_image(PREFIX + "group/app:1.2" + DIGEST) == (PREFIX + "group/app", DIGEST[1:])
    assert docker_ops._split_image(PREFIX + "app" + DIGEST) == (PREFIX + "app", DIGEST[1:])


def test_demux_logs():
    frame = lambda stream, data: bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data  # noqa: E731
    assert docker_ops._demux_logs(frame(1, b"out\n") + frame(2, b"err\n")) == "out\nerr\n"
    assert docker_ops._demux_logs(b"plain tty output\n") == "plain tty output\n"


class FakePortainer:
    """Minimal Portainer API: stacks + Docker proxy calls the deployer uses."""

    def __init__(self):
        self.stacks: list[dict] = []
        self.containers: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.pull_error: str | None = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["X-API-Key"] == "ptr_test"
        path, method = req.url.path, req.method
        self.calls.append((method, path))
        if path == "/api/stacks" and method == "GET":
            return httpx.Response(200, json=self.stacks)
        if path == "/api/stacks/create/standalone/string":
            body = json.loads(req.content)
            assert req.url.params["endpointId"] == "3"
            self.stacks.append({"Id": len(self.stacks) + 1, "Name": body["name"], "EndpointId": 3,
                                "content": body["stackFileContent"], "Env": body["env"]})
            return httpx.Response(200, json=self.stacks[-1])
        if path.startswith("/api/stacks/") and method == "PUT":
            body = json.loads(req.content)
            st = next(x for x in self.stacks if x["Id"] == int(path.rsplit("/", 1)[1]))
            st.update(content=body["stackFileContent"], Env=body["env"])
            return httpx.Response(200, json=st)
        if path.startswith("/api/stacks/") and method == "DELETE":
            self.stacks = [x for x in self.stacks if x["Id"] != int(path.rsplit("/", 1)[1])]
            return httpx.Response(204)
        if path == "/api/endpoints/3/docker/images/create":
            auth = json.loads(base64.b64decode(req.headers["X-Registry-Auth"]))
            assert auth == {"registryId": 7}
            lines = [{"status": "Pulling"}] + ([{"error": self.pull_error}] if self.pull_error else [])
            return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))
        if path.startswith("/api/endpoints/3/docker/containers/") and path.endswith("/json"):
            name = path.split("/")[-2]
            if name in self.containers:
                return httpx.Response(200, json=self.containers[name])
            return httpx.Response(404, json={"message": "No such container"})
        if path.startswith("/api/endpoints/3/docker/containers/") and method == "DELETE":
            self.containers.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if path.startswith("/api/endpoints/3/docker/volumes/") and method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def portainer(monkeypatch, tmp_path):
    fake = FakePortainer()
    monkeypatch.setattr(docker_ops.Portainer, "transport", httpx.MockTransport(fake))
    return fake, dataclasses.replace(Settings(), apps_dir=str(tmp_path))


def test_pull_via_portainer(portainer):
    fake, s = portainer
    docker_ops.pull(s, PREFIX + "app:1" + DIGEST)
    fake.pull_error = "denied"
    with pytest.raises(docker_ops.DockerError, match="denied"):
        docker_ops.pull(s, PREFIX + "app" + DIGEST)


def test_stack_create_update_delete(portainer, tmp_path):
    fake, s = portainer
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "secrets.env").write_text("PASS=x\n")
    doc = docker_ops.render_compose(s, name="foo", project="Foo", image=PREFIX + "foo" + DIGEST, env={},
                                    volume_path="/data")
    docker_ops.up(s, "foo", doc)
    assert [x["Name"] for x in fake.stacks] == ["app-foo"]
    assert fake.stacks[0]["Env"] == [{"name": "PASS", "value": "x"}]
    assert yaml.safe_load(fake.stacks[0]["content"]) == doc

    doc2 = docker_ops.render_compose(s, name="foo", project="Foo", image=PREFIX + "bar" + DIGEST, env={},
                                     volume_path="/data")
    docker_ops.up(s, "foo", doc2)  # existing stack -> update, not a second stack
    assert len(fake.stacks) == 1 and yaml.safe_load(fake.stacks[0]["content"]) == doc2

    docker_ops.down(s, "foo", remove_volume=True)
    assert fake.stacks == []
    assert ("DELETE", "/api/endpoints/3/docker/volumes/app-foo-data") in fake.calls


def test_legacy_container_is_migrated_only_if_managed(portainer):
    fake, s = portainer
    doc = docker_ops.render_compose(s, name="foo", project="Foo", image=PREFIX + "foo" + DIGEST, env={},
                                    volume_path=None)
    fake.containers["app-foo"] = {"Config": {"Labels": {}}}
    with pytest.raises(docker_ops.DockerError, match="not managed"):
        docker_ops.up(s, "foo", doc)
    assert fake.stacks == [] and "app-foo" in fake.containers

    fake.containers["app-foo"] = {"Config": {"Labels": {"de.tk05.managed": "true"}}}
    docker_ops.up(s, "foo", doc)
    assert "app-foo" not in fake.containers and [x["Name"] for x in fake.stacks] == ["app-foo"]


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


def test_empty_env_means_default(monkeypatch):
    # The Portainer stack passes every variable through as "${VAR:-}", so unset ones arrive empty.
    monkeypatch.setenv("RESERVED_NAMES", "")
    monkeypatch.setenv("BASE_DOMAIN", "")
    monkeypatch.setenv("PORTAINER_VERIFY_TLS", "")
    s = Settings()
    assert "npm" in s.reserved_names and "deployer" in s.reserved_names
    assert s.base_domain == "tk05.org" and s.portainer_verify_tls is True
    monkeypatch.setenv("DEPLOYER_API_TOKEN", " ")
    with pytest.raises(RuntimeError, match="DEPLOYER_API_TOKEN"):
        Settings()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERNAL_VARS = {"DATA_DIR", "APPS_DIR", "LETSENCRYPT_DIR", "APPS_NETWORK"}  # fixed by the container layout


def test_stack_passes_every_setting():
    """docker-compose.yml, .env.example and config.py must list the same variables (all set in Portainer)."""
    import re
    with open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8") as f:
        config_vars = set(re.findall(r'_(?:env|csv)\(\s*"([A-Z0-9_]+)"', f.read())) - INTERNAL_VARS
    with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as f:
        compose_text = f.read()
    svc = yaml.safe_load(compose_text)["services"]["deployer"]
    assert "env_file" not in svc
    assert set(svc["environment"]) == config_vars
    for key, value in svc["environment"].items():
        assert value.startswith("${" + key + ":"), key  # only passed through, nothing hard-coded
    with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as f:
        example_vars = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", f.read(), re.M))
    stack_vars = set(re.findall(r"\$\{([A-Z0-9_]+)", compose_text))
    assert stack_vars <= example_vars, stack_vars - example_vars
