"""Reading secrets from the environment, and keeping them out of anything shown or logged.

Keys pasted into a host's settings often carry a trailing newline, which breaks HTTP headers ("Illegal header value"),
so every value is stripped. scrub() removes every configured secret from text (error messages, statuses) before it can
reach the browser or the logs.
"""
from __future__ import annotations

import os
import re

SECRET_NAMES = ("PRISMTRACE_API_KEY", "PRISM_API_KEY", "NICRRON_API_KEY", "AKTA_API_KEY", "ROUNDTABLE_MCP_TOKEN",
                "BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN", "SECRET_KEY")
_KEY_SHAPES = re.compile(r"(pt-sk-|vercel_blob_rw_|sk-|Bearer\s+)[A-Za-z0-9_\-.]{6,}")


def env(*names: str, default: str = "") -> str:
    """The first of these environment variables that is set to something other than whitespace, stripped."""
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return default


def scrub(text) -> str:
    """Replace every configured secret (and anything shaped like a key) with [hidden]."""
    s = str(text)
    for n in SECRET_NAMES:
        v = (os.getenv(n) or "").strip()
        if len(v) >= 6:
            s = s.replace(v, "[hidden]")
    return _KEY_SHAPES.sub(lambda m: m.group(1) + "[hidden]", s)
