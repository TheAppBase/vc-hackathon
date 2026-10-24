"""Last Touch engine: every person at the firm has an agent; agents share one firm memory and talk to each other.

Rules (scoring, triage, routing, follow-up policy) are plain code so every decision is explainable.
The LLM (Nicrron) only extracts fields from messy text and writes short drafts.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import integrations as ix

DATA = ix.DATA


def load(name):
    return json.loads((DATA / name).read_text())


# --------------------------------------------------------------------------- deterministic rules
GENERIC_PHRASES = ["dear sir", "madam", "to whom it may concern", "revolutionary", "disrupting the", "trillion",
                   "kindly find", "guaranteed", "game-changing", "seeking investment"]
VENDOR_PHRASES = ["we help vc", "offshore", "dev team", "marketing agency", "our agency", "we build mvps",
                  "right person to talk to about your fund"]
NOT_VC_SCALE = ["single-location", "second store", "bakery", "lifestyle business"]
FIT_KEYWORDS = {
    "cpg": ["p&g", "unilever", "brand manager", "cpg", "grocer", "beverage", "category"],
    "consumer": ["doordash", "instacart", "consumer", "marketplace", "restaurant"],
    "devtools": ["datadog", "stripe", "github", "api", "infra", "engineer"],
    "ai": ["ml", "machine learning", "phd", "research", "data scientist", "engineer"],
    "fintech": ["stripe", "plaid", "payments", "bank", "fintech"],
}
TECH_KEYWORDS = ["engineer", "phd", "machine learning", "open-source", "research", "cto"]

EMPTY_EXT = {"kind": "founder_pitch", "is_founder": True, "founder": None, "company": None, "website": None,
             "sector": None, "stage": None, "one_liner": "", "metrics": {}, "background": [], "raising_usd": None,
             "closing_weeks": None, "mutuals": [], "specific_ask": False, "ask": None}

EXTRACT_SYSTEM = """You extract structured data from a message sent to a venture capital associate.
Fields: kind (founder_pitch|vendor|spam|promo), is_founder (bool), founder, company, website,
sector (one of: consumer, cpg, ai, devtools, fintech, climate, health, other), stage (idea|pre-seed|seed|series-a|later|null),
one_liner (<=15 words), metrics (object of numbers ONLY if stated: mrr_usd, stores, pilots, github_stars, users, paying_customers),
background (list of short strings), raising_usd (number|null), closing_weeks (number|null),
mutuals (list of {name, degree (1|2), trusted (bool)}), specific_ask (bool), ask (string|null).
Use null when not stated. NEVER invent numbers."""


def triage(msg: dict, ext: dict) -> tuple[str, str]:
    text = (msg.get("subject", "") + " " + msg["text"]).lower()
    kind = ext.get("kind") or "founder_pitch"
    if kind == "vendor" or ext.get("is_founder") is False and kind != "spam" and kind != "promo" or any(p in text for p in VENDOR_PHRASES):
        return "drop", "Vendor or service pitch, not a founder"
    if kind == "spam" or "presale" in text or "token" in text:
        return "drop", "Spam (token presale / guaranteed returns)"
    if kind == "promo" or "webinar" in text:
        return "drop", "Event promo, not a pitch"
    if any(p in text for p in NOT_VC_SCALE):
        return "drop", "Not VC-scale (local small business)"
    signals = sum(p in text for p in GENERIC_PHRASES) + (0 if ext.get("company") or ext.get("founder") else 1) + (0 if ext.get("specific_ask") else 1)
    if signals >= 3:
        return "drop", f"Generic copy-paste pitch ({signals} template signals, no named company or specific ask)"
    return "keep", "Founder with a specific company or ask"


def features(ext: dict, member: dict) -> dict:
    sector = (ext.get("sector") or "").lower()
    stage = (ext.get("stage") or "").lower()
    thesis = 1.0 if sector in member["sectors"] else 0.0
    if stage not in member["stages"]:
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
    return {"member": member["id"], "name": member["name"], "score": round(sum(parts.values())),
            "parts": parts, "features": f}


def decide(ext: dict, best: dict, fund: dict) -> tuple[str, str]:
    sector, stage = ext.get("sector"), ext.get("stage")
    if best["features"]["thesis"] == 0:
        return "pass", f"Outside the firm's thesis ({sector}, {stage}). Decline politely and refer."
    if stage == "idea":
        if best["features"]["founder_fit"] >= 0.6:
            return "warm", "Too early (no product yet), but a promising founder. Keep in touch."
        return "pass", "Too early"
    if best["score"] >= 65:
        cw = ext.get("closing_weeks")
        if fund["status"] == "raising" and cw is not None and cw <= 8:
            return "park", (f"Strong ({best['score']}/100), but they close in {cw} weeks and {fund['label']} is still "
                            f"raising, so we can't commit before their close")
        return "hot", f"Strong fit for {best['name']}'s thesis ({best['score']}/100)"
    if best["score"] >= 45:
        return "warm", f"Promising but not a priority now ({best['score']}/100)"
    return "pass", f"Below the bar ({best['score']}/100)"


def due(hours=0, days=0) -> str:
    return (dt.datetime.now() + dt.timedelta(hours=hours, days=days)).strftime("%a %b %d, %I:%M %p")


def days_since(date_str: str) -> int:
    return (dt.date.today() - dt.date.fromisoformat(date_str)).days


# --------------------------------------------------------------------------- drafts (template, optionally AI-polished)
def first(name):
    return (name or "there").split()[0]


def draft(kind: str, note: dict, members: dict, fund: dict, live_ai: bool, run_id: str) -> str:
    f = first(note.get("founder"))
    company = note.get("company") or "your company"
    sender = members[note.get("draft_sender") or note["owner"]]["name"]
    evaluator = members.get(note.get("routed_to") or "", {}).get("name", "our partner")
    prior = (note.get("touches") or [{}])[0]
    templates = {
        "hot_followup": f"Hi {f}, thanks for reaching out about {company}. {first(evaluator)} on our team would love to "
                        f"learn more. Do you have 20 minutes this week or next? Best, {first(sender)}",
        "reintro": f"Hi {f}, great meeting you at {prior.get('where', 'the event')} on {prior.get('date', '')}, and sorry for the slow "
                   f"follow-up. I'd love to introduce you to my partner {evaluator}, who leads our {note.get('sector')} investing. "
                   f"Open to a quick call this week? {first(sender)}",
        "warm": f"Hi {f}, thanks for sharing what you're working on. It's a bit early for us to engage, but we'd love "
                f"to stay close. Could you add us to your monthly update? Best, {first(sender)}",
        "park": f"Hi {f}, we really like {company}. Our timing is tricky: we're finishing our own {fund['label']} raise "
                f"and can't commit before your close. Can we stay close and reconnect as soon as we can? Best, {first(sender)}",
        "pass": (f"Hi {f}, thank you for thinking of us. {company} sits outside our focus ({note.get('sector')}), so we're "
                 f"not the right partner, but {note['referral']} could be a great fit. "
                 f"Wishing you a strong raise. Best, {first(sender)}") if note.get("referral") else
                (f"Hi {f}, thank you for sharing {company} with us. It isn't a fit for us at this stage, but we'd be glad "
                 f"to hear how it develops. Wishing you a strong raise. Best, {first(sender)}"),
        "reengage": f"Hi {f}, good news on our side: {fund['label']} has closed. We'd love to pick up where we left off "
                    f"on {company}. Do you have time for a call next week? Best, {first(sender)}",
    }
    text = templates.get(kind, "")
    if live_ai and ix.LLM_CLIENT.live and text:
        polished = ix.LLM_CLIENT.chat(
            "You polish short outreach messages from a VC to a founder. Keep it under 80 words, warm and specific. "
            "Keep every fact; add NO new facts, numbers or commitments. Return only the message.",
            f"Founder note: {json.dumps({k: note.get(k) for k in ('founder', 'company', 'one_liner', 'sector')})}\n"
            f"Draft: {text}", agent_name=f"{sender.split()[0]}'s agent", max_tokens=220, session_id=run_id)
        if polished:
            text = polished.strip()
    return text


# --------------------------------------------------------------------------- briefs (deterministic, every claim sourced)
def money(x):
    if not x:
        return None
    return f"${x / 1_000_000:.1f}M" if x >= 1_000_000 else f"${x / 1000:.0f}k"


def metrics_line(m: dict) -> str:
    labels = {"mrr_usd": lambda v: f"{money(v)}/month revenue", "stores": lambda v: f"{v} stores",
              "pilots": lambda v: f"{v} pilots or design partners", "github_stars": lambda v: f"{v:,} GitHub stars",
              "paying_customers": lambda v: f"{v} paying customers", "repeat_rate": lambda v: f"{int(v * 100)}% repeat rate",
              "retail_chains": lambda v: f"in {v} retail chains", "users": lambda v: f"{v:,} users",
              "mom_velocity_growth": lambda v: f"{int(v * 100)}% MoM velocity growth",
              "waste_reduction_claimed": lambda v: f"~{int(v * 100)}% waste reduction (founder's claim)",
              "signed_contracts_usd": lambda v: f"{money(v)} signed contracts"}
    parts = [labels[k](v) for k, v in (m or {}).items() if v and k in labels]
    return ", ".join(parts) if parts else "Not stated"


def render_brief(n: dict, members: dict) -> str:
    src = f"[{n.get('msg_id', 'memory')}]"
    ak = n.get("akta") or {}
    lines = [f"### {n.get('company') or n.get('founder')}: {n.get('one_liner', '')}",
             f"**Verdict:** {n['status'].upper()} → {members.get(n.get('routed_to') or '', {}).get('name', '-')}"
             + (f" · score {n['score']}/100" if n.get('score') is not None else " · outside thesis"),
             f"**What they build:** {n.get('one_liner') or 'Not stated'} {src}",
             f"**Stage / raise:** {n.get('stage') or 'Not stated'}"
             + (f", raising {money(n.get('raising_usd'))}" if n.get('raising_usd') else "")
             + (f", closing in {n['closing_weeks']} weeks" if n.get('closing_weeks') else "") + f" {src}",
             f"**Team and background:** {n.get('founder') or 'Unknown'}: {'; '.join(n.get('background') or []) or 'Not stated'} {src}",
             f"**Traction:** {metrics_line(n.get('metrics'))} {src}",
             f"**Website:** {n.get('website') or 'Not provided'}",
             f"**Why it fits (or doesn't):** {n.get('reason', '')}"]
    if n.get("parts"):
        lines.append("**Score breakdown:** " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in n["parts"].items()))
    muts = n.get("mutuals") or []
    lines.append("**Mutual connections:** " + (", ".join(f"{m['name']} ({m['degree']}°{', trusted' if m.get('trusted') else ''})" for m in muts) or "None") + f" {src}")
    if n.get("prior"):
        p = n["prior"]
        lines.append(f"**Firm memory:** {members[p['who']]['name']} met {n.get('founder')} on {p['date']} at {p['where']} "
                     f"({p['days']} days ago). No follow-up was sent. [firm memory]")
    tag = f"[{ak.get('source', 'Akta')}]"
    ext_bits = []
    if ak.get("match"):
        mt = ak["match"]
        ext_bits.append(f"closest Akta record: {mt['name']} ({mt.get('website') or 'no site'}); verify it's the same company")
    elif ak.get("live"):
        ext_bits.append("not yet in Akta (normal for pre-seed)")
    if ak.get("industry"):
        ext_bits.append(f"industry: {ak['industry']['name']}")
    if ak.get("comps"):
        ext_bits.append("comparables: " + ", ".join(c["name"] for c in ak["comps"][:5]))
    if ak.get("news"):
        ext_bits.append("recent news in this space: " + "; ".join(a["title"] for a in ak["news"][:3]))
    lines.append("**Outside signals:** " + ("; ".join(ext_bits) if ext_bits else "none") + f" {tag}")
    lines.append(f"**Sources:** {src} original {n.get('channel', '')} message"
                 + (" · [firm memory] Roundtable note history" if n.get("prior") else "") + f" · {tag}")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- the run
def extract(msg: dict, live_ai: bool, run_id: str) -> tuple[dict, str]:
    if live_ai and ix.LLM_CLIENT.live:
        got = ix.LLM_CLIENT.json(EXTRACT_SYSTEM, f"Channel: {msg['channel']}\nFrom: {msg['from']}\nSubject: {msg.get('subject', '')}\n\n{msg['text']}",
                                 agent_name="Ava's agent", session_id=run_id)
        if got:
            return {**EMPTY_EXT, **got}, "Nicrron (live)"
    return {**EMPTY_EXT, **msg.get("demo_extraction", {})}, "cached demo extraction"


def run_firm(live_ai: bool = False, fund_status: str | None = None) -> dict:
    firm = load("firm.json")
    fund = firm["fund"]
    if fund_status:
        fund["status"] = fund_status
    members = {m["id"]: m for m in firm["members"]}
    partners = [m for m in firm["members"] if m["weights"]]
    inbox = load("inbox.json")
    notes = copy.deepcopy(load("memory_seed.json"))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    feed, triage_rows = [], []

    def say(agent, text, note_id=None):
        feed.append({"t": dt.datetime.now().strftime("%I:%M:%S %p"), "agent": agent, "text": text, "note": note_id})

    chans = {}
    for m in inbox:
        chans[m["channel"]] = chans.get(m["channel"], 0) + 1
    say("Ava's agent (Associate)", f"Picked up {len(inbox)} new messages ("
        + ", ".join(f"{v} {k}" for k, v in chans.items()) + "). Triaging against the fund thesis.")

    # 1) extract (parallel when live)
    with ThreadPoolExecutor(max_workers=6) as pool:
        extracted = list(pool.map(lambda m: extract(m, live_ai, run_id), inbox))

    ava_steps, kept = [], []
    for msg, (ext, how) in zip(inbox, extracted):
        decision, reason = triage(msg, ext)
        triage_rows.append({"id": msg["id"], "channel": msg["channel"], "from": msg["from"],
                            "preview": (msg.get("subject") or msg["text"])[:80], "triage": decision, "reason": reason,
                            "extraction": how})
        ava_steps.append({"step_type": "tool_call", "label": f"extract+triage {msg['id']}", "tool_name": "nicrron_extract",
                          "input_summary": msg["text"][:120], "output_summary": f"{decision}: {reason}"})
        if decision == "keep":
            kept.append((msg, ext))
    dropped = [r for r in triage_rows if r["triage"] == "drop"]
    say("Ava's agent (Associate)", f"Kept {len(kept)} founders, dropped {len(dropped)}: "
        + "; ".join(sorted({r['reason'].split(' (')[0] for r in dropped})) + ".")

    # 2) outside data (parallel)
    with ThreadPoolExecutor(max_workers=6) as pool:
        aktas = list(pool.map(lambda me: ix.AKTA.enrich_lead(me[1].get("company"), me[1].get("website"), me[1].get("one_liner") or ""), kept))
    for (msg, ext), ak in zip(kept, aktas):
        ava_steps.append({"step_type": "tool_call", "label": f"akta enrich {ext.get('company') or ext.get('founder')}",
                          "tool_name": "akta", "output_summary": f"source={ak['source']}, comps={len(ak['comps'])}"})

    # 3) each partner's agent scores each lead; router decides; memory check
    partner_steps = {p["id"]: [] for p in partners}
    for (msg, ext), ak in zip(kept, aktas):
        scores = sorted((score_member(ext, p) for p in partners), key=lambda s: -s["score"])
        for s in scores:
            partner_steps[s["member"]].append({"step_type": "reasoning", "label": f"score {ext.get('company') or ext.get('founder')}",
                                               "output_summary": f"{s['score']}/100 {s['parts']}"})
        best = scores[0]
        status, reason = decide(ext, best, fund)
        prior_note = next((n for n in notes if n.get("founder") and ext.get("founder")
                           and n["founder"].lower() == ext["founder"].lower()), None)
        note = prior_note or {"id": f"n-{msg['id']}", "touches": [], "actions": []}
        owner = (prior_note or {}).get("owner") or msg["to"]
        note.update({k: ext.get(k) for k in ("founder", "company", "website", "sector", "stage", "one_liner", "metrics",
                                             "background", "raising_usd", "closing_weeks", "mutuals", "ask")})
        note.update({"msg_id": msg["id"], "channel": msg["channel"], "owner": owner,
                     "routed_to": best["member"] if best["features"]["thesis"] > 0 else None,
                     "score": best["score"] if best["features"]["thesis"] > 0 else None,
                     "parts": best["parts"] if best["features"]["thesis"] > 0 else None, "all_scores": {s["member"]: s["score"] for s in scores},
                     "status": status, "reason": reason, "akta": ak, "source": msg["channel"]})
        note["touches"].append({"date": msg["received"][:10], "who": msg["to"], "channel": msg["channel"],
                                "where": "inbound" if msg["channel"] != "event" else "Deal Flow hackathon",
                                "summary": (msg.get("subject") or msg["text"])[:90]})
        sector = ext.get("sector") or ""
        if status == "pass" and sector in firm["referrals"]:
            note["referral"] = firm["referrals"][sector]
        label = ext.get("company") or ext.get("founder")
        best_agent = f"{best['name'].split()[0]}'s agent (Partner)"

        # follow-up policy
        acts = []
        if prior_note:
            p = prior_note["touches"][0]
            note["prior"] = {"who": p["who"], "date": p["date"], "where": p["where"], "days": days_since(p["date"])}
            say(f"{members[p['who']]['name'].split()[0]}'s agent (Partner)",
                f"Heads-up: I already know {ext.get('founder')}. I met them on {p['date']} at {p['where']}, "
                f"{note['prior']['days']} days ago, and no follow-up was sent. I own this relationship.", note["id"])
        if status == "hot":
            say(best_agent, f"{label}: {reason}. Requesting a first call.", note["id"])
            if prior_note and prior_note["owner"] != best["member"]:
                say("Router", f"{label} → {best['name']} evaluates (thesis fit {best['score']}/100); "
                    f"{members[prior_note['owner']]['name']} sends a warm re-intro within 24h.", note["id"])
                acts.append({"who": prior_note["owner"], "what": "Send warm re-intro (you met them first)", "due": due(hours=24), "kind": "reintro"})
                acts.append({"who": best["member"], "what": "Take the first call", "due": due(days=3), "kind": "first_call"})
            else:
                acts.append({"who": best["member"], "what": "Follow up: propose a 20-minute call", "due": due(hours=24), "kind": "hot_followup"})
            acts.append({"who": best["member"], "what": "Second touch if no reply", "due": due(days=14), "kind": "second_touch"})
        elif status == "park":
            say(best_agent, f"{label}: {reason}. Parked; I'll resurface it when we close.", note["id"])
            acts.append({"who": best["member"], "what": f"Re-engage when {fund['label']} closes or on a new signal", "trigger": "fund_closed | signal", "kind": "park"})
        elif status == "warm":
            say(best_agent if best["features"]["thesis"] > 0 else "Ava's agent (Associate)", f"{label}: {reason.rstrip('.')}.", note["id"])
            acts.append({"who": owner, "what": "Send keep-in-touch note; ask for monthly updates", "due": due(hours=48), "kind": "warm"})
            acts.append({"who": owner, "what": "Re-check progress", "due": due(days=60), "kind": "recheck"})
        else:
            say("Ava's agent (Associate)", f"{label}: {reason}" + (f" Suggesting {note['referral']}." if note.get("referral") else ""), note["id"])
            acts.append({"who": owner, "what": "Send polite decline" + (" + referral" if note.get("referral") else ""), "due": due(days=3), "kind": "pass"})
        note["actions"] = acts
        if not prior_note:
            notes.append(note)

    # 4) drafts (only for actions that send something)
    draftable = {"reintro", "hot_followup", "warm", "pass", "park"}
    jobs = []
    for n in notes:
        for a in n.get("actions", []):
            if a["kind"] in draftable and n.get("msg_id"):
                jobs.append((n, a))

    def make(job):
        n, a = job
        n2 = {**n, "draft_sender": a["who"]}
        return draft(a["kind"], n2, members, fund, live_ai, run_id)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for (n, a), text in zip(jobs, pool.map(make, jobs)):
            a["draft"] = text

    # 5) traces → PRISM
    model = ix.LLM_CLIENT.model if (live_ai and ix.LLM_CLIENT.live) else "rules+cache"
    ix.TRACER.local.clear()
    ava_steps.append({"step_type": "final_answer", "label": "post leads to firm memory",
                      "output_summary": f"{len(kept)} notes posted, {len(dropped)} dropped"})
    ix.TRACER.trajectory("Ava's agent (Associate)", ava_steps, run_id, model)
    for pid, steps in partner_steps.items():
        steps.append({"step_type": "final_answer", "label": "verdicts", "output_summary": f"{len(steps)} leads scored"})
        ix.TRACER.trajectory(f"{members[pid]['name'].split()[0]}'s agent (Partner)", steps, run_id, model)

    state = {"run_id": run_id, "fund": fund, "members": members, "notes": notes, "feed": feed,
             "triage": triage_rows, "live_ai": live_ai}
    persist(state)
    return state


def persist(state):
    ix.NOTES.save_all(state["notes"], lambda n: render_brief(n, state["members"]) if n.get("status") else json.dumps(n, indent=1))


# --------------------------------------------------------------------------- watcher
def watcher_fund_closed(state: dict) -> dict:
    fund, members = state["fund"], state["members"]
    fund["status"] = "closed"
    revived = []
    for n in state["notes"]:
        if n.get("status") == "park":
            n["was_parked"] = True
            n["status"], n["reason"] = "hot", f"{fund['label']} closed, so re-engage now (was parked: {n.get('reason', '')})"
            who = n.get("routed_to") or n.get("owner")
            n["actions"] = [{"who": who, "what": "Re-engage now: fund closed", "due": due(hours=24), "kind": "reengage"},
                            {"who": who, "what": "Second touch if no reply", "due": due(days=14), "kind": "second_touch"}]
            n["actions"][0]["draft"] = draft("reengage", {**n, "draft_sender": who}, members, fund, state.get("live_ai", False), state["run_id"])
            revived.append(n.get("company") or n.get("founder"))
    state["feed"].append({"t": dt.datetime.now().strftime("%I:%M:%S %p"), "agent": "Watcher",
                          "text": f"{fund['label']} closed. Resurfaced {len(revived)} parked founders: {', '.join(revived) or 'none'}. "
                                  f"Re-engage drafts are ready.", "note": None})
    ix.TRACER.trajectory("Watcher", [{"step_type": "tool_call", "label": "fund status changed", "tool_name": "fund_status",
                                      "output_summary": "closed"},
                                     {"step_type": "final_answer", "label": "resurface parked", "output_summary": ", ".join(revived)}],
                         state["run_id"])
    persist(state)
    return state


def watcher_signals(state: dict) -> dict:
    found = []
    for n in state["notes"]:
        if n.get("status") == "park" or n.get("was_parked"):
            sig = ix.AKTA.signal_for(n.get("company") or "", n.get("website"))
            if sig:
                n.setdefault("signals", []).append(sig)
                found.append(f"{n.get('company')}: {sig['text']} [{sig['source']}]")
    state["feed"].append({"t": dt.datetime.now().strftime("%I:%M:%S %p"), "agent": "Watcher",
                          "text": ("New signals on watched founders: " + " | ".join(found)) if found else "No new signals on watched founders.",
                          "note": None})
    persist(state)
    return state
