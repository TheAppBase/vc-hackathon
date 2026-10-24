"""Sponsor integrations: Nicrron (LLM), Akta (external data), PRISM (tracing), Roundtable (shared firm memory).

Every integration is optional. When a key is missing or a call fails, the app falls back to cached or demo data and
the UI says so, so the live demo never breaks.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
CACHE_FILE = RUNTIME / "cache.json"
_lock = threading.Lock()


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


# --------------------------------------------------------------------------- cache
def _cache_load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


_CACHE = _cache_load()


def cached(namespace: str, key_obj, fn):
    """Memoise expensive calls (LLM + Akta) to runtime/cache.json so demo replays are instant."""
    key = namespace + ":" + hashlib.sha1(json.dumps(key_obj, sort_keys=True, default=str).encode()).hexdigest()
    with _lock:
        if key in _CACHE:
            return _CACHE[key]
    val = fn()
    with _lock:
        _CACHE[key] = val
        CACHE_FILE.write_text(json.dumps(_CACHE, indent=1))
    return val


def clear_cache():
    with _lock:
        _CACHE.clear()
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()


# --------------------------------------------------------------------------- PRISM
class Tracer:
    """PRISM by Block Convey. Logs every LLM call (trace_llm) and every agent run (submit_trajectory)."""

    def __init__(self):
        self.key = os.getenv("PRISMTRACE_API_KEY")
        self.project = os.getenv("PRISMTRACE_PROJECT_ID")
        self.live = bool(self.key and self.project)
        self.error = None
        self.local: list[dict] = []  # always kept, shown in the UI "Trace" tab
        self.pt = None
        if self.live:
            try:
                from prismtrace import PRISMtrace

                self.pt = PRISMtrace(
                    api_key=self.key,
                    host=os.getenv("PRISMTRACE_HOST", "https://api.prism.blockconvey.com"),
                    project_id=self.project,
                )
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, f"PRISM init failed: {e}"

    def llm(self, model, messages, output, latency_ms, usage, agent_name, session_id=None):
        if not self.pt:
            return
        try:
            self.pt.trace_llm(
                model=model,
                input_messages=messages,
                output=output,
                latency_ms=latency_ms,
                token_count_input=getattr(usage, "prompt_tokens", 0) or 0,
                token_count_output=getattr(usage, "completion_tokens", 0) or 0,
                agent_id=_slug(agent_name),
                agent_name=agent_name,
                session_id=session_id,
            )
        except Exception as e:
            self.error = f"trace_llm failed: {e}"

    def trajectory(self, agent_name: str, steps: list[dict], run_id: str, model: str | None = None):
        rec = {"agent": agent_name, "run_id": run_id, "steps": steps, "prism_id": None}
        if self.pt:
            try:
                res = self.pt.submit_trajectory(
                    steps, agent_name=agent_name, agent_id=_slug(agent_name), conversation_id=run_id, model=model
                )
                rec["prism_id"] = (res or {}).get("id")
            except Exception as e:
                self.error = f"submit_trajectory failed: {e}"
        self.local.append(rec)
        return rec


TRACER = Tracer()


# --------------------------------------------------------------------------- Nicrron
class LLM:
    """Nicrron API: OpenAI-compatible endpoint (https://api.nicrron.ai/v1)."""

    def __init__(self):
        self.key = os.getenv("NICRRON_API_KEY")
        self.model = os.getenv("NICRRON_MODEL", "nicrron/auto")
        self.base = os.getenv("NICRRON_BASE_URL", "https://api.nicrron.ai/v1")
        self.live = bool(self.key)
        self.error = None
        self.client = None
        if self.live:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=self.key, base_url=self.base, timeout=60)
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, f"Nicrron init failed: {e}"

    def chat(self, system: str, user: str, agent_name: str, max_tokens: int = 700, session_id=None) -> str | None:
        if not self.client:
            return None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        def call():
            t0 = time.time()
            r = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.2, max_tokens=max_tokens
            )
            out = r.choices[0].message.content or ""
            TRACER.llm(self.model, messages, out, int((time.time() - t0) * 1000), getattr(r, "usage", None), agent_name, session_id)
            return out

        try:
            return cached("llm", [self.model, system, user], call)
        except Exception as e:
            self.error = f"Nicrron call failed: {e}"
            return None

    def json(self, system: str, user: str, agent_name: str, session_id=None) -> dict | None:
        out = self.chat(system + "\nReturn ONLY valid JSON. No prose, no code fences.", user, agent_name, session_id=session_id)
        if not out:
            return None
        txt = out.strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            txt = txt[txt.find("{"):]
        try:
            return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        except Exception:
            self.error = "Nicrron returned non-JSON output"
            return None


