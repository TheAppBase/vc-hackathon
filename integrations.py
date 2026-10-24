"""Integrations: PRISM (watches and guards the agent), Nicrron (LLM that reads event notes), Akta (akta.pro, the agents'
web search for companies), Roundtable (mirror of the firm memory).

All are optional and only ever return real data: a missing key turns that integration off, it never falls back to
made-up data. Without NICRRON_API_KEY the app reads notes with keyword rules (engine.heuristic_note).
PRISM is only used for users who pick Auto mode; in Ask mode nothing is sent to PRISM.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import httpx

import store
from env import env, scrub

PRISM_HOST = env("PRISMTRACE_HOST", default="https://api.prism.blockconvey.com").rstrip("/")
# The project id is not a secret (it comes from PRISM's setup brief); the API key is, and only lives in the environment.
PRISM_PROJECT = env("PRISMTRACE_PROJECT_ID", "PRISM_PROJECT_ID", default="b47943ae-502e-4b6e-baa6-389d4ca90449")


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


# --------------------------------------------------------------------------- PRISM
class Tracer:
    """PRISM by Block Convey: model calls (trace_llm), tool calls (trajectories), and the setup doctor."""

    def __init__(self):
        self.key = env("PRISMTRACE_API_KEY", "PRISM_API_KEY")
        self.project = PRISM_PROJECT
        self.live = bool(self.key)
        self.error = None
        self.pt = None
        self._status: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        if self.live:
            try:
                from prismtrace import PRISMtrace

                # 8s, not the SDK's 30s: a slow PRISM must not hold up saving a note.
                self.pt = PRISMtrace(api_key=self.key, host=PRISM_HOST, project_id=self.project, timeout=8)
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, scrub(f"PRISM init failed: {e}")

    def llm(self, model, messages, output, latency_ms, usage, agent_name, session_id=None, agent_id=None):
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
                agent_id=agent_id or _slug(agent_name),
                agent_name=agent_name,
                session_id=session_id,
            )
        except Exception as e:
            self.error = scrub(f"trace_llm failed: {e}")

    def tool(self, agent_name: str, agent_id: str, session_id: str, tool: str, input_summary: str, output_summary: str,
             latency_ms: int, status: str = "success"):
        """Record one tool call (an agent skill) as a step of the user's conversation."""
        if not self.pt:
            return
        step = {"step_type": "tool_call", "tool_name": tool, "label": tool, "status": status, "duration_ms": latency_ms,
                "input_summary": input_summary[:2000], "output_summary": output_summary[:2000]}
        try:
            self.pt.submit_trajectory([step], agent_name=agent_name, agent_id=agent_id, conversation_id=session_id,
                                      final_status="error" if status == "error" else "success", async_send=True)
        except Exception as e:
            self.error = scrub(f"submit_trajectory failed: {e}")

    def end_session(self, session_id: str, agent_id: str, agent_name: str):
        if self.pt and session_id:
            try:
                self.pt.end_session(session_id, reason="signed out", agent_id=agent_id, agent_name=agent_name)
            except Exception as e:
                self.error = scrub(f"end_session failed: {e}")

    def flush(self, budget: float = 3.0):
        """Serverless functions freeze after the response, so send queued traces before the request ends, waiting at most
        `budget` seconds in total (the SDK's own flush waits up to its timeout per queued send)."""
        if not self.pt:
            return
        with self.pt._threads_lock:
            threads, self.pt._pending_threads[:] = self.pt._pending_threads[:], []
        deadline = time.time() + budget
        for t in threads:
            t.join(max(0.0, deadline - time.time()))

    def status(self) -> dict:
        """Credential check (handshake) and live-trace check (setup doctor), cached for five minutes."""
        if not self.key:
            return {"credential": "missing", "detail": "PRISMTRACE_API_KEY is not set on the server", "live": False}
        if not self.pt:
            return {"credential": "fail", "detail": self.error or "PRISM could not start", "live": False}
        with self._lock:
            if self._status and time.time() - self._status[0] < 300:
                return self._status[1]
        headers = {"X-PRISMtrace-Key": self.key, "Content-Type": "application/json"}
        out: dict = {"live": False}
        try:
            r = httpx.post(f"{PRISM_HOST}/api/setup-doctor/handshake", headers=headers, timeout=10,
                           json={"project_id": self.project, "send_test_trace": False, "client": "last-touch"})
            out["credential"] = "ok" if r.status_code == 200 else "fail"
            if r.status_code != 200:
                try:
                    out["detail"] = scrub(r.json().get("detail") or r.text)[:200]
                except ValueError:
                    out["detail"] = scrub(r.text)[:200]
            else:
                d = httpx.get(f"{PRISM_HOST}/api/setup-doctor", params={"project_id": self.project}, headers=headers, timeout=10)
                if d.status_code == 200:
                    j = d.json()
                    out.update(live=bool(j.get("live_connected")), blocked_step=j.get("blocked_step"), overall=j.get("overall"))
        except httpx.HTTPError as e:
            out.update(credential="unreachable", detail=scrub(e)[:200])
        with self._lock:
            self._status = (time.time(), out)
        return out


