"""SQLite storage for deployments (audit log) and managed apps."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
create table if not exists deployments (
    id text primary key,
    action text not null,            -- deploy | remove
    project text not null,
    name text not null,              -- subdomain / app name
    image text,
    port integer,
    env text,                        -- json
    volume_path text,
    status text not null,            -- pending | running | succeeded | failed | rolled_back | expired
    summary text not null,           -- json
    log text not null default '[]',  -- json list
    created_at text not null,
    expires_at text not null,
    approved_at text,
    finished_at text
);
create table if not exists apps (
    name text primary key,
    project text not null,
    image text not null,
    port integer not null,
    env text not null,
    volume_path text,
    npm_host_id integer,
    dns_record_ids text not null default '[]',
    created_at text not null,
    updated_at text not null
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def init(path: str) -> None:
    global _conn
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("pragma journal_mode=wal")
    _conn.executescript(SCHEMA)


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database not initialised")
    return _conn


def _row_to_deployment(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["env"] = json.loads(d["env"] or "{}")
    d["summary"] = json.loads(d["summary"])
    d["log"] = json.loads(d["log"])
    return d


def _row_to_app(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["env"] = json.loads(d["env"] or "{}")
    d["dns_record_ids"] = json.loads(d["dns_record_ids"])
    return d


def create_deployment(*, action: str, project: str, name: str, image: str | None, port: int | None,
                      env: dict, volume_path: str | None, summary: dict, ttl_hours: int) -> dict:
    dep_id = uuid.uuid4().hex[:12]
    created = now()
    with _lock:
        _db().execute(
            "insert into deployments (id, action, project, name, image, port, env, volume_path, status,"
            " summary, created_at, expires_at) values (?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (dep_id, action, project, name, image, port, json.dumps(env), volume_path, json.dumps(summary),
             iso(created), iso(created + timedelta(hours=ttl_hours))),
        )
    return get_deployment(dep_id)


def get_deployment(dep_id: str) -> dict | None:
    row = _db().execute("select * from deployments where id=?", (dep_id,)).fetchone()
    return _row_to_deployment(row) if row else None


def expire_pending() -> None:
    with _lock:
        _db().execute(
            "update deployments set status='expired' where status='pending' and expires_at < ?", (iso(now()),)
        )


def claim_for_run(dep_id: str) -> bool:
    """Atomically move pending -> running. Returns False if not pending (or expired)."""
    expire_pending()
    with _lock:
        cur = _db().execute(
            "update deployments set status='running', approved_at=? where id=? and status='pending'",
            (iso(now()), dep_id),
        )
        return cur.rowcount == 1


def running_for_name(name: str) -> bool:
    row = _db().execute("select 1 from deployments where name=? and status='running'", (name,)).fetchone()
    return row is not None


def append_log(dep_id: str, line: str) -> None:
    with _lock:
        row = _db().execute("select log from deployments where id=?", (dep_id,)).fetchone()
        log = json.loads(row["log"]) if row else []
        log.append(f"{now().strftime('%H:%M:%S')} {line}")
        _db().execute("update deployments set log=? where id=?", (json.dumps(log), dep_id))


def finish(dep_id: str, status: str) -> None:
    with _lock:
        _db().execute("update deployments set status=?, finished_at=? where id=?", (status, iso(now()), dep_id))


def get_app(name: str) -> dict | None:
    row = _db().execute("select * from apps where name=?", (name,)).fetchone()
    return _row_to_app(row) if row else None


def list_apps() -> list[dict]:
    return [_row_to_app(r) for r in _db().execute("select * from apps order by name").fetchall()]


def upsert_app(*, name: str, project: str, image: str, port: int, env: dict, volume_path: str | None,
               npm_host_id: int | None, dns_record_ids: list[str]) -> None:
    ts = iso(now())
    with _lock:
        _db().execute(
            "insert into apps (name, project, image, port, env, volume_path, npm_host_id, dns_record_ids,"
            " created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?)"
            " on conflict(name) do update set project=excluded.project, image=excluded.image,"
            " port=excluded.port, env=excluded.env, volume_path=excluded.volume_path,"
            " npm_host_id=excluded.npm_host_id, dns_record_ids=excluded.dns_record_ids,"
            " updated_at=excluded.updated_at",
            (name, project, image, port, json.dumps(env), volume_path, npm_host_id, json.dumps(dns_record_ids),
             ts, ts),
        )


def delete_app(name: str) -> None:
    with _lock:
        _db().execute("delete from apps where name=?", (name,))
