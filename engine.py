"""Last Touch rules: read founder details out of recorded event notes and score them with a person's criteria.

Scoring and follow-up status are plain code so every decision is explainable. The LLM (Nicrron) writes up notes,
extracts their fields and decides what each message asks for; without a key, the keyword rules below do it.
"""
from __future__ import annotations

import datetime as dt
import math
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
    "consumer": ["consumer", "marketplace", "app for", "d2c", "dtc", "restaurant", "shoppers"],
    "cpg": ["cpg", "beverage", "beverages", "snack", "snacks", "grocery", "retail", "stores"],
    "climate": ["climate", "carbon", "clean energy", "renewable energy", "energy storage", "energy grid", "solar", "battery",
                "batteries", "decarbonization", "climate tech"],
    "health": ["health", "healthcare", "clinic", "clinics", "patient", "patients", "medical", "biotech"],
}
TECH_KEYWORDS = ["engineer", "phd", "machine learning", "open-source", "open source", "research", "cto"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
STOP = {"tons", "plenty", "dozens", "loads", "another", "numerous", "various", "quite", "maybe", "like", "all", "both",
        "each", "one", "nobody", "somebody", "anybody", "anyone", "last", "next", "week", "month", "morning", "evening",
        "night", "afternoon", "folks", "guys", "investor", "vc", "vcs", "partner", "partners", "the", "a", "an", "my", "our", "his", "her", "their", "with", "some", "someone", "team", "him", "them", "us",
        "investors", "founders", "founder", "two", "three", "four", "this", "that", "these", "those", "people", "everyone",
        "up", "today", "yesterday", "tonight", "and", "to", "for", "at", "in", "on", "about", "who", "it", "again", "back",
        "recently", "earlier", "later", "from", "of", "lots", "lot", "many", "several", "few", "bunch", "couple", "other",
        "more", "new", "great", "good", "amazing", "interesting", "cool", "really", "very", "so", "just", "also", "there",
        "here", "him", "she", "he", "they", "we", "you", "i", "was", "is", "and", "but", "then", "after", "before",
        "hundreds", "thousands", "heaps", "tonnes", "everybody", "handful", "dozen", "number", "five", "six", "seven",
        "eight", "nine", "ten", "twenty", "briefly", "online", "virtually", "finally", "quickly", "formally", "legal",
        "counsel", "lawyers", "accounting", "sales", "marketing", "product", "engineering", "design", "ops", "hr",
        "recruiting", "me", "your", "customers", "users", "references", "advisors", "mentors", "recruiters",
        "candidates", "investors", "lps", "partners", "colleagues", "portfolio", "folks", "people", "experts", "bankers"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME_WORDS = {"today", "yesterday", "tonight", "last", "this", "earlier", "recently", "again", "later", "next"} | set(WEEKDAYS)


def _word_re(w: str) -> str:
    # Whole words, with simple endings: "api" doesn't match "rapidly" or "capital", "cto" doesn't match "director",
    # but "engineers", "researcher", "consumers" and "banking" still match.
    return rf"(?<![a-z0-9]){re.escape(w)}(?:s|es|er|ers|ing)?(?![a-z0-9])"


def has_word(low: str, words: list[str]) -> bool:
    return any(re.search(_word_re(w), low) for w in words)


def word_hits(low: str, words: list[str]) -> int:
    return sum(len(re.findall(_word_re(w), low)) for w in words)


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
CAP = r"[A-ZÀ-ÖØ-ÞŁŚŻŹĆŃŠČŽ]"
LOWER = r"[a-zß-öø-ÿłśżźćńščž]"
NAME_WORD = rf"(?:[A-Z]{{2}}(?![A-Za-z])|{CAP}(?!{CAP}){LOWER}*(?:['’]{CAP}?{LOWER}+|{CAP}{LOWER}+)*)"  # JD, Maya, O'Brien, McDonald, José
PARTICLE = r"(?:de|la|del|da|di|van|von|der|le|du|bin|al)"
NAME = rf"{TITLE}({NAME_WORD}(?:[ -](?:{PARTICLE} )*{NAME_WORD}){{0,2}})"
DESCRIPTOR = r"(?:(?:the |a |an )?(?:[A-Z]{2,}|[A-Z][a-z]+) (?:founder|cofounder|co-founder|alum|alumna|alumnus|ceo|cto|exec|engineer) )"
COMPANY = r"([A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9](?:\s[A-Z][A-Za-z0-9&.\-]*[A-Za-z0-9])?)"
VERBS = (r"(?:met up with|catching up with|caught up with|met with|met|meeting with|talked to|talked with|spoke to|spoke with|chatted with|caught up with|ran into|"
         r"sat down with|(?:had |grabbed )?(?:a )?(?:call|coffee|lunch|dinner|breakfast|drinks|chat|meeting|zoom) with)")
UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
MONEY = r"\$?\s?(?<![\d.,])([\d][\d.,]{0,15})\s*(k|thousand|mm|mn|m|million|bn|b|billion)?\b"  # bounded: no backtracking blowup
SENTENCE_END = re.compile(r"(?<=[.!?])(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bMx\.)(?<!\bProf\.)(?<!\bSt\.)"
                          r"(?<!\bvs\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\b[A-Z]\.)(?<!\bInc\.)(?<!\bCorp\.)(?<!\bLtd\.)"
                          r"(?<!\bJr\.)(?<!\bSr\.)(?<!\b[ap]\.m\.)(?<!\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.)"
                          r"(?<!\bSept\.)\s+|\n+")
MINE = re.compile(r"\b(i'll|i will|i need to|i should|i'm going to|i am going to|i want to|we'll|we will|we need to|"
                  r"let's|follow up|follow-up|remind me|check in|check back|circle back|touch base|reconnect)\b", re.I)
DONE = re.compile(r"\b(sent|done|followed up|introduced|booked|scheduled|wrapped up|finished|completed)\b", re.I)
# The investor's own finished step: "Sent Maya the deck", "I introduced her to Cara", "Done:" (not "She booked $25k MRR").
MY_DONE = re.compile(r"^(?:i(?:'ve| have)?\s+|we(?:'ve| have)?\s+|just\s+|already\s+)?(?:sent|followed up|introduced|booked|"
                     r"scheduled|finished|completed|intro'd|emailed|pinged)\b|^done\b|^(?:that's|it's|that is|it is|all)\s+done\b|"
                     r"(?:^|[,;:]\s*)(?:all\s+)?done[.!]?$|(?:^|[,;:]\s*)(?:(?:also|and|so|btw|fyi|update)[,:]?\s+)?(?:the |my |our )?"
                     r"(?:(?:ic|investment|partner|reference|diligence|intro|follow[- ]?up|thank[- ]you)\s+)?"
                     r"(?:intro(?:duction)?|follow[- ]?up|memo|email|term sheet|call)(?:\s+(?:email|call|memo))?(?:\s+(?:to|with|for)(?:\s+[\w'’]+){1,3})?"
                     r"\s+(?:is|was)\s+(?:now\s+|all\s+)?done(?:\s+(?:now|already|too|today|yesterday|this (?:morning|afternoon|week)|last (?:night|week)))?"
                     r"(?=\s*(?:[.!,;:]|$|\b(?:and|so|but)\b))", re.I)
ACTION = re.compile(r"\b(send|intro(?!['’]d)|introduce|schedule|set up|loop in|book|get (?:him|her|them)|reach out|ping|email)\b", re.I)


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


def next_step_of(text: str, founder: str | None = None) -> str | None:
    """The investor's own next action: an explicit "Next step: ...", else a first-person or follow-up sentence, else an
    action sentence (unless it's the founder's own plan, like "She will send the data room"). Long unpunctuated
    dictation is cut to the clause."""
    sentences = [x.strip() for x in SENTENCE_END.split(text) if x and x.strip()]
    for sent in sentences:
        m = re.search(r"\b(?:next steps?|next|to ?do|follow[- ]up)\s*(?::|\s[\-–])\s*(.+)", sent, flags=re.I)
        if m:
            return _clause(m.group(1))
    who = r"he|she|they|her team|his team|their team|the founder|the team"
    if founder:
        who += "|" + re.escape(founder.split()[0])
    plan = (r"(?:will|'ll|is going to|are going to|plans? to|promised to|wants? to|(?:is|are|'s|'re) (?:happy|keen|willing|glad|able) to)"
            r"\s+(?:(?:also|probably|hopefully|definitely|then|likely)\s+)?")
    # The founder's own plan: "She will send us the data room", "Maya is going to intro us" (not "She has a lead, so...").
    theirs = re.compile(rf"(?:^|\b)(?:{who})(?:\s+|(?='))(?:'s going to\s+|'re going to\s+|{plan})$", re.I)

    def open_from(sentence: str, rx: re.Pattern) -> str | None:
        plain, last = sentence.replace("’", "'"), None  # phones type "she’ll"
        for m in rx.finditer(sentence):
            if rx is ACTION and (theirs.search(plain[:m.start()]) or
                                 last is not None and re.fullmatch(r"[^,;:]*\band\s+", plain[last:m.start()])):
                last = m.start()
                continue  # "he will send us the data room (and intro us to his lead)"
            if DONE.search(sentence[m.start():]):
                return None  # "Sent Priya the intro, done" is not a next step
            return _clause(sentence, m.start())
        return None

    for rx in (MINE, ACTION):
        for sent in sentences:
            step = open_from(sent, rx)
            if step:
                return step
    return None


def _clause(sentence: str, start: int | None = None) -> str:
    """The step itself, from where it starts when the sentence is long or opens with the meeting ("just met sam today
    ... send him the deck friday" -> "send him the deck friday")."""
    words = sentence.strip().rstrip(".!?").split()
    if start is not None:
        v = re.search(rf"\b(?i:{VERBS})\b", sentence)
        if len(words) > 22 or (v and v.start() < start):
            words = sentence[start:].rstrip(".!?").split()
    return " ".join(words[:22])[:160]


_STOPS = "|".join(sorted(map(re.escape, STOP | set(WEEKDAYS) | set(MONTHS)), key=len, reverse=True))
_WHEN = "|".join(WEEKDAYS + ["today", "yesterday", "earlier", "quickly", "briefly", "virtually", "online"])


def _date_word(cand: str, after: str) -> bool:
    """"Monday" or "May" alone, not followed by "from"/"of"/"(", is a day; "April Chen" or "May from Kite" is a name."""
    first = cand.split()[0].lower()
    return first in STOP or (first in set(WEEKDAYS) | set(MONTHS) and len(cand.split()) == 1
                             and not re.match(r"\s*(?:from\b|of\b|\()", after))


def _founder_in(text: str, low: str) -> str | None:
    """Who they met. Capitalized names after a meeting verb (skipping "YC founder", titles and day words), else, for
    all-lowercase dictation, one or two words after the verb."""
    for m in re.finditer(rf"\b(?i:{VERBS})\s+(?i:the founder |founder )?{DESCRIPTOR}?{NAME}", text):
        cand = m.group(1)
        if not _date_word(cand, text[m.end():]):
            return cand
        w = re.match(rf"\s*(?:\w+\s+){{0,3}}?(?i:with)\s+{DESCRIPTOR}?{NAME}", text[m.end():])  # "Met Monday with Maya Okafor"
        if w and not _date_word(w.group(1), text[m.end() + w.end():]):
            return w.group(1)
    word = rf"(?!(?:{_STOPS})\b)([a-zà-ÿ'’]+)"  # a word that isn't a common, number or time word
    m = re.search(rf"\b{VERBS}\s+(?:(?:on |last |this )?(?:{_WHEN})(?: morning| afternoon| evening| night)? with )?"
                  rf"(?:the founder |founder |(?:dr|mr|mrs|ms|prof)\.? )?{word}"
                  # two words, then punctuation or any common word: "met raj patel today", "maya okafor she is..."
                  rf"(?:\s+{word}(?=\s*(?:[(,.;:!?]|$|\b(?:{_STOPS}|over|re)\b))"
                  # one word only before "from", "at", a comma, the end, or when they met: "met maya today"
                  rf"|(?=\s*(?:[(,.;]|$|\b(?:from|at|who|of|and|he|she|they|is|was|about|{'|'.join(sorted(TIME_WORDS))})\b)))", low)
    return " ".join(x.capitalize() for x in m.groups() if x) if m else None


EVENT_CORE = r"(?:hackathon|conference|summit|meetup|demo day|dinner|breakfast|lunch|happy hour|event|panel|mixer|party|offsite|expo|retreat)"
NOT_PAST = r"(?!-)(?!\s+(?i:next|tomorrow|tonight|sometime|soon|this|later|on \w+day))"


PAST = re.compile(r"\b(?:met|caught up|talked|spoke|chatted|ran into|had (?:a )?(?:coffee|lunch|dinner|drinks|chat|call))\b", re.I)
SOON = r"(?!-)(?!\s+(?i:next|tomorrow|sometime|soon)\b)"  # after "met", "on Saturday" or "this morning" is when they met


def _event_in(text: str, low: str) -> str | None:
    """Where they met: "at the Deal Flow hackathon", "at Climate Week", "at the founders dinner" (not "at a fair price",
    "at lunch next week" or "at some point")."""
    mods = 3 if text == low else 2  # lowercase words before the event word ("deal flow hackathon")
    mod = rf"(?:(?!(?:the|a|an|some|this|that|my|our|his|her|their|point|stage)\s)[a-z][\w'&-]*\s){{0,{mods}}}"
    for m in re.finditer(r"\b(?i:at) (?:the |a |an )?", text):
        start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        past = [p for p in PAST.finditer(text, start, m.start())]
        # "Met Maya at the summit on Saturday" (when they met), not "met Raj, see him at the dinner on Thursday"
        later = past and re.search(r"'ll\b|\b(?:will|going to|gonna)\s+(?:\w+\s+)?(?:see|meet|catch|be)\b|\b(?:we're|we are|i'm|i am)\s+(?:seeing|meeting)\b|"
                                   r"\b(?:see|seeing|meet|meeting|catch|catching)\s+(?:him|her|them|you|up|again)\b|\blet['’]?s\b|"
                                   r"(?:[,;]|\band\b)\s*(?:he|she|they|we|i)\b|\b(?:can|could|should|might|may|would|agreed to|plan(?:ning)? to|hope to)\b",
                                   text[past[-1].end():m.start()], re.I)
        tail = SOON if past and not later else NOT_PAST
        e = re.match(rf"((?:[A-Z0-9][\w'&-]*\s){{0,4}}{mod}(?i:{EVENT_CORE})|(?:[A-Z0-9][\w'&-]*\s){{1,4}}(?i:week|fair)|"
                     rf"(?i:career|job|trade|science|startup) fair)\b{tail}", text[m.end():])
        if e:
            return e.group(1).strip()
    return None


def heuristic_note(text: str, today: dt.date) -> dict:
    """Keyword extraction for when no LLM key is set. Only fills fields it can read straight from the note."""
    ext = {**EMPTY_NOTE, "metrics": {}, "background": [], "step_done": False}
    low = text.lower()
    ext["founder"] = _founder_in(text, low)
    m = re.search(rf"\b(?i:from|founder of|ceo of|cto of|co-founder of|cofounder of|startup called|company called)\s+{COMPANY}", text)
    if m and m.group(1).split()[0] not in ("The", "A", "An", "I", "We"):
        ext["company"] = m.group(1)
    elif ext["founder"]:
        # "met maya okafor from quillstack": the word after "from"/"of" or in brackets after the name.
        m = re.search(rf"{re.escape(ext['founder'].lower())}\s*(?:\(|\bfrom\b|\bof\b)\s*([a-z0-9][a-z0-9&.\-]+)", low)
        if m and m.group(1) not in STOP:
            ext["company"] = m.group(1).strip(".-").capitalize()
    ext["event"] = _event_in(text, low)
    hits = {sector: word_hits(low, words) for sector, words in SECTOR_KEYWORDS.items()}
    best = max(hits, key=lambda k: hits[k])
    ext["sector"] = best if hits[best] else None
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
    m = re.search(r"(?<![\d.,])([\d][\d.,]{0,15})\s*(k)?\s*(?:github )?stars\b", low)
    if m:
        ext["metrics"]["github_stars"] = int(_num(m.group(1), m.group(2)))
    m = re.search(r"(?<![\d,])([\d][\d,]{0,15})\s*(?:paying )?customers\b", low)
    if m:
        ext["metrics"]["paying_customers"] = int(_num(m.group(1)))
    ext["raising_usd"] = money_after(r"\braising", low)
    ext["background"] = [k for k in TECH_KEYWORDS if has_word(low, [k])][:3]
    ext["next_step"] = next_step_of(text, ext["founder"])
    ext["next_due"] = due_from(ext["next_step"], today) if ext["next_step"] else None
    if ext["next_step"] and not ext["next_due"]:
        sents = [x.strip() for x in SENTENCE_END.split(text) if x and x.strip()]
        at = next((i for i, x in enumerate(sents) if ext["next_step"][:40] in x), None)
        if at is not None and at + 1 < len(sents) and re.match(
                r"(?:ideally |hopefully |preferably )?(?:due (?:by |on |before )?|by |before |on |no later than )(?:(?:this|next|the) )?(?:eod |cob )?"
                r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|eod|cob|end of (?:the |this |next )?(?:day|week|month)|next week)"
                r"(?:['’]s (?:partner |ic |board |team )?(?:meeting|call|sync))?"
                r"(?:,? (?:morning|afternoon|evening|night|noon|at noon|at the latest|latest|please|ideally|"
                r"at \d{1,2}(?::\d\d)?(?: ?[ap]\.?m\.?)?|\d{1,2}(?::\d\d)? ?[ap]\.?m\.?)){0,2}[.!]?$", sents[at + 1], re.I):
            ext["next_due"] = due_from(sents[at + 1], today)
    if not ext["next_step"]:
        ext["step_done"] = any(MY_DONE.search(x.strip().replace("’", "'")) for x in SENTENCE_END.split(text) if x and x.strip())
    return ext


AUX = r"(?:did|do|does|am|is|are|was|were|can|could|should|would|will|have|has)"
# Words that start a shorthand note right after the auxiliary: "Will send...", "Have a call...", "Is raising...",
# "Has three pilots", "Did lunch with...", "Can meet...", "Would love to...", "Could be a fit", "Was great meeting..."
NOTE_AFTER_AUX = (r"(?:to|a|an|been|got|just|already|also|still|really|very|so|super|quite|pretty|not|definitely|probably|"
                  r"great|good|nice|fun|amazing|awesome|impressive|impressed|excited|keen|happy|glad|interested|able|at|in|on|"
                  r"\d[\w$.,%]*|\$\S*|one|two|three|four|five|six|seven|eight|nine|ten|several|some|lots|plenty|"
                  r"send|follow|intro|introduce|email|call|ping|meet|book|schedule|set|share|check|loop|review|circle|reach|"
                  r"get|do|be|have|need|try|look|chat|catch|connect|draft|write|pass|think|keep|make|take|love|like|back|"
                  r"lunch|coffee|dinner|breakfast|drinks|intros|diligence|dd|calls|customers|revenue|traction|users|technical|profitable|"
                  r"seed|pre-\w+|ex-\w+|\w+ing|\w{3,}ed)\b")
SUBJECT = (r"(?:i|we|you|anyone|anybody|someone|somebody|anything|they|he|she|it|there|the|my|our|this|that|these|those|"
           r"any|his|her|their)\b")


def is_question(t: str) -> bool:
    """A short message without "?" that reads as a question. Commas, colons and semicolons mark shorthand notes
    ("Did coffee with Raj, wants intro to Cara", "What Jonah said: raising in Q1")."""
    q = t.lower()
    if c := re.search(r"[,:;]", t):
        # "Did Maya reply, or should I ping her", "Is Patchbay raising, and how much" are still questions
        return bool(re.match(r"\s*(?:or\b|and (?:how|what|when|where|which|who|why|if)\b)", t[c.end():], re.I)) and is_question(t[:c.start()])
    if m := re.match(rf"^({AUX}) {SUBJECT}", q):
        if re.match(r"the\b" if m.group(1) == "did" else r"(?:the|my|our|this|that|these|those|his|her|their|any)\b", q[m.end(1) + 1:]) \
                and m.group(1) in ("have", "has", "did"):
            # "have my founders replied", "did the patchbay intro go out"; not "Have the term sheet from Raj", "Did the intro to Cara"
            return bool(re.search(r"\b(?:\w{3,}ed|been|gone|come|done|met|sent|heard|seen|got|gotten|go|reply|respond|send|"
                                  r"close|sign|happen|land|ship|launch|get|work|show|turn|raise|say|mention|hear)\b", q[m.end():]))
        return True  # "is the deck in", "is there anything due this week", "do my notes mention stripe"
    w = t.split()
    if len(w) > 1 and re.fullmatch(AUX, w[0].lower()) and w[0].lower() != "will" and (
            w[1][:1].isupper() or w[0].lower() in ("did", "does") and re.fullmatch(r"(?!(?:b?ring|ping)$)(?:\w+ing|\w{3,}ed)", w[1].lower())):
        return True  # a name that ends like "raising" or "impressed": "Did Ahmed reply", "Is Ming raising", "did jared follow up"
    if m := re.match(rf"^{AUX} (?!{NOTE_AFTER_AUX})(\w[\w'.&-]*)", q):
        # "is patchbay raising", "has jonah replied", "did jonah reply"; "will" needs a verb after the name, since
        # "Will Smith from Kite is raising" is a note
        return not q.startswith("will ") or bool(re.match(r"^will \S+ (?:\S+ )?(?:send|reply|respond|intro|introduce|email|"
                                                          r"call|meet|share|follow|get|be|raise|close|take|sign|come|join|"
                                                          r"lead|invest|do|make|have|need|want)\b", q))
    if re.match(r"^(?:what|which|where|when|why|how)\b(?! an? \b| (?:i|we|he|she|they)\b)", q):
        # "what about quillstack", "what stage is quillstack at", "how long ago did i meet maya";
        # not "What a pitch", "When I met Raj he was already raising", "What I liked about Patchbay is the team"
        return True
    return bool(re.match(r"^who\b(?! i met| we met)", q))


def route_rules(text: str) -> dict:
    """What a message asks for, by phrase matching (used when no LLM key is set). Statements are notes."""
    t = " ".join(text.strip().split()).rstrip("?!. ")
    t = re.sub(r"(?i)\b(what|who|how|where|when)(?:s|’s)\b", r"\1's", t)  # "whats next", "who’s raising" (phones)
    if not (text.strip().endswith("?") or len(t.split()) <= 12):
        return {"intent": "save_note", "query": ""}  # longer statements are notes, even if they mention "follow up"
    lead = r"^(?:hey |hi |ok |okay )?(?:can you |could you |would you |please |pls |i want you to |i'd like you to |help me )?"

    def match(pattern: str):
        return re.match(lead + pattern, t, flags=re.I)

    if m := match(r"(?:ask (?:the |my )?team (?:about |if anyone knows )?|who (?:else )?knows |does anyone know |"
                  r"(?:has |does )?(?:anyone|anybody|any one) (?:else )?(?:know|knows|met) )(.+)$"):
        return {"intent": "who_knows", "query": m.group(1)}
    if (m := match(r"(?:look ?up|search(?: akta(?:\.pro)?)?(?: for)?|research|check out|tell me about)\s+(.+?)(?: on akta(?:\.pro)?)?$")) \
            and not re.search(r"\b(founders?|startups?|companies)\b", m.group(1), flags=re.I):
        return {"intent": "lookup", "query": m.group(1)}
    q = t.lower()
    if match(r"(?:rank|score|prioriti[sz]e) (?:my |the )?(?:founders|startups|companies|pipeline)$|rank$|(?:rank|prioriti[sz]e|sort) (?:(?:my|our|the|these|those|all)(?: [\w-]+){0,3} (?:founders|startups|companies|pipeline|deals)|them|these|those)$") or \
            re.search(r"^(?:show )?(?:me )?my (?:best )?founders$|^(?:who are )?my best founders$|^who should i (meet|back|call)", q):
        return {"intent": "rank", "query": ""}
    if re.search(r"^(?:show|find|list|get|see)(?: me)? (?:all )?(?:my|our|the) (?:founders|startups|companies|pipeline|people|notes)$|"
                 r"^(?:show|find|list|get|see)(?: me)? (?:all )?(?:my|our|the) (?:founders|startups|companies|people) (?:i|we) (?:met|talked to|spoke (?:to|with))\b", q):
        return {"intent": "conversations", "query": ""}
    # "Show me my founders" is the user's own founders, never a search for new ones.
    if (m := match(r"(?:source|find|look for|search for|discover|show me|get me|suggest|recommend|scout)\b"
                  r"(?:\s+(?:me|some|new|more|a few|good|great|promising|early[- ]stage|interesting))*"
                  r"(?:\s+(.+?))?\s+(?:founders?|startups?|companies|teams)\b"
                  r"(?:\s+(?:in|for|doing|working on|building|around|focused on|within|on)\s+(.+))?$")) \
            and (m.group(1) or "").lower() not in ("the", "these", "those"):
        if re.match(r"(?:my|our)\b", m.group(1) or "", flags=re.I):
            return {"intent": "rank", "query": ""}  # "show me my climate founders": the user's own
        return {"intent": "source", "query": " ".join(x for x in (m.group(1), m.group(2)) if x)}
    if re.search(r"^who (have|am) i (been )?talk|^who did i meet|^(show )?(me )?my conversations$|^what('s| is) next$|"
                 r"^(my |show )?(me )?(my )?follow[- ]?ups?$|^what do i owe|^(what|who)('s| is) (overdue|due)", q):
        return {"intent": "conversations", "query": ""}
    if re.search(r"^what('s| is) my team(?: working on| doing| up to)?$|^what are my teammates (working on|doing)", q):
        return {"intent": "team", "query": ""}
    if re.match(r"^(help|what can you do)$", q):
        return {"intent": "help", "query": ""}
    # Dictated questions often have no "?": "did jonah reply", "is patchbay raising", "which founders did i meet at yc".
    if text.strip().endswith("?") or is_question(t):
        return {"intent": "help", "query": "", "question": True}  # an unanswered question is never saved into the shared notes
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


def placeholder(v: str) -> bool:
    """'Unknown', 'N/A.', 'Not specified', 'Unnamed startup'... are not values."""
    key = re.sub(r"[^a-z/ ]+", "", v.lower()).strip()
    return not key or bool(re.match(r"^(unknown|unnamed|unspecified|anonymous|n/?a|na|none|null|nil|tbd|tba|"
                                     r"not (stated|specified|mentioned|provided|given|known|available|sure)|no (name|company))\b", key))


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def clean_note(ext: dict | None) -> dict:
    """Coerce an extraction (from the LLM or the keyword rules) to the stored shape."""
    ext = ext or {}
    out = {**EMPTY_NOTE}
    for k in ("founder", "company", "event", "sector", "stage", "one_liner", "next_step"):
        v = ext.get(k)
        if isinstance(v, list):  # several founders: keep the first, the rest go into background
            names = [x.strip() for x in v if isinstance(x, str) and x.strip()]
            v = names[0] if names else ""
            if k == "founder" and len(names) > 1:
                ext.setdefault("background", [])
                if isinstance(ext["background"], list):
                    ext["background"] = [f"also met {n}" for n in names[1:]] + ext["background"]
        v = str(v).strip() if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
        out[k] = v[:160] if v and not placeholder(v) else None
    if out["sector"]:
        out["sector"] = out["sector"].lower()
    if out["stage"]:
        out["stage"] = out["stage"].lower().replace(" ", "-")
    m = ext.get("metrics") if isinstance(ext.get("metrics"), dict) else {}
    out["metrics"] = {k: float(v) for k, v in m.items()
                      if k in ("mrr_usd", "stores", "pilots", "github_stars", "users", "paying_customers") and _number(v)}
    bg = ext.get("background") if isinstance(ext.get("background"), list) else []
    out["background"] = [b.strip()[:80] for b in bg if isinstance(b, str) and b.strip() and not placeholder(b)][:5]
    r = ext.get("raising_usd")
    out["raising_usd"] = float(r) if _number(r) else None
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
    # Focus words outside the known sectors ("robotics") count when the sector or one-liner says them.
    known = {"ai", "devtools", "fintech", "consumer", "cpg", "climate", "health"}
    about = f"{sector} {ext.get('one_liner') or ''}".lower()
    stems = [w[:-3] if len(w) > 6 and w.endswith(("ics", "ing")) else w for w in member.get("extra_focus") or []]
    if any(re.search(rf"\b{re.escape(w)}", about) for w in stems):
        thesis = 1.0
    else:
        thesis = 0.5 if sector not in known else 1.0 if sector in member["sectors"] else 0.0
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