# --------------------------------------------------------------------------- Akta
def _items(obj) -> list:
    """Akta responses differ by endpoint; find the list of records defensively."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("data", "results", "companies", "articles", "items", "records", "industries", "news"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = _items(v)
                if inner:
                    return inner
        for v in obj.values():
            if isinstance(v, list):
                return v
    return []


def _name(x: dict) -> str:
    return str(x.get("name") or x.get("company_name") or x.get("industry_name") or x.get("title") or "?")


class Akta:
    """akta.pro by Wokelo: company search (free), industry search (free), company lists and news (credits)."""

    def __init__(self):
        self.key = os.getenv("AKTA_API_KEY")
        self.base = os.getenv("AKTA_BASE_URL", "https://api.akta.pro/api")
        self.live = bool(self.key)
        self.use_paid = os.getenv("AKTA_USE_PAID", "1") == "1"
        self.error = None
        self.fixtures = json.loads((DATA / "akta_fixtures.json").read_text())

    def _req(self, method: str, path: str, params=None, body=None):
        if not self.live:
            return None

        def call():
            r = httpx.request(
                method, self.base + path, params=params, json=body, headers={"x-api-key": self.key}, timeout=30
            )
            if r.status_code == 204:
                return {}
            r.raise_for_status()
            return r.json()

        try:
            return cached("akta", [method, path, params, body], call)
        except Exception as e:
            self.error = f"Akta {path} failed: {e}"
            return None

    def company_search(self, query: str) -> list[dict]:
        return _items(self._req("GET", "/v1/company/search", params={"query": query}))

    def industry_search(self, query: str) -> list[dict]:
        return _items(self._req("GET", "/v1/industry/search", params={"query": query, "level": "l4"}))

    def news(self, **params) -> list[dict]:
        if not self.use_paid:
            return []
        params.setdefault("limit", 3)
        return _items(self._req("GET", "/v1/news", params=params))

    def comps(self, query: str, limit: int = 5) -> list[dict]:
        if not self.use_paid:
            return []
        return _items(self._req("POST", "/v1/list/generate/companies/", body={"query": query, "limit": limit}))

    def enrich_lead(self, company: str | None, website: str | None, one_liner: str) -> dict:
        """Everything the brief needs from outside data. Falls back to labelled fixtures."""
        out = {"live": False, "match": None, "industry": None, "comps": [], "news": [], "source": "demo fixture"}
        if self.live:
            q = website or company
            matches = self.company_search(q) if q else []
            if matches:
                m = matches[0]
                out["match"] = {"name": _name(m), "website": m.get("website"), "status": m.get("company_status")}
            inds = self.industry_search(one_liner) if one_liner else []
            if inds:
                out["industry"] = {"name": _name(inds[0]), "code": inds[0].get("code")}
            comps = self.comps(one_liner) if one_liner else []
            out["comps"] = [{"name": _name(c), "website": c.get("website")} for c in comps[:5]]
            if out["industry"] and out["industry"].get("code"):
                arts = self.news(industry=out["industry"]["code"], group_articles="true")
                out["news"] = [
                    {"title": _name(a), "date": a.get("published_date") or a.get("date") or a.get("published_at"), "url": a.get("url")}
                    for a in arts[:3]
                ]
            if out["match"] or out["industry"] or out["comps"] or out["news"]:
                out["live"], out["source"] = True, "Akta (live)"
        if not out["live"]:
            names = self.fixtures["comps"].get(company or "", [])
            out["comps"] = [{"name": n, "website": None} for n in names]
        return out

    def signal_for(self, company: str, website: str | None) -> dict | None:
        """Watcher: has anything happened to a parked company?"""
        if self.live and website and not website.endswith(".example"):
            arts = self.news(company=website, limit=3)
            if arts:
                a = arts[0]
                return {"text": _name(a), "source": "Akta (live)", "url": a.get("url")}
        sim = self.fixtures["signals"].get(company)
        return {"text": sim, "source": "demo fixture"} if sim else None


# --------------------------------------------------------------------------- Roundtable
class NotesStore:
    """Firm-wide memory. One note per founder.

    Local JSON always works. Roundtable: Arjun's advice was to use shared NOTES (fastest, visual).
    We don't know Roundtable's exact API shape yet, so:
      1) Every note is also written as Markdown to runtime/roundtable_outbox/. A Roundtable-connected agent
         (Claude Code / custom agent connected per the quickstart) can post these as shared notes.
      2) If ROUNDTABLE_NOTES_ENDPOINT + ROUNDTABLE_API_KEY are set, push() POSTs each note there.
         TODO: adjust push() to the payload in the "Build on Roundtable" quickstart.
    """

    def __init__(self):
        self.path = RUNTIME / "notes.json"
        self.outbox = RUNTIME / "roundtable_outbox"
        self.outbox.mkdir(exist_ok=True)
        self.endpoint = os.getenv("ROUNDTABLE_NOTES_ENDPOINT")
        self.key = os.getenv("ROUNDTABLE_API_KEY")
        self.live = bool(self.endpoint and self.key)
        self.error = None
        self.pushed = 0

    def save_all(self, notes: list[dict], render_md):
        self.path.write_text(json.dumps(notes, indent=1, default=str))
        for n in notes:
            (self.outbox / f"{n['id']}.md").write_text(render_md(n))
            self.push(n, render_md(n))

    def push(self, note: dict, markdown: str):
        if not self.live:
            return
        try:
            r = httpx.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.key}"},
                json={"title": f"{note.get('company') or note.get('founder')}: {note.get('status')}", "content": markdown},
                timeout=20,
            )
            r.raise_for_status()
            self.pushed += 1
        except Exception as e:
            self.error = f"Roundtable push failed: {e}"


def status() -> dict:
    return {
        "Nicrron (LLM)": ("live" if LLM_CLIENT.live else "cached demo", LLM_CLIENT.error),
        "Akta (external data)": ("live" if AKTA.live else "demo fixture", AKTA.error),
        "PRISM (tracing)": ("live" if TRACER.live else "local trace", TRACER.error),
        "Roundtable (firm memory)": ("live" if NOTES.live else "local + outbox", NOTES.error),
    }


LLM_CLIENT = LLM()
AKTA = Akta()
NOTES = NotesStore()
