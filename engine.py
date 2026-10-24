"""Last Touch rules: read founder details out of recorded event notes and score them with a person's criteria.

Scoring and follow-up status are plain code so every decision is explainable. The LLM (Nicrron) writes up notes,
extracts their fields and decides what each message asks for; without a key, the keyword rules below do it.
"""
from __future__ import annotations

import datetime as dt
import re

FIT_KEYWORDS = {
    "cpg": ["p&g", "unilever", "brand manager", "cpg", "grocer", "beverage", "category"],
    "consumer": ["doordash", "instacart", "consumer", "marketplace", "restaurant"],
    "devtools": ["datadog", "stripe", "github", "api", "infra", "engineer"],
    "ai": ["ml", "machine learning", "phd", "research", "data scientist", "engineer"],
    "fintech": ["stripe", "plaid", "payments", "bank", "fintech"],
}
SECTOR_KEYWORDS = {
    "ai": ["ai", "llm", "llms", "machine learning", "ai agents", "agents", "genai", "generative ai"],
    "devtools": ["developer", "developers", "devtool", "devtools", "api", "apis", "open-source", "open source", "sdk",
                 "infra", "infrastructure", "observability", "webhook", "webhooks"],
    "fintech": ["fintech", "payments", "banking", "lending", "finance team", "finance teams", "accounting"],
    "consumer": ["consumer", "marketplace", "app for", "d2c", "dtc"],
    "cpg": ["cpg", "beverage", "beverages", "snack", "snacks", "grocery", "retail", "stores"],
    "climate": ["climate", "carbon", "energy", "solar", "battery", "batteries"],
    "health": ["health", "healthcare", "clinic", "clinics", "patient", "patients", "medical", "biotech"],
}
TECH_KEYWORDS = ["engineer", "phd", "machine learning", "open-source", "open source", "research", "cto"]
STOP = {"the", "a", "an", "my", "our", "his", "her", "their", "with", "some", "someone", "team", "him", "them", "us",
        "investors", "founders", "founder", "two", "three", "four", "this", "that", "these", "those", "people", "everyone",
        "up", "today", "yesterday", "tonight", "and", "to", "for", "at", "in", "on", "about", "who", "it", "again", "back",
        "recently", "earlier", "later", "from", "of", "lots", "lot", "many", "several", "few", "bunch", "couple", "other",
        "more", "new", "great", "good", "amazing", "interesting", "cool", "really", "very", "so", "just", "also", "there",
        "here", "him", "she", "he", "they", "we", "you", "i", "was", "is", "and", "but", "then", "after", "before"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PLACEHOLDERS = {"unknown", "n/a", "na", "none", "null", "tbd", "not stated", "not mentioned", "?", "-", "--"}


def has_word(low: str, words: list[str]) -> bool:
    """Whole-word match, so "api" doesn't match "rapidly" and "cto" doesn't match "director"."""
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) for w in words)


EMPTY_NOTE = {"founder": None, "company": None, "event": None, "sector": None, "stage": None, "one_liner": None,
              "metrics": {}, "background": [], "raising_usd": None, "next_step": None, "next_due": None, "step_done": False}

NOTE_SYSTEM = """You are the note-taking agent of a venture capital investor. They just recorded a note after an event or a meeting,
often dictated, so expect speech-to-text mistakes and filler words.

1. "note": rewrite it as a clear, professional investor note. Concise, well-punctuated, first person ("Met ...", "I'll ..."),
   short sentences or a few tight lines. Keep EVERY fact: every name, company, number, date, place and next step. Fix obvious
   speech-to-text errors and drop filler ("um", "like", "you know"). NEVER add facts, opinions, numbers or names that are
   not in the note. If the note is already clean, return it almost unchanged.
2. Extract: founder (full name of the founder they met, or null), company (startup name or null), event (where they met,
   e.g. "Deal Flow hackathon", or null), sector (one of: consumer, cpg, ai, devtools, fintech, climate, health, other, or
   null), stage (idea|pre-seed|seed|series-a|later|null), one_liner (<=15 words on what the company does, or null),
   metrics (object of numbers ONLY if stated: mrr_usd, stores, pilots, github_stars, users, paying_customers),
   background (list of short strings about the founder), raising_usd (number|null), next_step (the investor's own next
   action, short, or null), next_due (ISO date YYYY-MM-DD if a deadline is stated or implied like "by Monday", else null),
   step_done (true only if the note says their earlier next step with this person is now done, e.g. "sent Jonah the deck").
Today is {today} ({weekday}). Use null when not stated. NEVER invent names or numbers.
Return JSON with keys: note, founder, company, event, sector, stage, one_liner, metrics, background, raising_usd, next_step,
next_due, step_done."""

