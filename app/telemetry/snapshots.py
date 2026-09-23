"""
Content hashing and snapshot registration for agent telemetry.

Each register_* function writes at most one row per unique hash into the
matching *_snapshots table and returns the hash.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone


def content_hash(obj) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prompt_hash(base_prompt: str) -> str:
    return content_hash(base_prompt)


def _toolset_shape(tools) -> list:
    shaped = []
    for t in tools:
        schema = t.args_schema
        try:
            schema_repr = schema.model_json_schema()
        except AttributeError:
            schema_repr = str(schema)
        shaped.append({"name": t.name, "description": t.description, "args_schema": schema_repr})
    shaped.sort(key=lambda d: d["name"])
    return shaped


def toolset_hash(tools) -> str:
    return content_hash(_toolset_shape(tools))


def _menu_shape(menu) -> list:
    shaped = []
    for item in menu.items:
        shaped.append({"entity_id": item.entity_id, "name": item.name, "description": item.description})
    return shaped


def menu_hash(menu) -> str:
    return content_hash(_menu_shape(menu))


def _register(conn, table, column, hash_value, payload):
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE hash=?", (hash_value,)).fetchone()
    if exists is None:
        conn.execute(f"INSERT INTO {table} (hash, {column}, first_seen) VALUES (?, ?, ?)",
                     (hash_value, payload, _now()))
        conn.commit()
    return hash_value


def register_prompt(conn, base_prompt: str) -> str:
    return _register(conn, "prompt_snapshots", "text", prompt_hash(base_prompt), base_prompt)


def register_toolset(conn, tools) -> str:
    shape = _toolset_shape(tools)
    return _register(conn, "toolset_snapshots", "schemas_json", content_hash(shape),
                     json.dumps(shape, separators=(",", ":")))


def register_menu(conn, menu) -> str:
    shape = _menu_shape(menu)
    return _register(conn, "menu_snapshots", "items_json", content_hash(shape),
                     json.dumps(shape, separators=(",", ":")))
