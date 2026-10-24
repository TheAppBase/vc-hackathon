"""Optional AI integrations: PRISM (watches and guards the agent) and Nicrron (LLM that reads event notes).

Both are optional. Without NICRRON_API_KEY the app reads notes with keyword rules (engine.heuristic_note).
PRISM is only used for users who pick Auto mode; in Ask mode nothing is sent to PRISM.
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx

PRISM_HOST = os.getenv("PRISMTRACE_HOST", "https://api.prism.blockconvey.com").rstrip("/")
# The project id is not a secret (it comes from PRISM's setup brief); the API key is, and only lives in the environment.
PRISM_PROJECT = os.getenv("PRISMTRACE_PROJECT_ID") or os.getenv("PRISM_PROJECT_ID") or "b47943ae-502e-4b6e-baa6-389d4ca90449"


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


# --------------------------------------------------------------------------- PRISM
class Tracer:
    """PRISM by Block Convey: model calls (trace_llm), tool calls (trajectories), and the setup doctor."""

    def __init__(self):
        self.key = os.getenv("PRISMTRACE_API_KEY") or os.getenv("PRISM_API_KEY")
        self.project = PRISM_PROJECT
        self.live = bool(self.key)
        self.error = None
        self.pt = None
        self._status: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        if self.live:
            try:
                from prismtrace import PRISMtrace

                self.pt = PRISMtrace(api_key=self.key, host=PRISM_HOST, project_id=self.project)
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, f"PRISM init failed: {e}"

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
            self.error = f"trace_llm failed: {e}"

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
            self.error = f"submit_trajectory failed: {e}"

    def end_session(self, session_id: str, agent_id: str, agent_name: str):
        if self.pt and session_id:
            try:
                self.pt.end_session(session_id, reason="signed out", agent_id=agent_id, agent_name=agent_name)
            except Exception as e:
                self.error = f"end_session failed: {e}"

    def flush(self):
        """Serverless functions freeze after the response, so send queued traces before the request ends."""
        if self.pt:
            self.pt.flush(timeout=3.0)

    def status(self) -> dict:
        """Credential check (handshake) and live-trace check (setup doctor), cached for five minutes."""
        if not self.key:
            return {"credential": "missing", "detail": "PRISMTRACE_API_KEY is not set on the server", "live": False}
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
                    out["detail"] = str(r.json().get("detail") or r.text)[:200]
                except ValueError:
                    out["detail"] = r.text[:200]
            else:
                d = httpx.get(f"{PRISM_HOST}/api/setup-doctor", params={"project_id": self.project}, headers=headers, timeout=10)
                if d.status_code == 200:
                    j = d.json()
                    out.update(live=bool(j.get("live_connected")), blocked_step=j.get("blocked_step"), overall=j.get("overall"))
        except httpx.HTTPError as e:
            out.update(credential="unreachable", detail=str(e)[:200])
        with self._lock:
            self._status = (time.time(), out)
        return out


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

                self.client = OpenAI(api_key=self.key, base_url=self.base, timeout=30)
            except Exception as e:  # pragma: no cover
                self.live, self.error = False, f"Nicrron init failed: {e}"

    def chat(self, system: str, user: str, agent_name: str, max_tokens: int = 700, session_id=None, trace: bool = True,
             agent_id=None) -> str | None:
        if not self.client:
            return None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t0 = time.time()
        try:
            r = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.2, max_tokens=max_tokens)
        except Exception as e:
            self.error = f"Nicrron call failed: {e}"
            if trace:
                TRACER.llm(self.model, messages, f"[error] {e}"[:300], int((time.time() - t0) * 1000), None, agent_name,
                           session_id, agent_id)
            return None
        out = r.choices[0].message.content or ""
        if trace:
            TRACER.llm(self.model, messages, out, int((time.time() - t0) * 1000), getattr(r, "usage", None), agent_name,
                       session_id, agent_id)
        return out

    def json(self, system: str, user: str, agent_name: str, session_id=None, trace: bool = True, agent_id=None) -> dict | None:
        out = self.chat(system + "\nReturn ONLY valid JSON. No prose, no code fences.", user, agent_name,
                        session_id=session_id, trace=trace, agent_id=agent_id)
        if not out:
            return None
        txt = out.strip()
        try:
            return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        except ValueError:
            self.error = "Nicrron returned non-JSON output"
            return None


LLM_CLIENT = LLM()
