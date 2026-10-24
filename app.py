"""Last Touch MVP (Track 1, sourcing). Flask app for Vercel (exports `app`).

Every person at the firm gets a personal agent. The agent interviews them to learn their domain knowledge,
then sources founders with their own criteria, tracks who they have been talking to across platforms,
takes notes by voice or text, and talks to teammates' agents about who owns which founder and what's next.
Everything runs on mock data; scoring uses the rules engine in engine.py with weights from the interview.

Run locally: flask --app app run
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import datetime as dt  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402

from flask import Flask, jsonify, request  # noqa: E402

import engine  # noqa: E402
import integrations as ix  # noqa: E402
import notes as nx  # noqa: E402

app = Flask(__name__)
MVP = engine.load("mvp.json")
FIRM = engine.load("firm.json")

_state: dict = {}
_lock = threading.Lock()


def base_state() -> dict:
    """One deterministic agent run over the mock inbox, cached per server instance."""
    with _lock:
        if "s" not in _state:
            _state["s"] = engine.run_firm(live_ai=False)
        return _state["s"]


def first(name: str | None) -> str:
    return (name or "").split()[0] if name else ""


CHANNEL = {"linkedin": "LinkedIn", "email": "Gmail", "event": "Event notes"}
WEIGHTS = {
    "technical": {"thesis": 30, "traction": 10, "founder_fit": 20, "mutual": 5, "capital_efficiency": 0, "technical_depth": 35},
    "revenue": {"thesis": 30, "traction": 30, "founder_fit": 15, "mutual": 10, "capital_efficiency": 15, "technical_depth": 0},
    "warm": {"thesis": 30, "traction": 15, "founder_fit": 15, "mutual": 30, "capital_efficiency": 5, "technical_depth": 5},
    "balanced": {"thesis": 30, "traction": 20, "founder_fit": 20, "mutual": 10, "capital_efficiency": 10, "technical_depth": 10},
}
LABEL = {"thesis": "thesis fit", "traction": "traction", "founder_fit": "founder fit", "mutual": "warm intro",
         "capital_efficiency": "capital efficiency", "technical_depth": "technical depth"}


def build_profile(a: dict) -> tuple[dict, dict]:
    name = (str(a.get("name") or "").strip() or "You")[:40]
    focus = str(a.get("focus") or "")
    fl = focus.lower()
    sectors = []
    if re.search(r"\b(ai|a\.i\.|devtools?|dev tools|infra|developers?)\b", fl):
        sectors += ["ai", "devtools"]
    if any(k in fl for k in ("consumer", "cpg", "brand", "food", "retail")):
        sectors += ["consumer", "cpg"]
    if any(k in fl for k in ("fintech", "payments", "finance")):
        sectors.append("fintech")
    for k in ("climate", "health"):
        if k in fl:
            sectors.append(k)
    sectors = sectors or list(FIRM["fund"]["sectors"])
    stages = ["seed"]
    if "pre-seed" in fl or "preseed" in fl:
        stages = ["pre-seed", "seed"]
    if "series a" in fl or "series-a" in fl:
        stages = ["seed", "series-a"]
    if "idea" in fl:
        stages.insert(0, "idea")
    sig = str(a.get("signal") or "").lower()
    if any(k in sig for k in ("technical", "open-source", "open source", "engineer", "github")):
        style = "technical"
    elif any(k in sig for k in ("revenue", "repeat", "traction", "mrr", "sales")):
        style = "revenue"
    elif any(k in sig for k in ("intro", "trust", "network", "referral")):
        style = "warm"
    else:
        style = "balanced"
    sources = a.get("sources") or []
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    if not isinstance(sources, (list, tuple)):
        sources = []
    member = {"id": "me", "name": name, "sectors": sectors, "stages": stages, "weights": WEIGHTS[style]}
    fixtures = engine.load("akta_fixtures.json")["comps"]
    state = base_state()
    comps = [c for n in state["notes"] if n.get("status") and n.get("sector") in sectors for c in fixtures.get(n.get("company") or "", [])]
    profile = {
        "name": name, "first": first(name), "role": str(a.get("role") or "Investor")[:80], "focus": focus[:80] or "Generalist",
        "sectors": sectors, "stages": stages, "signal": str(a.get("signal") or "")[:100], "style": style,
        "sources": [str(s)[:30] for s in sources][:6], "nos": str(a.get("nos") or "No hard no's")[:80], "weights": WEIGHTS[style],
        "weights_text": ", ".join(f"{LABEL[k]} {v}%" for k, v in sorted(WEIGHTS[style].items(), key=lambda kv: -kv[1]) if v),
        "akta": ("mapped " + ", ".join(dict.fromkeys(comps).keys()) if comps else "no comparables yet") + " [demo fixture]",
    }
    return profile, member


def nice_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def lead_for(n: dict, member: dict, members: dict) -> dict:
    s = engine.score_member(n, member)
    thesis = s["features"]["thesis"]
    score = s["score"]
    if thesis == 0:
        verdict = "Outside your thesis"
    elif n.get("status") == "park":
        verdict = "Parked"
    elif score >= 65:
        verdict = "Take the first call"
    elif score >= 45:
        verdict = "Keep warm"
    else:
        verdict = "Pass"
    top = sorted(((k, v) for k, v in s["parts"].items() if v), key=lambda kv: -kv[1])[:2]
    why = ", ".join(f"{LABEL[k]} {v:g}/{member['weights'][k]}" for k, v in top)
    if n.get("closing_weeks") and n["closing_weeks"] <= 8:
        why += f". Closing in {n['closing_weeks']} weeks"
    acts = [a for a in n.get("actions", []) if a["kind"] not in ("second_touch", "recheck")]
    lead_act = acts[0] if acts else None
    evaluator = first(members[n["routed_to"]]["name"]) if n.get("routed_to") in members else None
    founder, company = n.get("founder"), n.get("company")
    known = None
    if n.get("prior"):
        p = n["prior"]
        who = first(members[p["who"]]["name"])
        call = evaluator or who
        known = {"id": p["who"], "name": who, "rank": 0,
                 "text": f"{who} met {founder} on {nice_date(p['date'])} at {p['where']}, {p['days']} days ago, and no follow-up was sent. "
                         f"{who} will send a warm re-intro within 24 hours, and {call} takes the first call. I'll add you to it.",
                 "next": f"{who} re-intros within 24h. {call} takes the first call, you join."}
    elif n.get("owner") and n["owner"] != "ava":
        who = first(members[n["owner"]]["name"])
        known = {"id": n["owner"], "name": who, "rank": 1,
                 "text": f"{who} met {founder} at the Deal Flow hackathon yesterday and owns the relationship. "
                         f"{who} is proposing a 20-minute call and will add you to it.",
                 "next": f"{who} proposes the call. You join."}
    elif n.get("status") == "park" and lead_act:
        who = first(members[lead_act["who"]]["name"])
        known = {"id": lead_act["who"], "name": who, "rank": 3,
                 "text": f"{who} parked {company}: they close in {n.get('closing_weeks')} weeks and Fund II is still raising, "
                         f"so we can't commit before their close. I'll resurface it the day Fund II closes.",
                 "next": "Parked until Fund II closes."}
    elif lead_act:
        who = first(members[lead_act["who"]]["name"])
        label = {"hot": "hot", "warm": "keeping warm", "pass": "declining"}.get(n.get("status"), n.get("status"))
        what = lead_act["what"].replace("Follow up: ", "").rstrip(".")
        known = {"id": lead_act["who"], "name": who, "rank": 2 if n.get("status") == "hot" else 4,
                 "text": f"{who} owns {company} ({label}). Next step on the board: {what[0].lower() + what[1:]}. "
                         + ("I'll add you to the call." if n.get("status") == "hot" else "Happy to add you as co-owner."),
                 "next": f"{who}: {what}."}
    ak = n.get("akta") or {}
    comps = ", ".join(c["name"] for c in ak.get("comps", [])[:4])
    f = first(founder) or "there"
    if n.get("channel") == "event":
        draft = (f"Hi {f}, great meeting you at the Deal Flow hackathon. I'd love to keep the conversation about "
                 f"{company or 'your company'} going. Do you have 20 minutes next week? Best, {first(member['name'])}")
    else:
        draft = (f"Hi {f}, thanks for reaching out about {company or 'your company'}. I'd love to learn more about what you're "
                 f"building. Do you have 20 minutes next week? Best, {first(member['name'])}")
    return {
        "id": n["id"], "company": company, "founder": founder, "one_liner": n.get("one_liner") or "",
        "sector": n.get("sector"), "stage": n.get("stage"), "channel": CHANNEL.get(n.get("channel"), n.get("channel")),
        "msg_id": n.get("msg_id"), "score": score, "verdict": verdict, "why": why, "known": known,
        "akta": (f"comparables: {comps}" if comps else "no records yet (normal for pre-seed)") + f" [{ak.get('source', 'demo fixture')}]",
        "traction": engine.metrics_line(n.get("metrics")), "background": "; ".join(n.get("background") or []) or "Not stated",
        "raise": engine.money(n.get("raising_usd")), "draft": draft,
    }


def team_board(state: dict) -> list[dict]:
    members = state["members"]
    rows = []
    for n in state["notes"]:
        if not n.get("status"):
            continue
        for a in n.get("actions", []):
            if a["kind"] in ("second_touch", "recheck"):
                continue
            rows.append({"who": a["who"], "name": members[a["who"]]["name"], "founder": n.get("founder"), "company": n.get("company"),
                         "status": n["status"], "what": a["what"].replace("(you met them first)", "(met them first)"),
                         "due": a.get("due") or "When Fund II closes or a new signal"})
    order = {"ben": 0, "cara": 1, "ava": 2}
    return sorted(rows, key=lambda r: (order.get(r["who"], 9), r["status"] != "hot"))


def handoff(state: dict) -> str:
    """The one dropped-ball the team board should call out: a founder someone met but never followed up with."""
    members = state["members"]
    for n in state["notes"]:
        if n.get("prior"):
            who = first(members[n["prior"]["who"]]["name"])
            call = first(members[n["routed_to"]]["name"]) if n.get("routed_to") in members else who
            return (f"One handoff to watch: {who} met {n['founder']} {n['prior']['days']} days ago and never followed up. "
                    f"{who}'s agent has a re-intro draft ready, and {call} takes the first call.")
    return ""


def note_facts(note: dict) -> list[str]:
    facts = []
    if note.get("one_liner"):
        facts.append(note["one_liner"])
    if note.get("event"):
        facts.append(f"Met at {note['event']}")
    if note.get("stage"):
        facts.append({"pre-seed": "Pre-seed", "seed": "Seed", "series-a": "Series A", "idea": "Idea stage"}.get(note["stage"], note["stage"]))
    if note.get("raising_usd"):
        facts.append(f"Raising {engine.money(note['raising_usd'])}")
    traction = engine.metrics_line(note.get("metrics"))
    if traction != "Not stated":
        facts.append(traction)
    return facts


ACTION = re.compile(r"\bnext steps?\b[\s:,-]*|\b(?:follow up|send|intro(?:duce)?|schedule|set up|loop in|book|email|call)\b", re.I)
SECTOR_LABEL = {"ai": "AI", "cpg": "CPG", "devtools": "devtools", "fintech": "fintech", "consumer": "consumer"}


def tidy_next(nxt: str) -> str:
    """Browser dictation has no punctuation, so a next step can swallow the whole note. Start it at the last action phrase."""
    if len(nxt) > 45 or "," in nxt:
        h = ACTION.search(nxt)
        if h:
            cand = nxt[h.end() if h.group(0).lower().startswith("next step") else h.start():].strip(" ,.:;-")
            if len(cand) >= 6:
                nxt = cand
    return nxt[0].upper() + nxt[1:]


def due_label(iso: str | None) -> str:
    if not iso:
        return "this week"
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}"


@app.route("/api/note", methods=["POST"])
def api_note():
    """Read a dictated or typed note: who you met, at which company, what's next, and who at the firm already knows them."""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    text = str(body.get("text") or "").strip()[:2000]
    if not text:
        return jsonify({"error": "The note is empty."}), 400
    today = dt.date.today()
    ext, how = None, "keyword rules"
    if ix.LLM_CLIENT.live:
        got = ix.LLM_CLIENT.json(nx.NOTE_SYSTEM.format(today=today.isoformat(), weekday=today.strftime("%A")), text,
                                 agent_name="Note taker")
        if isinstance(got, dict):
            ext, how = got, "Nicrron"
    if ext is None:
        ext = nx.heuristic_note(text, today)
    note = nx.clean_note(ext)
    if not note["founder"] and how == "keyword rules":
        # "Dana Lee, Orbit, ..." or "Dana Lee from Orbit ...": a note that starts with the person's name.
        m = re.match(r"\s*([A-Z][a-z'\u2019]+(?:\s[A-Z][a-z'\u2019]+){0,2})\s*(?:,|\bfrom\b|\bof\b|\bat\b)\s*"
                     r"([A-Z][\w&.\-]*(?:\s[A-Z][\w&.\-]*)?)?", text)
        if m and m.group(1).split()[0].lower() not in nx.STOP | {"okay", "ok", "so", "just", "great", "note", "today", "met", "i"}:
            note["founder"] = m.group(1)
            if not note["company"] and m.group(2):
                note["company"] = m.group(2)

    # Does anyone at the firm already know this founder? Check firm memory, then route by thesis.
    state = base_state()
    members = state["members"]
    match = None
    for n in state["notes"]:
        if not n.get("status"):
            continue
        same_founder = note["founder"] and (n.get("founder") or "").lower() == note["founder"].lower()
        same_company = note["company"] and (n.get("company") or "").lower() == note["company"].lower()
        if same_founder or same_company:
            match = n
            break
    who = note["founder"] or note["company"] or "them"
    known = None
    if match:
        neutral = {"id": "me", "name": "You", "sectors": [], "stages": [], "weights": WEIGHTS["balanced"]}
        known = lead_for(match, neutral, members)["known"]
        if known:
            known = {**known, "intro": False}
    if known is None:
        partner = next((m for m in members.values() if m["weights"] and note["sector"] in m["sectors"]), None)
        if partner:
            p = first(partner["name"])
            known = {"id": partner["id"], "name": p, "intro": True,
                     "text": f"{p} hasn't met {who} yet, and nobody else at the firm has either. It sounds like {p}'s thesis "
                             f"({SECTOR_LABEL.get(note['sector'], note['sector'])}), so {p} would like an intro. {p} is free Monday at 10:00 or 2:00. "
                             f"I'll wait for your approval before anything goes out.",
                     "next": f"You own the relationship. {p} would like an intro."}
        else:
            known = {"id": None, "name": "", "intro": False,
                     "text": f"Nobody at the firm has met {who} yet, so the relationship is yours.",
                     "next": "You own the relationship."}

    saved = "local"
    if ix.NOTES.live:
        try:
            ix.NOTES.call("save_note", {"kind": "note", "folder": ix.NOTES.folder, "content": text,
                                        "title": f"Last Touch note · {note['founder'] or note['company'] or 'untitled'}",
                                        "request_id": f"lt-note-{abs(hash(text)) % 10**12}"})
            saved = "roundtable"
        except Exception as e:  # keep the note in the session; say the save failed
            ix.NOTES.error = f"Roundtable save_note failed: {e}"
            saved = "failed"
    nxt = tidy_next(note["next_step"] or "Follow up this week")
    return jsonify({"note": note, "how": how, "known": known, "saved": saved, "facts": note_facts(note),
                    "next": nxt, "due": due_label(note["next_due"])})


