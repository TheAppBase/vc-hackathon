"""Safety layer for what the agent writes into the shared firm memory. Reading is always allowed.

Notes never need the user's approval: they save straight away. Two protections:
1. Always on: secrets (API keys, private keys, passwords, card numbers) are redacted before a note reaches the firm
   memory, where teammates' agents can read it.
2. Auto mode: PRISM watches the agent and reviews every note before it is saved. A clear block stops the save; a pass
   or no clear answer saves it (the note is the user's own words, so the user is never asked to approve it).
In Ask mode PRISM is not used at all.
"""
from __future__ import annotations

import json
import re
import threading
import time

from env import scrub
from integrations import TRACER

SECRET_RES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "a private key"),
    (re.compile(r"\b(?:sk|pk|rk|pt-sk|sk-ant|sk-proj)[-_][A-Za-z0-9_-]{16,}"), "an API key"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "a GitHub token"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,40}"), "a Google API key"),
    (re.compile(r"\bvercel_blob_rw_[A-Za-z0-9_]{10,}"), "a Vercel Blob token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS key"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "a bearer token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a token"),
    # "password: hunter22", "pin is 4455", "pw = x9!k"; not "Pinecone", "pin down" or "password reset".
    (re.compile(r"\b(?:password|passcode|passwd|pwd|pw|pin code|pin)\b\s*(?::|=|\bis\b)\s*['\"]?(?=[^\s'\"]*(?:\d|_|[^\w\s'\"]))[^\s'\"]{3,}['\"]?", re.I), "a password"),
]
CARD_RE = re.compile(r"(?<![\d+])[2-6](?:[ -]?\d){12,18}(?!\d)")  # payment cards start with 2-6
MAX_NOTE = 20000
# PRISM scores many things (task completion, efficiency, reliability...). Only guardrail / policy / safety results, or an
# explicit block decision, may stop a note; a low quality score must never block it.
SAFETY = re.compile(r"(?:^|_|\b)(?:guardrails?|polic(?:y|ies)|safety|safe|unsafe|harm(?:ful)?|toxic(?:ity)?|pii|privacy|secrets?|"
                    r"jailbreak|injection|abuse|violence|violent|hate|sexual|security|compliance|moderation|content_filter)"
                    r"(?:$|_|\b)", re.I)
BLOCK_WORDS = {"block", "blocked", "deny", "denied", "reject", "rejected", "unsafe", "disallow", "disallowed"}
FAIL = {"fail", "failed", "flagged", "violation", "violated", "high", "critical", "severe", "non_compliant", "noncompliant",
        "harmful", "triggered", "detected"} | BLOCK_WORDS
PASS = {"pass", "passed", "safe", "approved", "approve", "allow", "allowed", "compliant", "low", "none", "ok"}
PENDING = {"pending", "queued", "running", "processing", "in_progress", "evaluating"}


