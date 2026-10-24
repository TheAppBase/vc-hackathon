"""Firm memory on Vercel Blob (private store). Every user, note and pipeline is one JSON blob.

Set BLOB_READ_WRITE_TOKEN (Vercel adds it when you connect a Blob store to the project). Without it, the same
JSON files are written under runtime/store/ so the app also runs locally.

Layout:
  users/<username>.json            account, interview profile, team, Google connection
  google/<email>.json              which username a Google account is linked to
  notes/<username>/<id>.json       dictated or typed notes
  pipeline/<username>.json         founders the agent sourced, so teammates' agents can see them
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

API = "https://blob.vercel-storage.com"
API_VERSION = "12"
TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN") or ""
LOCAL = Path(os.getenv("LAST_TOUCH_RUNTIME") or ("/tmp/last-touch" if os.getenv("VERCEL") else Path(__file__).parent / "runtime")) / "store"
_lock = threading.Lock()


class Exists(Exception):
    """put(..., overwrite=False) on a pathname that is already taken."""


def _store_id(token: str) -> str:
    # vercel_blob_rw_<storeId>_<secret>
    return token.removeprefix("vercel_blob_rw_").split("_", 1)[0].lower()


LIVE = TOKEN.startswith("vercel_blob_rw_")
STORE_ID = _store_id(TOKEN) if LIVE else ""


def mode() -> str:
    return "Vercel Blob (private)" if LIVE else f"local files ({LOCAL})"


def _headers(extra: dict | None = None) -> dict:
    return {"authorization": f"Bearer {TOKEN}", "x-api-version": API_VERSION, **(extra or {})}


def get(path: str):
    """Return the parsed JSON at path, or None if it does not exist."""
    if not LIVE:
        f = LOCAL / path
        return json.loads(f.read_text()) if f.exists() else None
    url = f"https://{STORE_ID}.private.blob.vercel-storage.com/{quote(path, safe='/')}"
    # Bust the CDN cache so a read right after an overwrite sees the new version.
    r = httpx.get(url, params={"v": str(time.time_ns())}, headers={"authorization": f"Bearer {TOKEN}", "cache-control": "no-cache"},
                  timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


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


def list_paths(prefix: str) -> list[str]:
    if not LIVE:
        base = LOCAL / prefix
        root = base if base.is_dir() else base.parent
        if not root.exists():
            return []
        return sorted(str(p.relative_to(LOCAL)) for p in root.rglob("*.json") if str(p.relative_to(LOCAL)).startswith(prefix))
    out, cursor = [], None
    while True:
        params = {"prefix": prefix, "limit": "1000", **({"cursor": cursor} if cursor else {})}
        r = httpx.get(API, params=params, headers=_headers(), timeout=20)
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
    url = f"https://{STORE_ID}.private.blob.vercel-storage.com/{quote(path, safe='/')}"
    r = httpx.post(f"{API}/delete", json={"urls": [url]}, headers=_headers(), timeout=20)
    if r.status_code != 404:
        r.raise_for_status()
