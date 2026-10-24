"""Last Touch rules: triage, extraction and scoring for real founder emails.

Rules (scoring, triage) are plain code so every decision is explainable.
The LLM (Nicrron) only extracts fields from messy text; without a key, a keyword extractor does it.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- deterministic rules
GENERIC_PHRASES = ["dear sir", "madam", "to whom it may concern", "revolutionary", "disrupting the", "trillion",
                   "kindly find", "guaranteed", "game-changing", "seeking investment"]
VENDOR_PHRASES = ["we help vc", "offshore", "dev team", "marketing agency", "our agency", "we build mvps",
                  "right person to talk to about your fund", "book a demo", "unsubscribe"]
NOT_VC_SCALE = ["single-location", "second store", "bakery", "lifestyle business"]
PITCH_WORDS = ["raising", "pre-seed", "preseed", "seed round", "series a", "deck", "founder", "co-founder",
               "we're building", "we are building", "i'm building", "investors", "our startup", "round"]
FIT_KEYWORDS = {
    "cpg": ["p&g", "unilever", "brand manager", "cpg", "grocer", "beverage", "category"],
    "consumer": ["doordash", "instacart", "consumer", "marketplace", "restaurant"],
    "devtools": ["datadog", "stripe", "github", "api", "infra", "engineer"],
    "ai": ["ml", "machine learning", "phd", "research", "data scientist", "engineer"],
    "fintech": ["stripe", "plaid", "payments", "bank", "fintech"],
}
SECTOR_KEYWORDS = {
    "ai": ["ai ", "llm", "machine learning", "agents", "model"],
    "devtools": ["developer", "devtool", "api", "open-source", "open source", "sdk", "infra", "observability"],
    "fintech": ["fintech", "payments", "banking", "lending", "finance team", "accounting"],
    "consumer": ["consumer", "marketplace", "app for", "d2c", "dtc"],
    "cpg": ["cpg", "beverage", "snack", "grocery", "retail", "stores"],
    "climate": ["climate", "carbon", "energy", "solar", "battery"],
    "health": ["health", "clinic", "patient", "medical", "biotech"],
}
TECH_KEYWORDS = ["engineer", "phd", "machine learning", "open-source", "research", "cto"]
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "me.com",
             "proton.me", "protonmail.com", "aol.com", "live.com"}

EMPTY_EXT = {"kind": "founder_pitch", "is_founder": True, "founder": None, "company": None, "website": None,
             "sector": None, "stage": None, "one_liner": "", "metrics": {}, "background": [], "raising_usd": None,
             "closing_weeks": None, "mutuals": [], "specific_ask": False, "ask": None}

EXTRACT_SYSTEM = """You extract structured data from an email sent to a venture capital investor.
Fields: kind (founder_pitch|vendor|spam|promo|other), is_founder (bool), founder, company, website,
sector (one of: consumer, cpg, ai, devtools, fintech, climate, health, other), stage (idea|pre-seed|seed|series-a|later|null),
one_liner (<=15 words), metrics (object of numbers ONLY if stated: mrr_usd, stores, pilots, github_stars, users, paying_customers),
background (list of short strings), raising_usd (number|null), closing_weeks (number|null),
mutuals (list of {name, degree (1|2), trusted (bool)}), specific_ask (bool), ask (string|null).
Use null when not stated. NEVER invent numbers. Use kind "other" for anything that is not a pitch, vendor, spam or promo."""


def triage(msg: dict, ext: dict) -> tuple[str, str]:
    text = (msg.get("subject", "") + " " + msg["text"]).lower()
    kind = ext.get("kind") or "founder_pitch"
    if kind == "vendor" or any(p in text for p in VENDOR_PHRASES):
        return "drop", "Vendor or service pitch, not a founder"
    if kind == "spam" or "presale" in text:
        return "drop", "Spam"
    if kind == "promo" or "webinar" in text:
        return "drop", "Event promo, not a pitch"
    if kind == "other" or ext.get("is_founder") is False:
        return "drop", "Not a founder pitch"
    if any(p in text for p in NOT_VC_SCALE):
        return "drop", "Not VC-scale (local small business)"
    signals = sum(p in text for p in GENERIC_PHRASES) + (0 if ext.get("company") or ext.get("founder") else 1) + (0 if ext.get("specific_ask") else 1)
    if signals >= 3:
        return "drop", f"Generic copy-paste pitch ({signals} template signals, no named company or specific ask)"
    return "keep", "Founder with a specific company or ask"


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


def heuristic_extract(msg: dict) -> dict:
    """Keyword extraction for when no LLM key is set. Only fills fields it can read straight from the email."""
    subject, text = msg.get("subject", ""), msg.get("text", "")
    low = f"{subject} {text}".lower()
    ext = {**EMPTY_EXT, "metrics": {}, "background": [], "mutuals": []}
    if not any(w in low for w in PITCH_WORDS):
        ext.update(kind="other", is_founder=False)
    ext["founder"] = msg.get("from_name") or None
    domain = (msg.get("from_email") or "").split("@")[-1].lower()
    if domain and domain not in FREE_MAIL:
        ext["website"] = domain
        ext["company"] = domain.split(".")[0].replace("-", " ").title()
    for sector, words in SECTOR_KEYWORDS.items():
        if any(w in low for w in words):
            ext["sector"] = sector
            break
    if "pre-seed" in low or "preseed" in low:
        ext["stage"] = "pre-seed"
    elif "series a" in low:
        ext["stage"] = "series-a"
    elif "seed" in low:
        ext["stage"] = "seed"
    m = re.search(r"\$\s?([\d.,]+\s?[km]?)\s*(?:mrr|/mo|per month|monthly)", low)
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
    ext["specific_ask"] = any(w in low for w in ("call", "meet", "chat", "20 minutes", "15 minutes", "coffee"))
    ext["one_liner"] = subject[:90]
    return ext


def features(ext: dict, member: dict) -> dict:
    sector = (ext.get("sector") or "").lower()
    stage = (ext.get("stage") or "").lower()
    thesis = 1.0 if sector in member["sectors"] else 0.0
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
    fit = 1.0 if any(k in bg for k in FIT_KEYWORDS.get(sector, [])) else (0.6 if bg else 0.3)
    mutual = 0.0
    for x in ext.get("mutuals") or []:
        val = 1.0 if x.get("degree") == 1 and x.get("trusted") else 0.8 if x.get("degree") == 1 else 0.5
        mutual = max(mutual, val)
    raising = ext.get("raising_usd") or 0
    if mrr and raising:
        ratio = mrr * 12 / raising
        eff = 1.0 if ratio >= 0.3 else 0.6 if ratio >= 0.15 else 0.3
    else:
        eff = 0.2
    tech = 1.0 if any(k in bg for k in TECH_KEYWORDS) or (m.get("github_stars") or 0) > 0 else (0.4 if bg else 0.2)
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
