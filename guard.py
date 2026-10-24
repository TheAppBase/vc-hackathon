"""Safety gate for every write the agent makes (send an email, save to firm memory). Reads are always allowed.

Two layers:
1. Hard rules (always on, both modes): caps on recipients, length and send rate, and no secrets or card numbers in an email.
2. In Auto mode, PRISM reviews the proposed action. The action is submitted as a PRISM trajectory, PRISM evaluates it,
   and the agent only goes ahead on a clear pass. If PRISM blocks it, it is blocked. If PRISM is unreachable or its
   answer is unclear, the agent falls back to asking the user (fail closed, never fail open).
In Ask mode PRISM is not used: every write waits for the user's approval.
"""
from __future__ import annotations

import json
import re
import time

from integrations import TRACER

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SECRET_RES = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "looks like a card or account number"),
    (re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"), "contains an API key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "contains an AWS key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "contains a private key"),
    (re.compile(r"\bpassword\s*[:=]", re.I), "contains a password"),
]
MAX_RECIPIENTS = 3
MAX_EMAILS_PER_DAY = 25
PASS = {"pass", "passed", "safe", "approved", "approve", "allow", "allowed", "compliant", "low"}
FAIL = {"fail", "failed", "unsafe", "blocked", "block", "deny", "denied", "flagged", "violation", "reject", "rejected",
        "high", "critical", "non_compliant", "noncompliant"}
PENDING = {"pending", "queued", "running", "processing", "in_progress", "evaluating"}


def live() -> bool:
    return TRACER.pt is not None


def hard_rules(kind: str, payload: dict, sent_today: int) -> str | None:
    """Return a reason to block, or None."""
    if kind == "send_email":
        to = payload.get("to") or []
        if not to:
            return "No recipient"
        if len(to) > MAX_RECIPIENTS:
            return f"More than {MAX_RECIPIENTS} recipients"
        bad = [t for t in to if not EMAIL_RE.match(t)]
        if bad:
            return f"Invalid address: {bad[0]}"
        body = payload.get("body") or ""
        if not body.strip():
            return "Empty email"
        if len(body) > 5000 or len(payload.get("subject") or "") > 200:
            return "Email is too long"
        for rx, why in SECRET_RES:
            if rx.search(body) or rx.search(payload.get("subject") or ""):
                return f"Blocked: the email {why}"
        if sent_today >= MAX_EMAILS_PER_DAY:
            return f"Daily limit of {MAX_EMAILS_PER_DAY} emails reached"
    elif kind == "save_note":
        text = payload.get("text") or ""
        if not text.strip():
            return "Empty note"
        if len(text) > 8000:
            return "Note is too long"
        for rx, why in SECRET_RES[1:]:
            if rx.search(text):
                return f"Blocked: the note {why}"
    elif kind in ("share_pipeline", "share_contacts"):
        if len(payload.get("items") or []) > 200:
            return "Too many items"
    return None


def _decide(ev) -> tuple[str, str]:
    """Read PRISM's evaluation defensively. Returns (allow|block|pending|unknown, reason)."""
    if ev is None:
        return "unknown", "PRISM returned no evaluation"
    found: list[tuple[str, str]] = []

    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k.lower())
        elif isinstance(x, list):
            if key in ("violations", "flags", "issues", "blocked_reasons", "failures") and x:
                found.append(("block", f"{key}: {json.dumps(x, default=str)[:160]}"))
            for v in x:
                walk(v, key)
        elif isinstance(x, bool):
            if key in ("passed", "pass", "safe", "approved", "allowed", "compliant"):
                found.append(("allow" if x else "block", key))
            elif key in ("blocked", "flagged", "unsafe", "violation", "is_violation"):
                found.append(("block" if x else "allow", key))
        elif isinstance(x, str) and key in ("status", "verdict", "result", "decision", "outcome", "risk", "risk_level",
                                             "evaluation_status", "label"):
            v = x.strip().lower()
            if v in FAIL:
                found.append(("block", f"{key}={v}"))
            elif v in PENDING:
                found.append(("pending", f"{key}={v}"))
            elif v in PASS:
                found.append(("allow", f"{key}={v}"))

    walk(ev)
    for want in ("block", "pending", "allow"):
        hits = [r for d, r in found if d == want]
        if hits:
            return want, hits[0]
    return "unknown", "PRISM's evaluation had no clear verdict"


def review(agent: str, username: str, action: dict, wait_s: float = 12.0) -> dict:
    """Ask PRISM whether the agent may take this action."""
    pt = TRACER.pt
    if not pt:
        return {"decision": "unknown", "reason": "PRISM is not configured"}
    steps = [
        {"step_type": "reasoning", "label": "Proposed action", "status": "success",
         "input_summary": f"{agent} wants to: {action['summary']}",
         "output_summary": "Waiting for PRISM approval before acting. Reads are allowed; this is a write."},
        {"step_type": "tool_call", "tool_name": action["type"], "label": action["summary"][:120], "status": "success",
         "input_summary": json.dumps(action["payload"], default=str)[:4000]},
    ]
    res = pt.submit_trajectory(steps, agent_name=agent, agent_id=f"last-touch-{username}", conversation_id=action["id"],
                               request_id=action["id"], final_status="success")
    tid = (res or {}).get("id") or (res or {}).get("trajectory_id")
    if not tid:
        return {"decision": "unknown", "reason": "PRISM did not accept the review request"}
    deadline = time.time() + wait_s
    decision, why = "unknown", "PRISM did not answer in time"
    while time.time() < deadline:
        decision, why = _decide(pt.get_trajectory_evaluation(tid))
        if decision in ("allow", "block"):
            break
        time.sleep(1.5)
    if decision == "pending":
        decision, why = "unknown", "PRISM is still evaluating"
    return {"decision": decision, "reason": why, "trajectory_id": tid}
