"""Read a recorded note: who the investor met, at which company, and what happens next.

The LLM (Nicrron) does this when NICRRON_API_KEY is set; otherwise keyword rules read only what the note states.
Copied from the real branch's engine.py (note extraction only).
"""
from __future__ import annotations

import datetime as dt
import re

SECTOR_KEYWORDS = {
    "ai": [" ai ", " ai,", " ai.", "llm", "machine learning", "agents", "ai-"],
    "devtools": ["developer", "devtool", "api", "open-source", "open source", "sdk", "infra", "observability", "webhook"],
    "fintech": ["fintech", "payments", "banking", "lending", "finance team", "accounting"],
    "consumer": ["consumer", "marketplace", "app for", "d2c", "dtc"],
    "cpg": ["cpg", "beverage", "snack", "grocery", "retail", "stores"],
    "climate": ["climate", "carbon", "energy", "solar", "battery"],
    "health": ["health", "clinic", "patient", "medical", "biotech"],
}
TECH_KEYWORDS = ["engineer", "phd", "machine learning", "open-source", "open source", "research", "cto"]
STOP = {"the", "a", "an", "my", "our", "his", "her", "their", "with", "some", "someone", "team", "him", "them", "us",
        "investors", "founders", "two", "three", "this", "that", "people", "everyone", "up", "today", "yesterday", "and",
        "to", "for", "at", "in", "on", "about", "who", "it", "again", "back", "recently", "earlier", "later"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

EMPTY_NOTE = {"founder": None, "company": None, "event": None, "sector": None, "stage": None, "one_liner": None,
              "metrics": {}, "background": [], "raising_usd": None, "next_step": None, "next_due": None}

NOTE_SYSTEM = """You read a short note a venture capital investor recorded right after meeting someone (often dictated, so
expect speech-to-text mistakes). Extract: founder (full name of the founder they met, or null), company (startup name or null),
event (where they met, e.g. "Deal Flow hackathon", or null), sector (one of: consumer, cpg, ai, devtools, fintech, climate,
health, other, or null), stage (idea|pre-seed|seed|series-a|later|null), one_liner (<=15 words on what the company does, or null),
metrics (object of numbers ONLY if stated: mrr_usd, stores, pilots, github_stars, users, paying_customers),
background (list of short strings about the founder), raising_usd (number|null),
next_step (the investor's own next action, short, or null), next_due (ISO date YYYY-MM-DD if a deadline is stated or implied
like "by Monday", else null). Today is {today} ({weekday}). Use null when not stated. NEVER invent names or numbers."""


# --------------------------------------------------------------------------- keyword extraction (no LLM key)
NAME = r"([A-Z][a-zà-ÿ'’]+(?:[ -][A-Z][a-zà-ÿ'’]+){0,2})"
COMPANY = r"([A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9](?:\s[A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9])?)"
EVENT_WORDS = r"(?:hackathon|conference|summit|meetup|demo day|dinner|breakfast|lunch|happy hour|event|panel|mixer|party|offsite|week|fair|expo)"


def _num(s: str) -> float:
    s = s.lower().replace(",", "").strip()
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0


def due_from(text: str, today: dt.date) -> str | None:
    low = text.lower()
    if "tomorrow" in low:
        return (today + dt.timedelta(days=1)).isoformat()
    if "today" in low or "tonight" in low:
        return today.isoformat()
    m = re.search(r"\bin (\d{1,2}) days?\b", low)
    if m:
        return (today + dt.timedelta(days=int(m.group(1)))).isoformat()
    if "next week" in low:
        return (today + dt.timedelta(days=7 - today.weekday())).isoformat()
    if "end of (the )?week" in low or re.search(r"\bend of (the )?week\b", low):
        return (today + dt.timedelta(days=(4 - today.weekday()) % 7)).isoformat()
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b(?:by|on|this|next)?\s*{d}\b", low):
            ahead = (i - today.weekday()) % 7 or 7
            return (today + dt.timedelta(days=ahead)).isoformat()
    return None


def heuristic_note(text: str, today: dt.date) -> dict:
    """Keyword extraction for when no LLM key is set. Only fills fields it can read straight from the note."""
    ext = {**EMPTY_NOTE, "metrics": {}, "background": []}
    low = f" {text.lower()} "
    # The verb is case-insensitive ("Met", "met"); the name must be capitalized. "intro to X" is who to introduce, not who was met.
    m = re.search(rf"\b(?i:met with|met|meeting with|talked to|talked with|spoke to|spoke with|chatted with|call with|"
                  rf"coffee with|lunch with|dinner with|caught up with|ran into|sat down with)\s+{NAME}", text)
    if m:
        ext["founder"] = m.group(1)
    else:
        # Dictation is sometimes all lowercase: take the one or two words after the verb unless they are common words.
        m = re.search(r"\b(?:met with|met|talked to|spoke with|spoke to|coffee with|call with|caught up with|ran into)\s+"
                      r"([a-zà-ÿ'’]+)(?:\s+([a-zà-ÿ'’]+))?", text.lower())
        if m and m.group(1) not in STOP:
            words = [m.group(1)] + ([m.group(2)] if m.group(2) and m.group(2) not in STOP else [])
            ext["founder"] = " ".join(w.capitalize() for w in words)
    m = re.search(rf"\b(?i:from|founder of|ceo of|cto of|co-founder of|cofounder of|startup called|company called)\s+{COMPANY}", text)
    if m and m.group(1).split()[0] not in ("The", "A", "An", "I", "We"):
        ext["company"] = m.group(1)
    elif ext["founder"]:
        # "met maya okafor, quillstack" or "met Maya Okafor from quillstack": the word right after the name or after "from".
        m = re.search(rf"{re.escape(ext['founder'].lower())}\s*(?:,|\(|from|of|at)\s*([a-z0-9][a-z0-9&.\-]+)", text.lower())
        if m and m.group(1) not in STOP:
            ext["company"] = m.group(1).strip(".-").capitalize()
    m = re.search(rf"\bat (?:the |a |an )?((?:[A-Z][\w'&-]*\s){{0,4}}{EVENT_WORDS})", text, flags=re.I)
    if m:
        ext["event"] = m.group(1).strip()
    for sector, words in SECTOR_KEYWORDS.items():
        if any(w in low for w in words):
            ext["sector"] = sector
            break
    if "pre-seed" in low or "preseed" in low or "pre seed" in low:
        ext["stage"] = "pre-seed"
    elif "series a" in low:
        ext["stage"] = "series-a"
    elif re.search(r"\bseed\b", low):
        ext["stage"] = "seed"
    elif "idea stage" in low or "pre-product" in low:
        ext["stage"] = "idea"
    m = re.search(r"\$\s?([\d.,]+\s?[km]?)\s*(?:mrr|/mo|a month|per month|monthly)", low)
    if m:
        ext["metrics"]["mrr_usd"] = _num(m.group(1))
    m = re.search(r"([\d.,]+\s?k?)\s*(?:github )?stars", low)
    if m:
        ext["metrics"]["github_stars"] = int(_num(m.group(1)))
    m = re.search(r"([\d,]+)\s*(?:paying )?customers", low)
    if m:
        ext["metrics"]["paying_customers"] = int(_num(m.group(1)))
    m = re.search(r"raising\s+(?:a\s+)?\$\s?([\d.,]+\s?[km]?)", low)
    if m:
        ext["raising_usd"] = _num(m.group(1))
    ext["background"] = [k for k in TECH_KEYWORDS if k in low][:3]
    m = re.search(r"\b(?:next steps?|next|to ?do|follow[- ]up)\s*[:\-–]\s*(.+?)(?:\.\s|\.$|\n|$)", text, flags=re.I)
    if not m:
        m = re.search(r"([^.!?\n]*\b(?:follow up|send|intro|introduce|schedule|set up|loop in|get (?:him|her|them)|book|email|call|ping|reach out)\b[^.!?\n]*)", text, flags=re.I)
    if m:
        ext["next_step"] = m.group(1).strip().rstrip(".")[:160]
        ext["next_due"] = due_from(ext["next_step"], today)
    return ext


def clean_note(ext: dict | None) -> dict:
    """Coerce an extraction (from the LLM or the keyword rules) to the stored shape."""
    ext = ext or {}
    out = {**EMPTY_NOTE}
    for k in ("founder", "company", "event", "sector", "stage", "one_liner", "next_step"):
        v = ext.get(k)
        out[k] = str(v).strip()[:160] if v not in (None, "", "null") else None
    if out["sector"]:
        out["sector"] = out["sector"].lower()
    if out["stage"]:
        out["stage"] = out["stage"].lower().replace(" ", "-")
    m = ext.get("metrics") if isinstance(ext.get("metrics"), dict) else {}
    out["metrics"] = {k: float(v) for k, v in m.items()
                      if k in ("mrr_usd", "stores", "pilots", "github_stars", "users", "paying_customers") and isinstance(v, (int, float)) and v}
    bg = ext.get("background") if isinstance(ext.get("background"), list) else []
    out["background"] = [str(b)[:80] for b in bg][:5]
    r = ext.get("raising_usd")
    out["raising_usd"] = float(r) if isinstance(r, (int, float)) and r > 0 else None
    d = ext.get("next_due")
    try:
        out["next_due"] = dt.date.fromisoformat(str(d)[:10]).isoformat() if d else None
    except ValueError:
        out["next_due"] = None
    return out