TRACER = Tracer()


# --------------------------------------------------------------------------- Nicrron
class LLM:
    """Nicrron API: OpenAI-compatible endpoint (https://api.nicrron.ai/v1) that routes to many providers' models.

    NICRRON_MODEL writes up notes (quality matters); NICRRON_FAST_MODEL decides what each message asks for (speed matters).
    There is no "nicrron/auto" model, so that old default is ignored."""

    DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"
    DEFAULT_FAST_MODEL = "anthropic/claude-haiku-5-5"

    def __init__(self):
        self.key = env("NICRRON_API_KEY")
        configured = env("NICRRON_MODEL")
        self.model = configured if configured and configured != "nicrron/auto" else self.DEFAULT_MODEL
        self.fast_model = env("NICRRON_FAST_MODEL", default=self.DEFAULT_FAST_MODEL)
        self.base = env("NICRRON_BASE_URL", default="https://api.nicrron.ai/v1")
        self.live = bool(self.key)
        self.error = None
        self.client = None
        if self.live:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=self.key, base_url=self.base, timeout=30)
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, scrub(f"Nicrron init failed: {e}")

    def chat(self, system: str, user: str, agent_name: str, max_tokens: int = 700, session_id=None, trace: bool = True,
             agent_id=None, fast: bool = False) -> str | None:
        if not self.client:
            return None
        model = self.fast_model if fast else self.model
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t0 = time.time()
        try:
            try:
                r = self._create(model, messages, max_tokens, fast)
            except Exception as e:
                # A configured model that doesn't exist: fall back to the default once.
                default = self.DEFAULT_FAST_MODEL if fast else self.DEFAULT_MODEL
                if model == default or "model" not in str(e).lower():
                    raise
                model = default
                r = self._create(model, messages, max_tokens, fast)
        except Exception as e:
            self.error = scrub(f"Nicrron ({model}) failed: {e}")[:240]
            if trace:
                TRACER.llm(model, messages, scrub(f"[error] {e}")[:300], int((time.time() - t0) * 1000), None, agent_name,
                           session_id, agent_id)
            return None
        self.error = None
        out = r.choices[0].message.content or ""
        if trace:
            TRACER.llm(model, messages, out, int((time.time() - t0) * 1000), getattr(r, "usage", None), agent_name,
                       session_id, agent_id)
        return out

    def _create(self, model: str, messages: list, max_tokens: int, fast: bool):
        # Reading a request must be quick (8s, no retry) so a slow AI falls back to phrase matching fast; writing up a
        # note may take longer (30s, one retry).
        client = self.client.with_options(timeout=8.0, max_retries=0) if fast else self.client.with_options(timeout=30.0, max_retries=1)
        return client.chat.completions.create(model=model, messages=messages, temperature=0.2, max_tokens=max_tokens)

    def json(self, system: str, user: str, agent_name: str, session_id=None, trace: bool = True, agent_id=None,
             max_tokens: int = 700, fast: bool = False) -> dict | None:
        out = self.chat(system + "\nReturn ONLY valid JSON. No prose, no code fences.", user, agent_name, max_tokens=max_tokens,
                        session_id=session_id, trace=trace, agent_id=agent_id, fast=fast)
        if not out:
            return None
        txt = out.strip()
        try:
            return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        except ValueError:
            self.error = "Nicrron returned non-JSON output"
            return None