def live() -> bool:
    return TRACER.pt is not None


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch) * (2 if alt else 1)
        total += d - 9 if d > 9 else d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Replace secrets with [redacted] and say what was removed. Card numbers must pass the Luhn check, so phone numbers
    and other long numbers stay."""
    found = []
    for rx, why in SECRET_RES:
        if rx.search(text):
            found.append(why)
            text = rx.sub("[redacted]", text)

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("a card number")
            return "[redacted]"
        return m.group(0)

    text = CARD_RE.sub(card, text)
    return text, list(dict.fromkeys(found))


def _decide(ev) -> tuple[str, str]:
    """Read PRISM's evaluation. Returns (allow|block|pending|unknown, reason). Blocks only on safety signals."""
    if ev is None:
        return "unknown", "PRISM returned no evaluation"
    found: list[tuple[str, str]] = []

    def walk(x, path: tuple):
        key = path[-1] if path else ""
        safety = any(SAFETY.search(p) for p in path)
        where = ".".join(path) or "evaluation"
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, path + (str(k).lower(),))
        elif isinstance(x, list):
            if x and (key in ("guardrail_violations", "policy_violations", "triggered_guardrails", "safety_violations")
                      or (safety and key in ("violations", "flags", "issues", "triggered", "failures", "detections"))):
                found.append(("block", f"{where}: {json.dumps(x, default=str)[:160]}"))
            for v in x:
                walk(v, path)
        elif isinstance(x, bool):
            if not safety:
                return
            if key in ("passed", "pass", "safe", "is_safe", "approved", "allowed", "compliant") or SAFETY.fullmatch(key or "x"):
                found.append(("allow" if x else "block", f"{where}={x}"))
            elif key in ("blocked", "block", "flagged", "triggered", "detected", "unsafe", "violation", "is_violation", "violated"):
                found.append(("block" if x else "allow", f"{where}={x}"))
        elif isinstance(x, str):
            v = re.sub(r"[\s-]+", "_", x.strip().lower())
            if key in ("decision", "verdict", "action") and v in BLOCK_WORDS and (safety or len(path) <= 2):
                found.append(("block", f"{where}={v}"))
            elif key in ("status", "evaluation_status", "state") and v in PENDING:
                found.append(("pending", f"{where}={v}"))
            elif safety and (key in ("status", "verdict", "result", "decision", "outcome", "risk", "risk_level", "severity", "label")
                             or SAFETY.fullmatch(key or "x")):
                if v in FAIL:
                    found.append(("block", f"{where}={v}"))
                elif v in PASS:
                    found.append(("allow", f"{where}={v}"))

    walk(ev, ())
    for want in ("block", "pending", "allow"):
        hits = [r for d, r in found if d == want]
        if hits:
            return want, hits[0]
    return "unknown", "no guardrail or safety result in PRISM's evaluation"


def _bounded(fn, seconds: float):
    """Run one PRISM call, but give up after `seconds` (the call keeps running in the background and is ignored)."""
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        raise TimeoutError("PRISM didn't answer in time")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def review(agent: str, agent_id: str, session_id: str, action: dict, budget: float = 8.0) -> dict:
    """Ask PRISM whether the agent may take this action, within one overall time budget."""
    pt = TRACER.pt
    if not pt:
        return {"decision": "unknown", "reason": "PRISM is not configured"}
    deadline = time.time() + budget
    p = action["payload"]
    steps = [
        {"step_type": "reasoning", "label": "Proposed write", "status": "success",
         "input_summary": f"{agent} wants to: {action['summary']}",
         "output_summary": "Waiting for PRISM's review before writing to the shared firm memory. Reads are allowed."},
        # The whole note, not just its start, so nothing is saved unreviewed.
        {"step_type": "tool_call", "tool_name": action["type"], "label": action["summary"][:120], "status": "success",
         "input_summary": str(p.get("text") or "")[:MAX_NOTE],
         "output_summary": json.dumps({k: v for k, v in p.items() if k != "text"}, ensure_ascii=False, default=str)[:4000]},
    ]
    try:
        res = _bounded(lambda: pt.submit_trajectory(steps, agent_name=agent, agent_id=agent_id, conversation_id=session_id,
                                                    request_id=action["id"], final_status="success"), deadline - time.time())
    except Exception as e:
        return {"decision": "unknown", "reason": scrub(f"PRISM error: {e}")[:140]}
    tid = (res or {}).get("id") or (res or {}).get("trajectory_id")
    if not tid:
        return {"decision": "unknown", "reason": "PRISM did not accept the review request"}
    decision, why, last = "unknown", "PRISM did not answer in time", None
    while time.time() < deadline - 0.2:
        try:
            last = _bounded(lambda: pt.get_trajectory_evaluation(tid), deadline - time.time())
            decision, why = _decide(last)
        except Exception as e:
            decision, why = "unknown", scrub(f"PRISM error: {e}")[:140]
            break
        if decision in ("allow", "block"):
            break
        status = str(last.get("status") or last.get("evaluation_status") or "").lower() if isinstance(last, dict) else ""
        if decision != "pending" and status in ("completed", "complete", "done", "finished", "failed", "error", "success", "succeeded"):
            break  # PRISM has finished and nothing is still pending; polling again won't change its answer
        time.sleep(min(1.5, max(0.0, deadline - time.time() - 0.2)))
    if decision == "pending":
        decision, why = "unknown", "PRISM is still evaluating"
    return {"decision": decision, "reason": why, "trajectory_id": tid, "raw": scrub(json.dumps(last, default=str))[:2000] if last else None}
