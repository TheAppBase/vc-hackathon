"""Optional AI integrations: Nicrron (LLM, extracts fields from founder emails) and PRISM (traces every LLM call).

Both are optional. Without NICRRON_API_KEY the app extracts with keyword rules (engine.heuristic_extract).
"""
from __future__ import annotations

import json
import os
import time


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


# --------------------------------------------------------------------------- PRISM
class Tracer:
    """PRISM by Block Convey. Logs every LLM call (trace_llm)."""

    def __init__(self):
        self.key = os.getenv("PRISM_API_KEY") or os.getenv("PRISMTRACE_API_KEY")
        self.project = os.getenv("PRISM_PROJECT_ID") or os.getenv("PRISMTRACE_PROJECT_ID")
        self.live = bool(self.key and self.project)
        self.error = None
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

    def chat(self, system: str, user: str, agent_name: str, max_tokens: int = 700, session_id=None, trace: bool = True) -> str | None:
        if not self.client:
            return None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        def call():
            t0 = time.time()
            r = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.2, max_tokens=max_tokens
            )
            out = r.choices[0].message.content or ""
            if trace:
                TRACER.llm(self.model, messages, out, int((time.time() - t0) * 1000), getattr(r, "usage", None), agent_name, session_id)
            return out

        try:
            return call()
        except Exception as e:
            self.error = f"Nicrron call failed: {e}"
            return None

    def json(self, system: str, user: str, agent_name: str, session_id=None, trace: bool = True) -> dict | None:
        out = self.chat(system + "\nReturn ONLY valid JSON. No prose, no code fences.", user, agent_name, session_id=session_id, trace=trace)
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


LLM_CLIENT = LLM()