ROUTER_SYSTEM = """You are the personal AI agent of a venture capital investor, inside a chat app. Decide what their message
asks for and return JSON {{"intent": ..., "query": ..., "reply": ...}}.

intent is one of:
- "save_note": they are telling you about a meeting, a person, a company, an event, or anything to remember. This is the
  default for statements. query "", reply "".
- "conversations": who they have been talking to, who they met, their follow-ups, what is due or overdue.
- "rank": rank, score or prioritise the founders they have met.
- "source": find, source, discover or suggest new founders, startups or companies. query = the field, sector or kind of
  company they asked for (e.g. "climate tech", "AI agents for finance"), or "" if they did not name one.
- "lookup": look up or research one specific company. query = the company name.
- "who_knows": whether anyone on their team knows or has met a person or company. query = that name.
- "team": what their teammates are working on.
- "help": what you can do.
- "chat": any other question or request. reply = a short, helpful answer (2-4 sentences). Never invent facts about their
  notes, founders or team; point them to the right request instead.
Their focus: {focus}."""


# --------------------------------------------------------------------------- keyword extraction (no LLM key)
TITLE = r"(?:(?:Dr|Mr|Mrs|Ms|Mx|Prof)\.?\s+)?"
NAME_WORD = r"[A-Z][a-zà-ÿ]*(?:['’][A-Z]?[a-zà-ÿ]+|[A-Z][a-zà-ÿ]+)*"  # Maya, O'Brien, McDonald, DeShawn
NAME = rf"{TITLE}({NAME_WORD}(?:[ -]{NAME_WORD}){{0,2}})"
COMPANY = r"([A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9](?:\s[A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9])?)"
EVENT_WORDS = r"(?:hackathon|conference|summit|meetup|demo day|dinner|breakfast|lunch|happy hour|event|panel|mixer|party|offsite|fair|expo|retreat)"
VERBS = (r"(?:met with|met|meeting with|talked to|talked with|spoke to|spoke with|chatted with|caught up with|ran into|"
         r"sat down with|(?:had |grabbed )?(?:a )?(?:call|coffee|lunch|dinner|breakfast|drinks|chat|meeting|zoom) with)")
UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
MONEY = r"\$?\s?([\d][\d.,]*)\s*(k|thousand|mm|mn|m|million|bn|b|billion)?\b"
SENTENCE_END = re.compile(r"(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bMrs)(?<!\bSt)(?<!\bvs)(?<=[.!?])\s+|\n+")
MINE = re.compile(r"\b(i'll|i will|i need to|i should|i'm going to|i am going to|i want to|we'll|we will|we need to|"
                  r"let's|follow up|follow-up|remind me)\b", re.I)
DONE = re.compile(r"\b(sent|done|followed up|introduced|booked|scheduled|wrapped up|already|finished|completed)\b", re.I)
ACTION = re.compile(r"\b(send|intro|introduce|schedule|set up|loop in|book|get (?:him|her|them)|reach out|ping|email)\b", re.I)


def _num(s: str, unit: str | None = None) -> float:
    try:
        return float(s.replace(",", "")) * UNITS.get((unit or "").lower(), 1)
    except ValueError:
        return 0


def money_after(pattern: str, low: str) -> float | None:
    """The amount after a phrase like "raising": "$1.2M", "1.2 million", or the low end of "$2-3M"."""
    m = re.search(pattern + r"\s+(?:a |an |around |about |roughly |~)?" + MONEY + r"(?:\s*(?:-|–|to)\s*" + MONEY + ")?", low)
    if not m:
        return None
    unit = m.group(2) or m.group(4)  # "$2-3M": the unit after the range applies to both ends
    return _num(m.group(1), unit) or None


