"""Firm memory on Vercel Blob (private store). Every user, note and agent action is one JSON blob.

Credentials, in the order the app tries them (Vercel provides one or both when you connect a Blob store to the project):
  BLOB_READ_WRITE_TOKEN             a static read-write token
  BLOB_STORE_ID + Vercel OIDC       the short-lived OIDC token Vercel sends with each request (x-vercel-oidc-token)
Without either, the same JSON files are written under runtime/store/ so the app also runs locally.

Layout:
  users/<username>.json            account, interview profile, team, safety mode
  notes/<username>/<id>.json       recorded event notes and what the agent read from them
  actions/<username>/<id>.json     every write the agent proposed, and what happened to it
  akta/<hash>.json                 akta.pro lookups, shared by the team
  config/session-secret.json       the session signing key when SECRET_KEY is not set, shared by every server instance
  attempts/<username>/<n>.json     reserves a username's one password try per 10-second window (deleted after)
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx

from env import env

API = "https://blob.vercel-storage.com"
API_VERSION = "12"
TOKEN = env("BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN")
LOCAL = Path(os.getenv("LAST_TOUCH_RUNTIME") or Path(__file__).parent / "runtime") / "store"
ON_VERCEL = bool(os.getenv("VERCEL"))
_lock = threading.Lock()


class Exists(Exception):
    """put(..., overwrite=False) on a pathname that is already taken."""


if TOKEN.startswith("vercel_blob_rw_"):
    KIND, STORE_ID = "token", TOKEN.removeprefix("vercel_blob_rw_").split("_", 1)[0]  # vercel_blob_rw_<storeId>_<secret>
elif env("BLOB_STORE_ID"):
    KIND, STORE_ID = "oidc", env("BLOB_STORE_ID").removeprefix("store_")
else:
    KIND, STORE_ID = "local", ""
LIVE = KIND != "local"
_http = httpx.Client(timeout=15)
HOST = f"https://{STORE_ID.lower()}.private.blob.vercel-storage.com"


def mode() -> str:
    return {"token": "Vercel Blob (private, read-write token)", "oidc": "Vercel Blob (private, OIDC)"}.get(
        KIND, "not connected (no BLOB_READ_WRITE_TOKEN or BLOB_STORE_ID)" if ON_VERCEL else f"local files ({LOCAL})")


def _bearer() -> str:
    if KIND == "token":
        return TOKEN
    token = ""
    try:
        from flask import has_request_context, request

        if has_request_context():
            token = (request.headers.get("x-vercel-oidc-token") or "").strip()
    except ImportError:
        pass
    token = token or env("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError("Vercel OIDC token missing: turn on OIDC in the project settings, or set BLOB_READ_WRITE_TOKEN")
    return token


def _headers(extra: dict | None = None) -> dict:
    """The headers the Vercel Blob SDK sends: bearer credential, API version, and the store id when using OIDC."""
    h = {"authorization": f"Bearer {_bearer()}", "x-api-version": API_VERSION}
    if KIND == "oidc":
        h["x-vercel-blob-store-id"] = STORE_ID
    return {**h, **(extra or {})}


def get(path: str):
    """Return the parsed JSON at path, or None if it does not exist."""
    if not LIVE:
        f = LOCAL / path
        return json.loads(f.read_text()) if f.exists() else None
    # cache=0 reads from origin storage, not the CDN, so a read right after an overwrite or delete sees the change
    # (otherwise it can take up to 60 seconds to propagate). Same as the Blob SDK's useCache: false.
    return _fetch(path, _headers())


def _fetch(path: str, headers: dict):
    r = _http.get(f"{HOST}/{quote(path, safe='/')}", params={"cache": "0"}, headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_many(paths: list[str]) -> list:
    """Read many blobs in parallel (a sequential read per note made team views take tens of seconds)."""
    if not LIVE or len(paths) < 2:
        return [get(p) for p in paths]
    headers = _headers()  # built here, inside the request, so the OIDC token from the request header is available
    with ThreadPoolExecutor(16) as pool:
        return list(pool.map(lambda p: _fetch(p, headers), paths))


def put(path: str, data, overwrite: bool = True):
    body = json.dumps(data, separators=(",", ":"), default=str).encode()
    if not LIVE:
        f = LOCAL / path
        with _lock:
            if not overwrite and f.exists():
                raise Exists(path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(body)
        return
    r = httpx.put(API, params={"pathname": path}, content=body, timeout=20, headers=_headers({
        "x-vercel-blob-access": "private", "x-add-random-suffix": "0", "x-allow-overwrite": "1" if overwrite else "0",
        "x-content-type": "application/json", "x-cache-control-max-age": "60"}))
    if not overwrite and 400 <= r.status_code < 500 and r.status_code not in (401, 403) and get(path) is not None:
        raise Exists(path)
    r.raise_for_status()


def list_many(prefixes: list[str]) -> list[list[str]]:
    """list_paths for several prefixes in parallel."""
    if not LIVE or len(prefixes) < 2:
        return [list_paths(p) for p in prefixes]
    headers = _headers()
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda p: list_paths(p, headers), prefixes))


def list_paths(prefix: str, headers: dict | None = None) -> list[str]:
    if not LIVE:
        base = LOCAL / prefix
        root = base if base.is_dir() else base.parent
        if not root.exists():
            return []
        return sorted(str(p.relative_to(LOCAL)) for p in root.rglob("*.json") if str(p.relative_to(LOCAL)).startswith(prefix))
    out, cursor = [], None
    while True:
        params = {"prefix": prefix, "limit": "1000", **({"cursor": cursor} if cursor else {})}
        r = _http.get(API, params=params, headers=headers or _headers(), timeout=20)
        r.raise_for_status()
        j = r.json()
        out += [b["pathname"] for b in j.get("blobs", [])]
        cursor = j.get("cursor")
        if not j.get("hasMore") or not cursor:
            return sorted(out)


def delete(path: str):
    if not LIVE:
        f = LOCAL / path
        if f.exists():
            f.unlink()
        return
    r = httpx.post(f"{API}/delete", json={"urls": [f"{HOST}/{quote(path, safe='/')}"]}, headers=_headers(), timeout=20)
    if r.status_code != 404:
        r.raise_for_status()


def shared_secret() -> str:
    """One random session key for every server instance, kept in the store (used when SECRET_KEY is not set)."""
    path = "config/session-secret.json"
    got = get(path)
    if got and got.get("key"):
        return got["key"]
    key = secrets.token_hex(32)
    try:
        put(path, {"key": key}, overwrite=False)
    except Exists:
        return get(path)["key"]  # another instance created it first
    return key
