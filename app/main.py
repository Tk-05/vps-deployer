"""HTTP API. Two-phase: create pending (validated, no changes) -> approve (runs)."""
from __future__ import annotations

import hmac
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import db, engine
from .config import get_settings
from .validation import ValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="vps-deployer", version="1.0.0", docs_url=None, redoc_url=None)
# One worker: deployments run strictly one after another (NPM/nginx reloads must not interleave).
executor = ThreadPoolExecutor(max_workers=1)


@app.on_event("startup")
def _startup() -> None:
    s = get_settings()
    db.init(os.path.join(s.data_dir, "deployer.sqlite"))
    # Anything still "running" after a restart was interrupted.
    for row in db._db().execute("select id from deployments where status='running'").fetchall():
        db.append_log(row["id"], "interrupted by service restart - check state manually")
        db.finish(row["id"], "failed")


def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {get_settings().api_token}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


class DeployRequest(BaseModel):
    project: str = Field(min_length=1, max_length=100)
    image: str = Field(min_length=1, max_length=300)
    subdomain: str | None = None
    port: int | None = None
    env: dict[str, str] | None = None
    volume_path: str | None = None


def _public(dep: dict) -> dict:
    summary = dep["summary"]
    return {
        "id": dep["id"],
        "action": dep["action"],
        "project": dep["project"],
        "status": dep["status"],
        "summary": summary,
        "url": summary.get("url") if dep["status"] == "succeeded" and dep["action"] == "deploy" else None,
        "log": dep["log"],
        "created_at": dep["created_at"],
        "expires_at": dep["expires_at"],
        "approved_at": dep["approved_at"],
        "finished_at": dep["finished_at"],
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/deployments", status_code=201, dependencies=[Depends(require_token)])
def create_deployment(req: DeployRequest) -> dict:
    s = get_settings()
    try:
        dep = engine.prepare_deploy(s, project=req.project, image=req.image, subdomain=req.subdomain,
                                    port=req.port, env=req.env, volume_path=req.volume_path)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except engine.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:  # Portainer pull / NPM / Cloudflare unreachable
        raise HTTPException(status_code=502, detail=str(exc))
    return _public(dep)


@app.post("/deployments/{dep_id}/approve", status_code=202, dependencies=[Depends(require_token)])
def approve(dep_id: str) -> dict:
    dep = db.get_deployment(dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not db.claim_for_run(dep_id):
        dep = db.get_deployment(dep_id)
        raise HTTPException(status_code=409, detail=f"deployment is {dep['status']}, not pending")
    db.append_log(dep_id, "approved")
    executor.submit(engine.run, get_settings(), dep_id)
    return _public(db.get_deployment(dep_id))


@app.post("/deployments/{dep_id}/cancel", dependencies=[Depends(require_token)])
def cancel(dep_id: str) -> dict:
    dep = db.get_deployment(dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="deployment not found")
    if dep["status"] == "pending":
        db.finish(dep_id, "expired")
        db.append_log(dep_id, "cancelled")
    return _public(db.get_deployment(dep_id))


@app.get("/deployments/{dep_id}", dependencies=[Depends(require_token)])
def get_deployment(dep_id: str) -> dict:
    db.expire_pending()
    dep = db.get_deployment(dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="deployment not found")
    return _public(dep)


@app.get("/apps", dependencies=[Depends(require_token)])
def list_apps() -> list[dict]:
    s = get_settings()
    return [{"name": a["name"], "project": a["project"], "url": f"https://{engine.fqdn_for(s, a['name'])}",
             "image": a["image"], "port": a["port"], "volume_path": a["volume_path"],
             "updated_at": a["updated_at"]} for a in db.list_apps()]


@app.delete("/apps/{name}", status_code=201, dependencies=[Depends(require_token)])
def remove_app(name: str, remove_volume: bool = False) -> dict:
    try:
        dep = engine.prepare_remove(get_settings(), name, remove_volume)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except engine.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _public(dep)