def due_from(text: str, today: dt.date) -> str | None:
    """A deadline in a next step. A stated day beats "today" (which usually says when they met)."""
    low = text.lower()
    m = re.search(r"\bin (\d{1,2}) days?\b", low)
    if m:
        return (today + dt.timedelta(days=int(m.group(1)))).isoformat()
    if "tomorrow" in low:
        return (today + dt.timedelta(days=1)).isoformat()
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b{d}\b", low):
            ahead = (i - today.weekday()) % 7 or 7
            return (today + dt.timedelta(days=ahead)).isoformat()
    if re.search(r"\bnext week\b", low):
        return (today + dt.timedelta(days=7 - today.weekday())).isoformat()
    if re.search(r"\bend of (the )?week\b", low):
        return (today + dt.timedelta(days=(4 - today.weekday()) % 7)).isoformat()
    if re.search(r"\b(today|tonight|eod|end of day)\b", low):
        return today.isoformat()
    return None


def next_step_of(text: str) -> str | None:
    """The investor's own next action: an explicit "Next step: ...", else a first-person or follow-up sentence, else an
    action sentence that isn't about what the founder will do. Long unpunctuated dictation is cut to the clause."""
    sentences = [x.strip() for x in SENTENCE_END.split(text) if x and x.strip()]
    for sent in sentences:
        m = re.search(r"\b(?:next steps?|next|to ?do|follow[- ]up)\s*(?::|\s[\-–])\s*(.+)", sent, flags=re.I)
        if m:
            return _clause(m.group(1))
    open_ = [x for x in sentences if not DONE.search(x)]  # "Sent Priya the intro, done" is not a next step
    mine = [x for x in open_ if MINE.search(x)]
    if mine:
        return _clause(mine[0], MINE)
    theirs = re.compile(r"^(he|she|they|the founder|founder)\b", re.I)
    acts = [x for x in open_ if ACTION.search(x) and not theirs.search(x)]
    return _clause(acts[0], ACTION) if acts else None


def _clause(sentence: str, start_at: re.Pattern | None = None) -> str:
    words = sentence.strip().rstrip(".!?").split()
    if len(words) > 22 and start_at is not None:
        m = start_at.search(sentence)
        words = sentence[m.start():].rstrip(".!?").split() if m else words
    return " ".join(words[:22])[:160]


def heuristic_note(text: str, today: dt.date) -> dict:
    """Keyword extraction for when no LLM key is set. Only fills fields it can read straight from the note."""
    ext = {**EMPTY_NOTE, "metrics": {}, "background": [], "step_done": False}
    low = text.lower()
    # The verb is case-insensitive ("Met", "met"); the name must be capitalized. "intro to X" is who to introduce, not who was met.
    m = re.search(rf"\b(?i:{VERBS})\s+(?i:the founder |founder )?{NAME}", text)
    if m and m.group(1).split()[0].lower() not in STOP:
        ext["founder"] = m.group(1)
    else:
        # Dictation is sometimes all lowercase: take the one or two words after the verb unless they are common words.
        m = re.search(rf"\b{VERBS}\s+([a-zà-ÿ'’]+)(?:\s+([a-zà-ÿ'’]+))?", low)
        if m and m.group(1) not in STOP:
            words = [m.group(1)] + ([m.group(2)] if m.group(2) and m.group(2) not in STOP else [])
            ext["founder"] = " ".join(w.capitalize() for w in words)
    m = re.search(rf"\b(?i:from|founder of|ceo of|cto of|co-founder of|cofounder of|startup called|company called)\s+{COMPANY}", text)
    if m and m.group(1).split()[0] not in ("The", "A", "An", "I", "We"):
        ext["company"] = m.group(1)
    elif ext["founder"]:
        # "met maya okafor from quillstack": the word after "from"/"of" or in brackets after the name.
        m = re.search(rf"{re.escape(ext['founder'].lower())}\s*(?:\(|\bfrom\b|\bof\b)\s*([a-z0-9][a-z0-9&.\-]+)", low)
        if m and m.group(1) not in STOP:
            ext["company"] = m.group(1).strip(".-").capitalize()
    m = re.search(rf"\bat (?:the |a |an )?((?:[A-Z][\w'&-]*\s){{0,4}}(?i:{EVENT_WORDS}))\b", text)
    if m:
        ext["event"] = m.group(1).strip()
    for sector, words in SECTOR_KEYWORDS.items():
        if has_word(low, words):
            ext["sector"] = sector
            break
    if re.search(r"\bpre[- ]?seed\b", low):
        ext["stage"] = "pre-seed"
    elif re.search(r"\bseries a\b", low):
        ext["stage"] = "series-a"
    elif re.search(r"\bseed\b", low):
        ext["stage"] = "seed"
    elif re.search(r"\bidea stage\b|\bpre-product\b", low):
        ext["stage"] = "idea"
    m = re.search(MONEY + r"\s*(?:mrr|/mo\b|a month|per month|monthly)", low)
    if m:
        ext["metrics"]["mrr_usd"] = _num(m.group(1), m.group(2) or "")
    m = re.search(r"([\d][\d.,]*)\s*(k)?\s*(?:github )?stars\b", low)
    if m:
        ext["metrics"]["github_stars"] = int(_num(m.group(1), m.group(2)))
    m = re.search(r"([\d][\d,]*)\s*(?:paying )?customers\b", low)
    if m:
        ext["metrics"]["paying_customers"] = int(_num(m.group(1)))
    ext["raising_usd"] = money_after(r"\braising", low)
    ext["background"] = [k for k in TECH_KEYWORDS if has_word(low, [k])][:3]
    ext["next_step"] = next_step_of(text)
    ext["next_due"] = due_from(ext["next_step"], today) if ext["next_step"] else None
    if not ext["next_step"]:
        ext["step_done"] = bool(DONE.search(text))
    return ext