LLM_CLIENT = LLM()


# --------------------------------------------------------------------------- Akta (akta.pro by Wokelo)
def _items(obj) -> list[dict]:
    """Akta responses differ by endpoint; find the list of records defensively. Only dict records are returned."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("data", "results", "companies", "articles", "items", "records", "industries", "news"):
            v = obj.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = _items(v)
                if inner:
                    return inner
        for v in obj.values():
            if isinstance(v, list) and any(isinstance(x, dict) for x in v):
                return [x for x in v if isinstance(x, dict)]
    return []


def _name(x: dict) -> str:
    return str(x.get("name") or x.get("company_name") or x.get("industry_name") or x.get("title") or "?")


class Akta:
    """akta.pro (docs.akta.pro): Company Search and Industry Search are free; List Generation costs credits
    (about 6 for 10 companies from a natural-language query), so it only runs when someone asks to source or look up.

    Results are cached in the firm memory (akta/<hash>.json) so the whole team shares one lookup.
    """

    TTL = 7 * 24 * 3600

    def __init__(self):
        self.key = env("AKTA_API_KEY")
        self.base = env("AKTA_BASE_URL", default="https://api.akta.pro/api").rstrip("/")
        self.live = bool(self.key)
        self.use_paid = env("AKTA_USE_PAID", default="1") == "1"
        self._local = threading.local()  # each request sees only its own error
        self.last_status = None  # for Settings: the last HTTP status, never the query or another user's details

    @property
    def error(self) -> str | None:
        return getattr(self._local, "error", None)

    @error.setter
    def error(self, value: str | None):
        self._local.error = value

    def _req(self, method: str, path: str, params=None, body=None, ttl: int | None = None):
        if not self.live:
            return None
        ck = "akta/" + hashlib.sha1(json.dumps([method, path, params, body], sort_keys=True).encode()).hexdigest() + ".json"
        try:
            hit = store.get(ck)
            if hit and time.time() - hit.get("t", 0) < (ttl or self.TTL):
                return hit["v"]
        except Exception:
            pass
        try:
            # akta.pro paths end in "/" (without it the API answers 307); follow redirects in case that changes.
            r = httpx.request(method, self.base + path, params=params, json=body, headers={"x-api-key": self.key},
                              timeout=30, follow_redirects=True)
        except httpx.HTTPError as e:
            self.error = scrub(f"akta.pro {path}: {e}")[:200]
            return None
        self.last_status = r.status_code
        if r.status_code >= 400:
            try:
                j = r.json()
                detail = (j.get("detail") or j.get("message")) if isinstance(j, dict) else j
            except ValueError:
                detail = None
            detail = detail or r.text
            hint = {401: "check AKTA_API_KEY", 403: "your akta.pro plan doesn't include this", 429: "rate limited, try again shortly",
                    422: "akta.pro rejected the request"}.get(r.status_code, "")
            self.error = scrub(f"akta.pro {path} {r.status_code}: {str(detail)[:140]}" + (f" ({hint})" if hint else ""))[:220]
            return None
        try:
            val = r.json() if r.content else {}
        except ValueError:
            self.error = "akta.pro returned something that isn't JSON"
            return None
        try:
            store.put(ck, {"t": time.time(), "v": val})
        except Exception:
            pass
        return val

    def company_search(self, query: str) -> list[dict]:
        return _items(self._req("GET", "/v1/company/search/", params={"query": query})) if query else []

    def industry_search(self, query: str) -> list[dict]:
        return _items(self._req("GET", "/v1/industry/search/", params={"query": query, "level": "l4"})) if query else []

    def companies_like(self, query: str, limit: int = 10) -> list[dict]:
        """List Generation from a natural-language query (credits). Cached for a day."""
        if not self.use_paid or not query:
            return []
        return _items(self._req("POST", "/v1/list/generate/companies/", body={"query": query, "limit": min(limit, 20)},
                                ttl=24 * 3600))

    def lookup(self, company: str | None, similar: bool = False) -> dict:
        """What akta.pro knows about a company: the matching record and its category (free), and with similar=True,
        similar companies (List Generation, credits)."""
        out = {"live": self.live, "match": None, "industry": None, "similar": []}
        if not self.live or not company:
            return out
        matches = self.company_search(company)
        if matches:
            m = matches[0]
            out["match"] = {"name": _name(m), "website": m.get("website") or m.get("domain"),
                            "status": m.get("company_status") or m.get("status"),
                            "description": str(m.get("description") or m.get("short_description") or "")[:240] or None}
            if m.get("product_category"):
                out["industry"] = {"name": str(m["product_category"])[:80]}
        if similar and out["match"]:
            about = (out["industry"] or {}).get("name")
            like = self.companies_like(f"startups similar to {company}" + (f" ({about})" if about else ""), 6)
            out["similar"] = [{"name": _name(c), "website": c.get("website") or c.get("domain")} for c in like
                              if _name(c).lower() != company.lower()][:5]
        return out


AKTA = Akta()


# --------------------------------------------------------------------------- Roundtable (mirror of the firm memory)
class Roundtable:
    """Mirrors every saved note into Roundtable as a shared note, over MCP (Streamable HTTP).

    Needs ROUNDTABLE_MCP_URL and ROUNDTABLE_MCP_TOKEN: the MCP URL and bearer key of a connection created for this app
    at roundtable.bot (Agents -> Connect agent). Vercel Blob stays the source of truth; Roundtable is a copy.
    """

    def __init__(self):
        self.url = env("ROUNDTABLE_MCP_URL")
        self.token = env("ROUNDTABLE_MCP_TOKEN")
        self.folder = env("ROUNDTABLE_FOLDER", default="last-touch")
        self.live = bool(self.url and self.token)
        self.error = None if self.live or not self.url else "ROUNDTABLE_MCP_TOKEN is missing (the MCP URL needs its bearer key)"
        self.session = None
        self._rpc_id = 0
        self._lock = threading.Lock()

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self._rpc_id += 1
            body["id"] = self._rpc_id
        r = httpx.post(self.url, headers=headers, json=body, timeout=15)
        r.raise_for_status()
        self.session = r.headers.get("mcp-session-id") or self.session
        if notify:
            return None
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            msgs = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
            msg = next(m for m in msgs if m.get("id") == body["id"])
        else:
            msg = r.json()
        if msg.get("error"):
            raise RuntimeError(msg["error"].get("message", msg["error"]))
        return msg.get("result")

    def save_note(self, title: str, markdown: str, request_id: str) -> str | None:
        """Save one shared note. Returns Roundtable's note id, or None if it is off or failed (the error is kept)."""
        if not self.live:
            return None
        with self._lock:
            try:
                if self._rpc_id == 0 or not self.session:
                    self._rpc_id = 0
                    self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "last-touch", "version": "1.0"}})
                    self._rpc("notifications/initialized", notify=True)
                # Roundtable expects the tool arguments nested under "request".
                res = self._rpc("tools/call", {"name": "save_note", "arguments": {"request": {
                    "kind": "note", "folder": self.folder, "title": title, "content": markdown, "request_id": request_id}}}) or {}
            except Exception as e:
                self.error, self.session = scrub(f"Roundtable save_note failed: {e}")[:200], None
                return None
        if res.get("isError"):
            self.error = scrub("Roundtable save_note failed: " + " ".join(c.get("text", "") for c in res.get("content", [])))[:160]
            return None
        data = res.get("structuredContent") or {}
        if isinstance(data, dict) and list(data) == ["result"]:
            data = data["result"]
        if not data:
            for c in res.get("content", []):
                try:
                    data = json.loads(c.get("text", ""))
                    break
                except ValueError:
                    continue
        data = data if isinstance(data, dict) else {}
        self.error = None
        return data.get("note_id") or data.get("id") or (data.get("note") or {}).get("id")


ROUNDTABLE = Roundtable()