@app.route("/api/build", methods=["POST"])
def api_build():
    answers = request.get_json(silent=True)
    answers = answers if isinstance(answers, dict) else {}
    profile, member = build_profile(answers)
    state = base_state()
    leads = [lead_for(n, member, state["members"]) for n in state["notes"] if n.get("status")]
    leads.sort(key=lambda l: (l["verdict"] == "Outside your thesis", -l["score"]))
    return jsonify({"profile": profile, "leads": leads})


ICONS = {
    "spark": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    "tool": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "db": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    "agent": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
    "settings": '<line x1="21" y1="4" x2="14" y2="4"/><line x1="10" y1="4" x2="3" y2="4"/><line x1="21" y1="12" x2="12" y2="12"/><line x1="8" y1="12" x2="3" y2="12"/><line x1="21" y1="20" x2="16" y2="20"/><line x1="12" y1="20" x2="3" y2="20"/><line x1="14" y1="2" x2="14" y2="6"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="16" y1="18" x2="16" y2="22"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "userplus": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>',
    "Gmail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
    "LinkedIn": '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    "Google Calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
    "WhatsApp": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "Event notes": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
}


@app.route("/")
def index():
    state = base_state()
    data = {
        "mvp": MVP, "icons": ICONS, "fund": FIRM["fund"],
        "team": [{"id": m["id"], "name": m["name"], "role": m["role"]} for m in state["members"].values()],
        "board": team_board(state), "labels": LABEL, "handoff": handoff(state),
    }
    blob = json.dumps(data).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Last Touch</title><style>