def route_rules(text: str) -> dict:
    """What a message asks for, by phrase matching (used when no LLM key is set). Statements are notes."""
    t = " ".join(text.strip().split()).rstrip("?!. ")
    if not (text.strip().endswith("?") or len(t.split()) <= 12):
        return {"intent": "save_note", "query": ""}  # longer statements are notes, even if they mention "follow up"
    lead = r"^(?:hey |hi |ok |okay )?(?:can you |could you |would you |please |pls |i want you to |i'd like you to |help me )?"

    def match(pattern: str):
        return re.match(lead + pattern, t, flags=re.I)

    if m := match(r"(?:ask (?:the |my )?team (?:about |if anyone knows )?|who (?:else )?knows |does anyone know |"
                  r"has anyone (?:else )?met )(.+)$"):
        return {"intent": "who_knows", "query": m.group(1)}
    if (m := match(r"(?:look ?up|search(?: akta(?:\.pro)?)?(?: for)?|research|check out|tell me about)\s+(.+?)(?: on akta(?:\.pro)?)?$")) \
            and not re.search(r"\b(founders?|startups?|companies)\b", m.group(1), flags=re.I):
        return {"intent": "lookup", "query": m.group(1)}
    if m := match(r"(?:source|find|look for|search for|discover|show me|get me|suggest|recommend|scout)\b"
                  r"(?:\s+(?:me|some|new|more|a few|good|great|promising|early[- ]stage|interesting))*"
                  r"\s+(?:founders?|startups?|companies|teams)\b"
                  r"(?:\s+(?:in|for|doing|working on|building|around|focused on|within|on)\s+(.+))?$"):
        return {"intent": "source", "query": m.group(1) or ""}
    q = t.lower()
    if match(r"(?:rank|score|prioriti[sz]e) (?:my |the )?(?:founders|startups|companies|pipeline)$|rank$") or \
            re.search(r"^(?:show )?(?:me )?my (?:best )?founders$|^(?:who are )?my best founders$|^who should i (meet|back|call)", q):
        return {"intent": "rank", "query": ""}
    if re.search(r"^who (have|am) i (been )?talk|^who did i meet|^(show )?(me )?my conversations$|^what('s| is) next$|"
                 r"^(my |show )?(me )?(my )?follow[- ]?ups?$|^what do i owe|^(what|who)('s| is) (overdue|due)", q):
        return {"intent": "conversations", "query": ""}
    if re.search(r"^what('s| is) my team(?: working on| doing| up to)?$|^what are my teammates (working on|doing)", q):
        return {"intent": "team", "query": ""}
    if re.match(r"^(help|what can you do)$", q):
        return {"intent": "help", "query": ""}
    if text.strip().endswith("?"):
        return {"intent": "help", "query": ""}  # an unanswered question is never saved into the shared notes
    return {"intent": "save_note", "query": ""}


def tidy(text: str) -> str:
    """Minimal clean-up of a note when no LLM is available: spacing, capital first letter, closing punctuation."""
    t = re.sub(r"\s+([,.!?;:])", r"\1", " ".join(text.split()))
    t = re.sub(r"\b(um+|uh+|erm)\b[,]?\s*", "", t, flags=re.I).strip()
    if t:
        t = t[0].upper() + t[1:]
        if t[-1] not in ".!?":
            t += "."
    return t


def clean_note(ext: dict | None) -> dict:
    """Coerce an extraction (from the LLM or the keyword rules) to the stored shape."""
    ext = ext or {}
    out = {**EMPTY_NOTE}
    for k in ("founder", "company", "event", "sector", "stage", "one_liner", "next_step"):
        v = ext.get(k)
        v = str(v).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""
        out[k] = v[:160] if v and v.lower() not in PLACEHOLDERS else None
    if out["sector"]:
        out["sector"] = out["sector"].lower()
    if out["stage"]:
        out["stage"] = out["stage"].lower().replace(" ", "-")
    m = ext.get("metrics") if isinstance(ext.get("metrics"), dict) else {}
    out["metrics"] = {k: float(v) for k, v in m.items()
                      if k in ("mrr_usd", "stores", "pilots", "github_stars", "users", "paying_customers") and isinstance(v, (int, float)) and v}
    bg = ext.get("background") if isinstance(ext.get("background"), list) else []
    out["background"] = [b.strip()[:80] for b in bg if isinstance(b, str) and b.strip() and b.strip().lower() not in PLACEHOLDERS][:5]
    r = ext.get("raising_usd")
    out["raising_usd"] = float(r) if isinstance(r, (int, float)) and r > 0 else None
    d = ext.get("next_due")
    try:
        out["next_due"] = dt.date.fromisoformat(str(d)[:10]).isoformat() if d else None
    except ValueError:
        out["next_due"] = None
    out["step_done"] = ext.get("step_done") is True
    return out


# --------------------------------------------------------------------------- scoring
def features(ext: dict, member: dict) -> dict:
    sector = (ext.get("sector") or "").lower()
    stage = (ext.get("stage") or "").lower()
    # An unknown or "other" sector is not "outside the thesis": the note just didn't say. Score it halfway.
    thesis = 0.5 if sector in ("", "other") else 1.0 if sector in member["sectors"] else 0.0
    if stage and stage not in member["stages"]:
        thesis *= 0.5
    m = ext.get("metrics") or {}
    mrr = m.get("mrr_usd") or 0
    if mrr >= 30000:
        traction = 1.0
    elif mrr >= 10000 or (m.get("stores") or 0) >= 50 or (m.get("github_stars") or 0) >= 1000 \
            or (m.get("pilots") or 0) >= 5 or (m.get("users") or 0) >= 5000:
        traction = 0.7
    elif any(v for v in m.values() if v):
        traction = 0.4
    else:
        traction = 0.1
    bg = " ".join(ext.get("background") or []).lower()
    fit = 1.0 if has_word(bg, FIT_KEYWORDS.get(sector, [])) else (0.6 if bg else 0.3)
    mutual = 1.0 if ext.get("warm_intro") else 0.0
    raising = ext.get("raising_usd") or 0
    if mrr and raising:
        ratio = mrr * 12 / raising
        eff = 1.0 if ratio >= 0.3 else 0.6 if ratio >= 0.15 else 0.3
    else:
        eff = 0.2
    tech = 1.0 if has_word(bg, TECH_KEYWORDS) or (m.get("github_stars") or 0) > 0 else (0.4 if bg else 0.2)
    return {"thesis": thesis, "traction": traction, "founder_fit": fit, "mutual": mutual,
            "capital_efficiency": eff, "technical_depth": tech}


def score_member(ext: dict, member: dict) -> dict:
    f = features(ext, member)
    parts = {k: round(w * f[k], 1) for k, w in member["weights"].items()}
    return {"score": round(sum(parts.values())), "parts": parts, "features": f}


def money(x):
    if not x:
        return None
    return f"${x / 1_000_000:.1f}M" if x >= 1_000_000 else f"${x / 1000:.0f}k"


def metrics_line(m: dict) -> str:
    labels = {"mrr_usd": lambda v: f"{money(v)}/month revenue", "stores": lambda v: f"{v:g} stores",
              "pilots": lambda v: f"{v:g} pilots or design partners", "github_stars": lambda v: f"{v:,.0f} GitHub stars",
              "paying_customers": lambda v: f"{v:g} paying customers", "users": lambda v: f"{v:,.0f} users"}
    parts = [labels[k](v) for k, v in (m or {}).items() if v and k in labels]
    return ", ".join(parts) if parts else "Not stated"