:root{--text:#1d1d1f;--muted:rgba(29,29,31,.6);--glass:rgba(255,255,255,.42);--glass-strong:rgba(255,255,255,.64);--edge:rgba(255,255,255,.72);
--line:rgba(29,29,31,.1);--shadow:0 10px 40px rgba(16,44,52,.18),0 1px 2px rgba(16,44,52,.08);--blue:#0a84ff;--red:#ff3b30;--orange:#ff9500;
--green:#34c759;--gray:#8e8e93;--solid:#eef3f2;--grad:linear-gradient(180deg,#3d9bff,#0a84ff);
--sky:linear-gradient(180deg,#a7cdd2 0%,#bcd6d3 30%,#cfdcd4 48%,#8fae9f 66%,#55776d 82%,#2f4a47 100%)}
@media (prefers-color-scheme:dark){:root{--text:#f5f5f7;--muted:rgba(245,245,247,.62);--glass:rgba(28,34,38,.5);--glass-strong:rgba(38,44,48,.74);
--edge:rgba(255,255,255,.16);--line:rgba(255,255,255,.1);--shadow:0 10px 40px rgba(0,0,0,.45);--solid:#252b2e;
--sky:linear-gradient(180deg,#0e2a33 0%,#16343a 35%,#1f3d3b 55%,#13282a 80%,#0a1516 100%)}}
*{box-sizing:border-box}html{background:#2f4a47}
body{margin:0;height:100vh;height:100dvh;overflow:hidden;color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased;background:var(--sky)}
body:before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(60% 40% at 80% 70%,rgba(64,110,96,.55),transparent 70%),
radial-gradient(50% 30% at 15% 75%,rgba(214,120,72,.26),transparent 70%),radial-gradient(70% 45% at 50% 0%,rgba(255,255,255,.35),transparent 70%)}
button{font:inherit;color:inherit;cursor:pointer}.ic{flex:none;vertical-align:-3px}
.glass{background:var(--glass-strong);border:1px solid var(--edge);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.55);
-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}
.app{height:100%;display:flex;flex-direction:column}
.chat{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:4px;padding:72px max(20px,calc(50% - 440px)) 12px;scroll-behavior:smooth}
.msg{display:flex;gap:10px;align-items:flex-end;padding:3px 0;animation:in .25s ease-out}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.avatar{display:inline-grid;place-items:center;flex:none;width:34px;height:34px;border-radius:50%;color:#fff;font-weight:700;font-size:13px;
background:linear-gradient(160deg,color-mix(in srgb,var(--hue,#8e8e93) 65%,#fff),var(--hue,#8e8e93));box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 2px 8px rgba(0,0,0,.15)}
.avatar.me-agent{--hue:#0a84ff}
.bubble{background:var(--glass-strong);border:1px solid var(--edge);border-radius:20px 20px 20px 6px;padding:9px 14px;max-width:min(640px,82%);
-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);box-shadow:0 2px 10px rgba(16,44,52,.08)}
.bubble b.who{display:block;font-size:12.5px;color:var(--muted);font-weight:600;margin-bottom:1px}
.msg.me{justify-content:flex-end}.msg.me .bubble{background:var(--grad);color:#fff;border-color:transparent;border-radius:20px 20px 6px 20px}
.msg.sys{justify-content:center}.msg.sys span{font-size:12.5px;color:var(--muted);padding:4px 12px;border-radius:999px;background:var(--glass)}
.typing .bubble{display:flex;gap:4px;padding:13px 14px}.typing i{width:7px;height:7px;border-radius:50%;background:var(--muted);animation:dot 1s infinite}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}@keyframes dot{0%,80%,100%{opacity:.25}40%{opacity:1}}
.indent{margin-left:44px}
.toolcard{max-width:600px;padding:8px 12px;border-radius:14px;font-size:12.5px;background:var(--glass);border:1px dashed var(--edge);animation:in .25s ease-out}
.toolcard .th{display:flex;align-items:center;gap:6px;color:var(--muted);flex-wrap:wrap}.toolcard .th .ic{color:var(--blue)}
.toolcard .tk{font-weight:700;text-transform:uppercase;letter-spacing:.04em;font-size:10.5px;color:var(--blue)}
.toolcard .st{margin-left:auto;font-size:11px}.toolcard.running .st{color:var(--orange)}.toolcard .st.ok{color:var(--green)}
.toolcard code{display:block;margin:3px 0 1px;font:12.5px ui-monospace,"SF Mono",Menlo,monospace;word-break:break-word}.toolcard .tr{color:var(--muted)}
.card{background:var(--glass-strong);border:1px solid var(--edge);border-radius:18px;padding:14px 16px;box-shadow:0 4px 18px rgba(16,44,52,.1);animation:in .3s ease-out}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;max-width:860px}
.lead .top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.lead h4{margin:0;font-size:16px}.lead small{color:var(--muted);display:block}
.ring{--p:0;flex:none;width:44px;height:44px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;
background:radial-gradient(closest-side,var(--solid) 78%,transparent 80%),conic-gradient(var(--blue) calc(var(--p)*1%),rgba(127,127,127,.2) 0)}
.lead p{margin:8px 0;font-size:14px}.meta{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.pill{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;padding:2px 9px;border-radius:999px;background:var(--glass);border:1px solid var(--edge);color:var(--muted)}
.pill.s-hot,.pill.s-due,.pill.v-take{color:#c93400;background:rgba(255,149,0,.15);border-color:transparent}
.pill.s-overdue{color:var(--red);background:rgba(255,59,48,.13);border-color:transparent}.pill.s-scheduled,.pill.v-warm{color:var(--blue);background:rgba(10,132,255,.12);border-color:transparent}
.pill.s-park,.pill.s-parked,.pill.s-warm,.pill.s-pass{color:var(--gray);background:rgba(142,142,147,.15);border-color:transparent}
@media (prefers-color-scheme:dark){.pill.s-hot,.pill.s-due,.pill.v-take{color:#ffb340}}
.why{font-size:12.5px;color:var(--muted)}.acts{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:999px;font-weight:600;font-size:13px;background:var(--glass);border:1px solid var(--edge);
box-shadow:inset 0 1px 0 rgba(255,255,255,.5)}.btn.primary{background:var(--grad);color:#fff;border-color:transparent}.btn:hover{filter:brightness(1.05)}.btn:disabled{opacity:.45;cursor:default;filter:none}
.profile{max-width:560px}.profile .head{display:flex;gap:12px;align-items:center;margin-bottom:8px}.profile .head b{font-size:17px;display:block}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 12px;font-size:13.5px;margin:0}.kv dt{color:var(--muted)}.kv dd{margin:0}
.bars{display:grid;gap:5px;margin-top:10px}.bar{display:grid;grid-template-columns:130px 1fr 34px;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
.bar span.t{height:6px;border-radius:999px;background:rgba(127,127,127,.18);overflow:hidden}.bar span.t i{display:block;height:100%;background:var(--grad);border-radius:999px}
.rows{max-width:760px;padding:6px 16px}.row{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}.src{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--glass);border:1px solid var(--edge);color:var(--blue);flex:none}
.row b{font-weight:600}.row small{display:block;color:var(--muted);font-size:12.5px}.row .right{text-align:right;font-size:12px;color:var(--muted)}
.draft{white-space:pre-wrap;font-size:14px;background:var(--glass);border:1px solid var(--edge);border-radius:12px;padding:10px 12px;margin:6px 0}
.bottom{padding:0 max(14px,calc(50% - 450px)) 16px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;justify-content:center}
.chip{padding:7px 13px;border-radius:999px;font-size:13px;font-weight:500;background:var(--glass-strong);border:1px solid var(--edge);
-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);display:inline-flex;align-items:center;gap:6px}
.chip.on{background:var(--grad);color:#fff;border-color:transparent}.chip.go{background:var(--text);color:var(--glass-strong);border-color:transparent}
.composer{display:flex;gap:8px;align-items:flex-end;padding:6px;border-radius:24px;transition:box-shadow .2s}
.composer textarea{flex:1;min-width:0;border:0;background:transparent;font:inherit;color:var(--text);padding:8px 12px;outline:none;resize:none;
max-height:168px;line-height:1.45;overflow-y:auto}
.composer.listening{box-shadow:var(--shadow),0 0 0 2px rgba(255,59,48,.55)}
.round.mic.on{background:var(--red);color:#fff;border-color:transparent;animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,59,48,.45)}50%{box-shadow:0 0 0 7px rgba(255,59,48,0)}}
.listen-hint{min-height:18px;margin:6px 4px 0;text-align:center;font-size:12.5px;color:var(--muted)}
.round{width:38px;height:38px;border-radius:50%;border:0;display:grid;place-items:center;flex:none}
.round.send{background:var(--grad);color:#fff}.round.mic{background:var(--glass);border:1px solid var(--edge)}
.corner{position:fixed;top:16px;right:18px;z-index:30;width:40px;height:40px;border-radius:50%;display:grid;place-items:center}
.sheet{width:min(400px,100vw);height:100vh;height:100dvh;max-height:none;margin:0 0 0 auto;padding:22px 22px 30px;border-radius:22px 0 0 22px;border-right:0;
overflow-y:auto;color:var(--text)}
.sheet[open]{animation:slide .22s ease-out}@keyframes slide{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
dialog::backdrop{background:rgba(10,20,22,.25);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px)}
.sheet-head{display:flex;justify-content:space-between;align-items:center}.sheet h2{margin:0;font-size:20px}
.sheet h3{font-size:12.5px;color:var(--muted);font-weight:600;margin:20px 0 6px;text-transform:uppercase;letter-spacing:.04em}
.list{list-style:none;margin:0;padding:0}.list li{display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.list li:first-child{border-top:0}.list li small{display:block;color:var(--muted);font-size:12px}.list li .end{margin-left:auto}
.toggle{width:40px;height:24px;border-radius:999px;border:0;background:rgba(127,127,127,.3);position:relative;transition:background .2s;flex:none}
.toggle:after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:transform .2s}
.toggle.on{background:var(--green)}.toggle.on:after{transform:translateX(16px)}
.add{display:flex;gap:6px;padding:5px;border-radius:999px;background:var(--glass);border:1px solid var(--edge)}
.add input{flex:1;min-width:0;border:0;background:transparent;font:inherit;color:var(--text);padding:6px 10px;outline:none}
.hint{color:var(--muted);font-size:12px;margin:8px 2px 0}.hint.err{color:var(--red)}.rm{background:none;border:0;color:var(--muted);font-size:12.5px}
.small{color:var(--muted);font-size:12.5px}
@media (max-width:620px){.chat{padding:64px 12px 10px}.indent{margin-left:0}.bubble{max-width:88%}.corner{top:10px;right:10px}
.kv{grid-template-columns:90px 1fr}.bar{grid-template-columns:100px 1fr 30px}.chips{justify-content:flex-start;flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px}
.chip{flex:none}.chips.multi{flex-wrap:wrap}.bottom{padding:0 10px 10px}.row{grid-template-columns:auto 1fr}.row .right{grid-column:2;text-align:left}}
</style></head><body>
<div class="app"><div class="chat" id="chat" aria-live="polite"></div>
<div class="bottom"><div class="chips" id="chips"></div>
<form class="composer glass" id="composer"><button type="button" class="round mic" id="mic" aria-label="Talk instead of typing" title="Talk instead of typing"></button>
<textarea id="input" rows="1" autocomplete="off" placeholder="Message your agent" aria-label="Message"></textarea><button class="round send" aria-label="Send" id="send"></button></form>
<div class="listen-hint" id="listen-hint" aria-live="polite"></div></div></div>
<button class="corner glass" id="open-settings" aria-label="Settings"></button>
<dialog class="glass sheet" id="settings"></dialog>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById('data').textContent);
  const $ = (s) => document.querySelector(s);
  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const ic = (n, s) => `<svg class="ic" width="${s || 16}" height="${s || 16}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${D.icons[n] || ''}</svg>`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const chat = $('#chat'), chipsEl = $('#chips'), input = $('#input');
  const HUE = {ava: '#0a84ff', ben: '#30b0c7', cara: '#bf5af2'};
  const SOURCES = ['Gmail', 'LinkedIn', 'Google Calendar', 'WhatsApp', 'Event notes'];
  let S = {};
  try { S = JSON.parse(localStorage.getItem('lt_mvp') || '{}') || {}; } catch (e) { S = {}; }
  S.people = Array.isArray(S.people) ? S.people : [];
  S.answers = S.answers && typeof S.answers === 'object' ? S.answers : {};
  S.memory = S.memory && typeof S.memory === 'object' ? S.memory : {};
  if (S.profile && !(S.profile.weights && Array.isArray(S.profile.sources) && Array.isArray(S.profile.sectors) && Array.isArray(S.profile.stages))) { S.profile = null; S.leads = null; }
  const save = () => { try { localStorage.setItem('lt_mvp', JSON.stringify(S)); } catch (e) {} };
  $('#mic').innerHTML = ic('mic', 18); $('#send').innerHTML = ic('up', 18); $('#open-settings').innerHTML = ic('settings', 18);

  const initials = (n) => String(n).replace(/^@/, '').split(/[\s._-]+/).map((p) => p[0] || '').join('').slice(0, 2).toUpperCase();
  const me = () => { const n = (S.profile && S.profile.first) || String(S.answers.name || '').trim().split(/\s+/)[0]; return n ? `${n}'s agent` : 'Your agent'; };
  const scroll = () => { chat.scrollTop = chat.scrollHeight; };
  function add(html) { chat.insertAdjacentHTML('beforeend', html); scroll(); return chat.lastElementChild; }
  function agentAvatar(who) {
    if (!who) return `<span class="avatar me-agent">${ic('spark', 16)}</span>`;
    const full = (D.team.find((m) => m.id === who.id) || {}).name || who.name;
    return `<span class="avatar" style="--hue:${HUE[who.id] || '#ff9f0a'}">${esc(initials(full))}</span>`;
  }
  function say(html, who) {
    const name = who ? `${who.name}'s agent` : me();
    return add(`<div class="msg">${agentAvatar(who)}<div class="bubble"><b class="who">${esc(name)}</b><div>${html}</div></div></div>`);
  }
  const mine = (text, voice) => add(`<div class="msg me"><div class="bubble">${voice ? `${ic('mic', 13)} ` : ''}${esc(text)}</div></div>`);
  const sys = (text) => add(`<div class="msg sys"><span>${esc(text)}</span></div>`);
  const block = (html) => add(`<div class="indent">${html}</div>`);
  async function typing(ms) {
    const el = add(`<div class="msg typing">${agentAvatar()}<div class="bubble"><i></i><i></i><i></i></div></div>`);
    await sleep(ms || 650); el.remove();
  }
  async function tool(call, result, opts) {
    opts = opts || {};
    const icon = opts.kind === 'akta' ? 'globe' : opts.kind === 'mem' ? 'db' : opts.kind === 'agent' ? 'agent' : 'tool';
    const head = esc(opts.caller || me()) + (opts.target ? ` &rarr; ${esc(opts.target)}` : '');
    const el = block(`<div class="toolcard running"><div class="th">${ic(icon, 14)}<span class="tk">Tool call</span><span>${head}</span><span class="st">running</span></div><code>${esc(call)}</code><div class="tr"></div></div>`);
    await sleep(opts.wait || 520);
    const card = el.querySelector('.toolcard'); card.classList.remove('running');
    const st = card.querySelector('.st'); st.textContent = 'done'; st.classList.add('ok');
    card.querySelector('.tr').textContent = result; scroll();
  }
  async function post(url, body) {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  function setChips(list, cls) {
    chipsEl.className = 'chips ' + (cls || '');
    chipsEl.innerHTML = list.map((c, i) => `<button type="button" class="chip ${c.cls || ''}" data-i="${i}">${c.icon ? ic(c.icon, 14) : ''}${esc(c.label)}</button>`).join('');
    chipsEl.onclick = (ev) => { const b = ev.target.closest('.chip'); if (b && !busy) list[+b.dataset.i].act(b); };
    scroll(); requestAnimationFrame(scroll);
  }
  let busy = false, pending = null;
  async function run(fn) { if (busy) return; busy = true; try { await fn(); } catch (e) { sys('Something went wrong. Try again.'); } finally { busy = false; } }

  // ---------------------------------------------------------------- interview
  const Q = D.mvp.interview;
  let step = -1;
  async function startInterview() {
    S.answers = {}; S.profile = null; S.leads = null; save();
    chat.innerHTML = ''; step = -1;
    sys(`${D.fund.name} · Build your personal agent`);
    await next();
  }
  async function next() {
    step += 1;
    const q = Q[step];
    await typing(500);
    say(esc(q.q.replace('{name}', S.answers.name || '')));
    input.placeholder = q.placeholder || 'Type an answer or pick one';
    if (q.multi) {
      const picked = new Set(SOURCES);
      const draw = () => setChips(q.chips.map((c) => ({label: c, icon: c, cls: picked.has(c) ? 'on' : '',
        act: () => { picked.has(c) ? picked.delete(c) : picked.add(c); draw(); }}))
        .concat([{label: 'Connect', icon: 'check', cls: 'go', act: () => run(() => answer(SOURCES.filter((s) => picked.has(s)).join(', ') || 'Gmail'))}]), 'multi');
      draw();
    } else {
      setChips(q.chips.map((c) => ({label: c, act: () => run(() => answer(c))})));
    }
    if (!q.chips.length) input.focus();
  }
  async function answer(text, voice) {
    const q = Q[step];
    mine(text, voice); setChips([]);
    if (q.multi) {
      const t = text.toLowerCase();
      const picked = SOURCES.filter((s) => t.includes(s.toLowerCase()) || t.includes(s.toLowerCase().split(' ').pop()) || (s === 'Gmail' && /e-?mail/.test(t)));
      S.answers[q.key] = picked.length ? picked : SOURCES.slice();
    } else {
      S.answers[q.key] = text;
    }
    save();
    if (step < Q.length - 1) await next(); else await build();
  }
  async function build() {
    input.placeholder = 'Message your agent';
    await typing(500);
    say('Thanks. Give me a few seconds to build myself around how you work.');
    const src = S.answers.sources || [];
    await tool(`connect(${src.join(', ')})`, `${src.length} ${src.length === 1 ? 'platform' : 'platforms'} connected (demo data)`);
    await tool('read_history(last_90_days)', '214 founder threads, 38 meetings, 12 event notes');
    let r;
    try { r = await post('/api/build', S.answers); } catch (e) {
      say('I could not reach the server to finish building. Tap Try again.');
      setChips([{label: 'Try again', icon: 'check', cls: 'on', act: () => run(build)}]);
      return;
    }
    S.profile = r.profile; S.leads = r.leads; save();
    await tool(`learn_thesis("${r.profile.focus}")`, r.profile.weights_text);
    await tool(`akta.pro industry_map("${r.profile.focus}")`, r.profile.akta, {kind: 'akta'});
    await tool('register_agent(firm: Northbeam)', 'Joined the firm network: Ava, Ben and Cara’s agents can reach me', {kind: 'mem'});
    profileCard();
    await typing(450);
    say(`I'm ready, ${esc(S.profile.first)}. I source with your criteria and talk to your teammates' agents so nobody drops a founder. Where should we start?`);
    mainChips();
  }
  function profileCard() {
    const p = S.profile;
    const bars = Object.entries(p.weights).filter(([, v]) => v).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<div class="bar"><span>${esc(D.labels[k] || k.replace('_', ' '))}</span><span class="t"><i style="width:${v * 2.5}%"></i></span><span>${v}%</span></div>`).join('');
    block(`<div class="card profile"><div class="head">${agentAvatar()}<div><b>${esc(p.first)}'s agent</b><span class="small">Built from your interview</span></div></div>
      <dl class="kv"><dt>Role</dt><dd>${esc(p.role)}</dd><dt>Focus</dt><dd>${esc(p.sectors.join(', '))} · ${esc(p.stages.join(', '))}</dd>
      <dt>Trusts</dt><dd>${esc(p.signal || 'A balanced read')}</dd><dt>Listens on</dt><dd>${esc(p.sources.join(', '))}</dd><dt>Filters out</dt><dd>${esc(p.nos)}</dd></dl>
      <div class="bars">${bars}</div></div>`);
  }
  function mainChips() { setChips(mainList()); }
  function mainList() {
    return ([
      {label: 'Source founders', icon: 'spark', act: () => run(() => ask('Source founders for me'))},
      {label: 'Who have I been talking to?', icon: 'Gmail', act: () => run(() => ask('Who have I been talking to?'))},
      {label: 'Record a note', icon: 'mic', act: () => run(startNote)},
      {label: 'What is my team working on?', icon: 'agent', act: () => run(() => ask('What is my team working on?'))},
    ]);
  }

  // ---------------------------------------------------------------- skills
  async function leads() {
    if (!S.leads) { const r = await post('/api/build', S.answers || {}); S.profile = r.profile; S.leads = r.leads; save(); }
    return S.leads;
  }
  const words = (t) => String(t || '').toLowerCase().split(/[^a-z0-9\u00e0-\u00ff]+/).filter(Boolean);
  const find = (q) => {
    const w = new Set(words(q));
    return (S.leads || []).find((l) => [l.company, l.founder].some((x) => x && (q.includes(x.toLowerCase()) || words(x).some((t) => t.length > 3 && w.has(t)))));
  };
  // A note reads like "Just met …" or "Okay so I talked to …" (a few filler words allowed); questions are never notes.
  const NOTE_VERB = /^(?:[\w'\u2019]+[\s,]+){0,4}?(?:i\s+)?(?:just\s+)?(?:met|talked (?:to|with)|spoke (?:to|with)|chatted with|ran into|caught up with|coffee with|had (?:a )?(?:call|coffee|lunch|dinner|chat) with)\b/i;
  const isNote = (t) => /^note\s*[:,-]/i.test(t) || (!/\?\s*$/.test(t)
    && !/^(?:who|what|when|where|why|how|which|did|do|does|have|has|can|could|should|would|is|are|tell|show|find|source|ask|draft)\b/i.test(t) && NOTE_VERB.test(t));
  async function ask(text, voice) {
    mine(text, voice);
    const q = text.toLowerCase();
    const p = pending; pending = null; mainChips();
    if (p && /^(yes|yep|yeah|sure|ok|okay|please|go ahead|do it|approve)\b|draft (the )?(\d+ )?repl|approve/.test(q)) return p();
    if (noteMode || isNote(text.trim())) { noteMode = false; return processNote(text); }
    if (/^(?:record|take|add|make|start)\s+(?:a\s+)?(?:voice\s+)?note\b/.test(q) && q.split(/\s+/).length <= 5) return startNote();
    await leads();
    const l = find(q);
    if (l && /draft|follow|reply|write/.test(q)) return draft(l);
    if (l && /team|who knows|ask/.test(q)) return askTeam(l, true);
    if (l) return detail(l);
    if (/source|find|new founder|deal|pipeline/.test(q)) return sourcing();
    if (/talking|talk to|contacts|conversation|who have i/.test(q)) return contacts();
    if (/team|working|firm|everyone|board/.test(q)) return team();
    const mate = D.team.find((m) => words(q).includes(m.name.split(' ')[0].toLowerCase()));
    if (mate) return teammate(mate);
    await typing(400);
    say('I can source founders, show who you have been talking to, record a note about someone you just met, or check what your team is working on. You can also ask about a company, like Patchbay or Quillstack. Tap the mic to talk instead of typing.');
  }
  async function sourcing() {
    await typing(400);
    say('On it. Scanning your connected sources and the firm inbox.');
    const all = await leads();
    await tool(`scan_inbox(${(S.profile.sources || []).join(', ')})`, '14 new messages: 9 founders kept, 5 dropped (vendor, token spam, webinar promo, local bakery, copy-paste pitch)');
    const top = all.filter((l) => l.verdict !== 'Outside your thesis' && l.verdict !== 'Pass').slice(0, 4);
    if (!top.length) {
      await typing(300);
      say(`Nothing in ${esc(S.profile.sectors.join(', '))} came in today. Here are the closest founders on your other criteria:`);
      block(`<div class="cards">${all.slice(0, 3).map(leadCard).join('')}</div>`);
      return;
    }
    for (const l of top) await tool(`akta.pro search("${l.company}")`, l.akta, {kind: 'akta', wait: 380});
    await tool(`score_with_my_thesis(${top.length} founders)`, top.map((l) => `${l.company} ${l.score}`).join(' · '));
    await typing(350);
    say(`Here is who is worth your time today, ranked by your own criteria. ${all.length - top.length} more are below your bar or outside your thesis.`);
    block(`<div class="cards">${top.map(leadCard).join('')}</div>`);
    const known = top.filter((l) => l.known).sort((a, b) => a.known.rank - b.known.rank);
    if (known.length) {
      await typing(400);
      const n = Math.min(2, known.length);
      say(`${known.length} of these ${known.length === 1 ? 'is' : 'are'} already in motion at the firm. Checking ${n === 1 ? 'it' : 'the two most urgent'} with your teammates' agents so nobody double-contacts a founder.`);
      for (const l of known.slice(0, n)) await askTeam(l, false);
    }
  }
  function leadCard(l) {
    const v = l.verdict === 'Take the first call' ? 'v-take' : l.verdict === 'Keep warm' ? 'v-warm' : l.verdict === 'Parked' ? 's-park' : '';
    return `<div class="card lead"><div class="top"><div><h4>${esc(l.company)}</h4><small>${esc(l.founder)} · via ${esc(l.channel)}</small></div>
      <div class="ring" style="--p:${l.score}" title="Score on your thesis">${l.score}</div></div><p>${esc(l.one_liner)}</p>
      <div class="meta"><span class="pill ${v}">${esc(l.verdict)}</span><span class="pill">${esc(l.sector)} · ${esc(l.stage)}</span>${l.known ? `<span class="pill s-scheduled">Known to ${esc(l.known.name)}</span>` : ''}</div>
      <div class="why">Why: ${esc(l.why)}</div>
      <div class="acts"><button class="btn primary" data-act="draft" data-id="${esc(l.id)}">${ic('send', 13)}Draft follow-up</button><button class="btn" data-act="team" data-id="${esc(l.id)}">${ic('agent', 13)}Ask the team</button></div></div>`;
  }
  chat.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-act]'); if (!b || busy) return;
    const l = (S.leads || []).find((x) => x.id === b.dataset.id); if (!l) return;
    run(() => ask(b.dataset.act === 'draft' ? `Draft a follow-up to ${l.company}` : `Ask the team about ${l.company}`));
  });
  async function draft(l) {
    await tool(`draft_followup("${l.company}", voice: ${S.profile.first})`, 'Draft ready (not sent)');
    say(`Here is a draft for ${esc(l.founder)}. I never send anything without you.<div class="draft">${esc(l.draft)}</div>`);
  }
  async function detail(l) {
    await tool(`akta.pro search("${l.company}")`, l.akta, {kind: 'akta'});
    await tool(`read_firm_memory("${l.company}")`, l.known ? `known to ${l.known.name}` : 'no prior touches', {kind: 'mem'});
    say(`<b>${esc(l.company)}</b> · ${esc(l.one_liner)}<br>Team: ${esc(l.founder)} (${esc(l.background)})<br>Traction: ${esc(l.traction)}${l.raise ? `<br>Raising: ${esc(l.raise)}` : ''}<br>Score on your criteria: ${l.score}/100 (${esc(l.verdict)}). Top factors: ${esc(l.why)}.`);
    if (l.known) await askTeam(l, false);
  }
  async function askTeam(l, direct) {
    if (!l.known) {
      await tool(`who_knows("${l.founder}")`, 'Ava, Ben and Cara’s agents: no prior contact', {kind: 'agent', target: 'all team agents'});
      S.memory[l.company] = {founder: l.founder, next: 'You own first outreach'}; save();
      say(`Nobody at the firm has talked to ${esc(l.founder)} yet, so it's yours. I noted you as the owner in firm memory.`);
      return;
    }
    const who = {id: l.known.id, name: l.known.name};
    await tool(`check_relationship("${l.founder}", ${l.company})`, 'reply received', {kind: 'agent', target: `${who.name}'s agent`});
    await typing(450);
    say(esc(l.known.text), who);
    await tool(`update_firm_memory("${l.company}")`, `next step: ${l.known.next}`, {kind: 'mem'});
    S.memory[l.company] = {founder: l.founder, next: l.known.next}; save();
    if (direct) say(`Got it. Next step: ${esc(l.known.next)}`);
  }
  async function contacts() {
    const src = S.profile ? S.profile.sources : SOURCES;
    if (!src.length) { await typing(300); say('No platforms are connected yet. Turn one on in Settings and ask me again.'); return; }
    await tool(`sync(${src.join(', ')})`, 'synced (demo data)');
    const rows = D.mvp.conversations.filter((c) => src.includes(c.channel));
    if (!rows.length) { await typing(300); say('None of your connected platforms have founder conversations yet. Connect Gmail or LinkedIn in Settings.'); return; }
    const hidden = D.mvp.conversations.length - rows.length;
    await typing(350);
    say(`You have ${rows.length} active founder conversations${hidden ? ` (connect more platforms in Settings to see ${hidden} more)` : ''}. Here is who, where, and what's next:`);
    block(`<div class="card rows">${rows.map((c) => `<div class="row"><span class="src" title="${esc(c.channel)}">${ic(c.channel, 16)}</span>
      <div><b>${esc(c.founder)}</b> · ${esc(c.company)}<small>${esc(c.summary)}</small><small>Next: ${esc(c.next)}</small></div>
      <div class="right"><span class="pill s-${esc(c.status)}">${esc(c.due)}</span><br>${esc(c.channel)} · ${esc(c.when)}</div></div>`).join('')}</div>`);
    const urgent = rows.filter((c) => c.status === 'due' || c.status === 'overdue');
    if (urgent.length) {
      await typing(350);
      say(`${urgent.length} need you now: ${urgent.map((c) => `${esc(c.founder)} (${esc(c.due.replace(/^Due /, 'due '))})`).join(', ')}. Want me to draft the replies?`);
      pending = () => draftReplies(urgent);
      setChips([{label: `Draft ${urgent.length === 1 ? 'the reply' : `${urgent.length} replies`}`, icon: 'send', cls: 'on', act: () => run(() => ask('Yes, draft the replies'))}].concat(mainList()));
    }
  }
  async function draftReplies(rows) {
    const out = [];
    for (const c of rows) {
      const l = (S.leads || []).find((x) => x.company === c.company);
      await tool(`draft_reply("${c.founder}", ${c.company})`, 'Draft ready (not sent)', {wait: 360});
      out.push(`<b>${esc(c.founder)}</b> · ${esc(c.company)}<div class="draft">${esc(l ? l.draft : `Hi ${c.founder.split(' ')[0]}, thanks for your note about ${c.company}. ${c.next}. Best, ${S.profile.first}`)}</div>`);
    }
    say(`Here ${rows.length === 1 ? 'is the draft' : `are ${rows.length} drafts`}. Nothing is sent without you.<br>${out.join('')}`);
  }
  async function team() {
    for (const m of D.team) {
      const theirs = D.board.filter((r) => r.who === m.id);
      await tool('list_relationships()', `${theirs.length} founders, ${theirs.filter((r) => r.status === 'hot').length} hot`, {kind: 'agent', target: `${m.name.split(' ')[0]}'s agent`, wait: 380});
    }
    for (const u of S.people) await tool('list_relationships()', 'no founders yet (interview pending)', {kind: 'agent', target: `@${u}'s agent`, wait: 300});
    await typing(350);
    say('Here is who at the firm is talking to which founder, and what happens next:');
    const yours = Object.entries(S.memory || {}).map(([company, m]) => `<div class="row">${agentAvatar()}
      <div><b>${esc(S.profile.first)}</b> · ${esc([m.founder, company].filter(Boolean).join(', '))}<small>${esc(m.next)}</small></div>
      <div class="right"><span class="pill s-due">You</span><br>From your agent</div></div>`).join('');
    block(`<div class="card rows">${yours}${D.board.map((r) => `<div class="row">${agentAvatar({id: r.who, name: r.name})}
      <div><b>${esc(r.name.split(' ')[0])}</b> · ${esc(r.founder)}, ${esc(r.company)}<small>${esc(r.what)}</small></div>
      <div class="right"><span class="pill s-${esc(r.status)}">${esc({hot: 'Hot', park: 'Parked', warm: 'Warm', pass: 'Pass'}[r.status] || r.status)}</span><br>${esc(r.due)}</div></div>`).join('')}</div>`);
    await typing(350);
    if (D.handoff) say(esc(D.handoff));
  }

  async function teammate(m) {
    const f = m.name.split(' ')[0];
    const rows = D.board.filter((r) => r.who === m.id);
    await tool('list_relationships()', `${rows.length} founders`, {kind: 'agent', target: `${f}'s agent`});
    await typing(350);
    say(rows.length ? `${f} has ${rows.length} founders in motion: ${rows.map((r) => `${esc(r.company)} (${esc(r.what.toLowerCase())}, ${esc(r.due)})`).join('; ')}.` : `${f} has no founders in motion right now.`, {id: m.id, name: f});
  }

  // ---------------------------------------------------------------- notes
  let noteMode = false;
  async function startNote() {
    noteMode = true;
    await typing(250);
    say(`Go ahead: who did you meet, what are they building, and what's next? ${SR ? 'I\'m listening. Tap send when you\'re done.' : 'Type it below and send.'}`);
    if (SR && !listening) startDictation(); else input.focus();
  }
  async function processNote(text) {
    let r;
    try { r = await post('/api/note', {text}); } catch (e) { say('I could not read that note right now. Please try again.'); return; }
    const x = r.note, who = x.founder || x.company;
    await tool('extract(founder, company, next_step)', `${x.founder || 'no name heard'} · ${x.company || 'no company heard'} · ${r.next} (${r.how})`);
    await tool(`save_note("${who || 'untitled'}")`, r.saved === 'roundtable' ? 'saved to Roundtable firm memory, folder last-touch'
      : r.saved === 'failed' ? 'Roundtable save failed; kept in this session' : 'saved to firm memory (demo: Roundtable not connected)', {kind: 'mem'});
    if (!who) { say('Saved, but I could not tell who you met. Next time say their name and company, like "Just met Tracy Huang from Interlink IQ."'); return; }
    const k = r.known;
    await tool(`check_relationship("${who}")`, k.id ? 'reply received' : 'no prior contact', {kind: 'agent', target: k.id ? `${k.name}'s agent` : 'all team agents'});
    await typing(450);
    if (k.id) say(esc(k.text), {id: k.id, name: k.name}); else say(esc(k.text));
    await tool(`schedule_followup("${who}", ${r.due})`, `reminder set: ${r.next}`, {kind: 'mem'});
    S.memory[x.company || who] = {founder: x.founder || '', next: r.next}; save();
    say(`Saved.${r.facts.length ? ' ' + r.facts.map(esc).join(' · ') + '.' : ''}<br>Next step: ${esc(r.next)} (${esc(r.due)}). Nothing was sent without you.`);
    if (k.intro) {
      pending = async () => {
        await tool(`queue_intro("${who}", ${k.name}, Monday 10:00)`, 'intro email queued for your final review (demo data)', {kind: 'agent', target: `${k.name}'s agent`});
        say(`Done. The intro between ${esc(who)} and ${esc(k.name)} is queued with the Monday 10:00 slot. You review the final email before it goes out.`);
      };
      setChips([{label: `Approve intro to ${k.name}`, icon: 'check', cls: 'on', act: () => run(() => ask('Approve the intro'))}].concat(mainList()));
    }
  }

  // ---------------------------------------------------------------- settings
  function renderSettings() {
    const p = S.profile, dlg = $('#settings');
    const src = (p && p.sources) || [];
    dlg.innerHTML = `<div class="sheet-head"><h2>Settings</h2><button class="round mic" id="close-settings" aria-label="Close">${ic('x', 16)}</button></div>
      <h3>Your agent</h3>${p ? `<ul class="list"><li>${agentAvatar()}<span><b>${esc(p.first)}'s agent</b><small>${esc(p.sectors.join(', '))} · ${esc(p.stages.join(', '))}</small></span></li></ul>
      <button class="btn" id="redo">Redo the interview</button>` : '<p class="small">Finish the interview in the chat to build your agent.</p>'}
      <h3>Team agents</h3><ul class="list">${D.team.map((m) => `<li>${agentAvatar({id: m.id, name: m.name})}<span><b>${esc(m.name)}</b><small>${esc(m.role)} · agent ready</small></span></li>`).join('')}
      ${S.people.map((u, i) => `<li>${agentAvatar({id: 'x', name: u})}<span><b>@${esc(u)}</b><small>Invited · interview pending</small></span><button class="rm end" data-rm="${i}">Remove</button></li>`).join('')}</ul>
      <h3>Add a person</h3><form class="add" id="add-person"><input id="username" autocomplete="off" placeholder="Username, e.g. @dana" aria-label="Username"><button class="btn primary">${ic('userplus', 14)}Add</button></form>
      <p class="hint" id="add-hint">They get their own agent after a quick interview.</p>
      <h3>Connected platforms</h3><ul class="list">${SOURCES.map((s) => `<li><span class="src">${ic(s, 16)}</span><span>${esc(s)}<small>${src.includes(s) ? 'Connected (demo data)' : 'Not connected'}</small></span>
        <button class="toggle end ${src.includes(s) ? 'on' : ''}" data-src="${esc(s)}" aria-label="Toggle ${esc(s)}" ${p ? '' : 'disabled'}></button></li>`).join('')}</ul>
      <h3>Demo</h3><p class="small">All founders, messages and platforms are mock data.</p><button class="btn" id="reset">Reset demo</button>`;
    $('#close-settings').onclick = () => dlg.close();
    const redo = $('#redo'); if (redo) redo.onclick = () => { S.profile = null; S.leads = null; S.answers = {}; save(); location.reload(); };
    $('#reset').onclick = () => { try { localStorage.removeItem('lt_mvp'); } catch (e) {} location.reload(); };
    dlg.querySelectorAll('[data-src]').forEach((b) => { b.onclick = () => {
      const s = b.dataset.src, list = S.profile.sources;
      const i = list.indexOf(s); if (i >= 0) list.splice(i, 1); else list.push(s);
      S.answers.sources = list; save(); renderSettings();
    }; });
    dlg.querySelectorAll('[data-rm]').forEach((b) => { b.onclick = () => { const [u] = S.people.splice(+b.dataset.rm, 1); save(); renderSettings(); sys(`@${u} was removed`); }; });
    $('#add-person').onsubmit = (ev) => {
      ev.preventDefault();
      const u = $('#username').value.trim().replace(/^@/, ''), hint = $('#add-hint');
      if (!/^[A-Za-z0-9_.-]{2,30}$/.test(u)) { hint.textContent = 'Use 2 to 30 letters, numbers, dots, dashes or underscores.'; hint.className = 'hint err'; return; }
      if (S.people.some((x) => x.toLowerCase() === u.toLowerCase())) { hint.textContent = `@${u} is already on the team.`; hint.className = 'hint err'; return; }
      S.people.push(u); save(); renderSettings();
      $('#add-hint').textContent = `Invited @${u}. Their agent interviews them, then joins the firm network.`;
      sys(`@${u} was invited. Their agent joins after a quick interview.`);
    };
  }
  $('#open-settings').onclick = () => { renderSettings(); $('#settings').showModal(); };

  // ---------------------------------------------------------------- composer and voice input
  // Dictation fills the message box live and has no time limit: browsers end recognition after a pause, so it restarts
  // until you tap the mic again or send. Works for any message, including the interview.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, listening = false, baseText = '', finalText = '', dictated = false;
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 168) + 'px'; };
  function micUI() {
    $('#mic').classList.toggle('on', listening); $('#composer').classList.toggle('listening', listening);
    $('#mic').innerHTML = ic(listening ? 'stop' : 'mic', 18);
    $('#mic').setAttribute('aria-label', listening ? 'Stop listening' : 'Talk instead of typing');
    $('#listen-hint').textContent = listening ? 'Listening. Talk as long as you like, then tap send, or tap the mic to stop.' : '';
  }
  function startDictation() {
    if (!SR) { sys('Voice input needs Chrome, Edge or Safari. You can type instead.'); input.focus(); return; }
    if (rec) discardDictation();
    listening = true; dictated = true; finalText = '';
    baseText = input.value.trim() ? input.value.replace(/\s*$/, ' ') : '';
    // Each session writes to the box only while it is the current one, so words that arrive after a send never come back.
    const me = new SR(); rec = me;
    me.continuous = true; me.interimResults = true; me.lang = navigator.language || 'en-US';
    me.onresult = (e) => {
      if (rec !== me) return;
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) { const sofar = baseText + finalText; finalText += (sofar && !/\s$/.test(sofar) ? ' ' : '') + r[0].transcript.trim(); }
        else interim += r[0].transcript;
      }
      input.value = (baseText + finalText + (interim ? ' ' + interim.trim() : '')).replace(/^\s+/, '');
      grow(); input.scrollTop = input.scrollHeight;
    };
    me.onerror = (e) => {
      if (rec !== me) return;
      if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(e.error)) {
        stopDictation();
        sys(e.error === 'audio-capture' ? 'No microphone found. You can type instead.' : 'Microphone access is blocked. Allow it in your browser, or type instead.');
      } else if (e.error === 'network') { stopDictation(); sys('The speech service is unreachable right now. You can type instead.'); }
    };
    me.onend = () => {
      if (rec !== me) return;
      if (!listening) { rec = null; return; }  // stopped with the mic: the last words have landed, the session is over
      baseText = (baseText + finalText).replace(/\s*$/, ' '); finalText = '';
      try { me.start(); } catch (e) { setTimeout(() => { if (listening && rec === me) try { me.start(); } catch (x) { stopDictation(); } }, 250); }
    };
    try { me.start(); } catch (e) { listening = false; rec = null; }
    micUI(); input.focus();
  }
  function stopDictation() {
    // Tapping the mic: stop listening but let words already spoken finish landing in the box.
    listening = false;
    if (rec) { try { rec.stop(); } catch (e) {} }
    micUI();
  }
  function discardDictation() {
    // Sending: end the session now and drop anything still in flight, so it can't refill the box or join the next message.
    listening = false;
    const r = rec; rec = null; baseText = ''; finalText = '';
    if (r) { r.onresult = r.onerror = r.onend = null; try { r.abort(); } catch (e) { try { r.stop(); } catch (x) {} } }
    micUI();
  }
  $('#mic').onclick = () => (listening ? stopDictation() : startDictation());
  input.addEventListener('input', () => { grow(); if (listening) { baseText = input.value.replace(/\s*$/, ' '); finalText = ''; } });
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); $('#composer').requestSubmit(); } });
  $('#composer').addEventListener('submit', (ev) => {
    ev.preventDefault();
    if (busy) return;
    const voice = dictated;
    discardDictation(); dictated = false;
    const text = input.value.trim(); if (!text) return;
    input.value = ''; grow();
    if (S.profile) run(() => ask(text, voice));
    else if (step >= Q.length - 1 && S.answers[Q[Q.length - 1].key] != null) run(build);
    else if (step >= 0) run(() => answer(text, voice));
  });

  // ---------------------------------------------------------------- start
  if (S.profile) {
    try {
      sys(`${D.fund.name} · ${S.profile.first}'s agent`);
      profileCard();
      say(`Welcome back, ${esc(S.profile.first)}. What should we work on?`);
      mainChips();
    } catch (e) {
      S = {people: S.people, answers: {}, memory: {}}; save(); chat.innerHTML = ''; run(startInterview);
    }
  } else {
    run(startInterview);
  }
})();
</script></body></html>"""


if __name__ == "__main__":
    app.run(debug=True, port=8000)
