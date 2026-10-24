"""Last Touch (Track 1, sourcing). Flask app for Vercel (exports `app`).

Every person at the firm signs in with a name and a username and gets a personal agent. After an event or a meeting
they record a quick note (dictated or typed). The agent reads each note for the founder, company, where they met and
the next step, keeps track of who they have been talking to and what's overdue, ranks founders with their own criteria,
and shares it all with their team through the firm memory on Vercel Blob. Recorded notes are the only data source.
Every write goes through guard.py: Ask mode asks the user, Auto mode lets PRISM decide.

Run locally: flask --app app run
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import datetime as dt  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import time  # noqa: E402
from functools import wraps  # noqa: E402
from pathlib import Path  # noqa: E402

from flask import Flask, g, jsonify, request, session  # noqa: E402
from flask.sessions import SecureCookieSessionInterface  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import engine  # noqa: E402
import guard  # noqa: E402
import integrations as ix  # noqa: E402
import store  # noqa: E402
from env import env, scrub  # noqa: E402

app = Flask(__name__)


class SharedKeySessions(SecureCookieSessionInterface):
    """Sessions must verify on every serverless instance, so the signing key must be the same everywhere: SECRET_KEY,
    or else one random key kept in the store and shared by all instances. Resolved on first use (inside a request, so
    Vercel's OIDC credentials are available)."""

    def get_signing_serializer(self, app):
        if not app.secret_key:
            app.secret_key = env("SECRET_KEY") or store.shared_secret()
        return super().get_signing_serializer(app)


app.session_interface = SharedKeySessions()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))

USER_RE = re.compile(r"^[a-z0-9_.-]{2,30}$")
MIN_PASSWORD = 8
MAX_FAILS, LOCK_MINUTES = 8, 15
INTERVIEW = [
    {"key": "role", "q": "Hi {name}, I'm your new agent. After every event or meeting, record a quick note and I'll keep track of "
                         "who you met and what's next. First, what's your role?",
     "chips": ["Associate: sourcing and first calls", "Partner: leading deals", "Talent partner: founders and hiring"]},
    {"key": "focus", "q": "Which sectors and stages do you invest in?",
     "chips": ["AI and devtools, pre-seed to seed", "Consumer and CPG, seed", "Fintech, seed to Series A"]},
    {"key": "signal", "q": "What makes you take a first call? Tell me the signal you trust most.",
     "chips": ["Technical founders with open-source traction", "Real revenue and repeat purchase", "A warm intro from someone I trust"]},
    {"key": "nos", "q": "Last one. Any hard no's I should flag for you?",
     "chips": ["Token or crypto raises", "Agencies and services businesses", "No hard no's"]},
]
ALL_SECTORS = ["ai", "devtools", "fintech", "consumer", "cpg", "climate", "health"]
WEIGHTS = {
    "technical": {"thesis": 30, "traction": 10, "founder_fit": 20, "mutual": 5, "capital_efficiency": 0, "technical_depth": 35},
    "revenue": {"thesis": 30, "traction": 30, "founder_fit": 15, "mutual": 10, "capital_efficiency": 15, "technical_depth": 0},
    "warm": {"thesis": 30, "traction": 15, "founder_fit": 15, "mutual": 30, "capital_efficiency": 5, "technical_depth": 5},
    "balanced": {"thesis": 30, "traction": 20, "founder_fit": 20, "mutual": 10, "capital_efficiency": 10, "technical_depth": 10},
}
LABEL = {"thesis": "thesis fit", "traction": "traction", "founder_fit": "founder fit", "mutual": "warm intro",
         "capital_efficiency": "capital efficiency", "technical_depth": "technical depth"}


def first(name: str | None) -> str:
    return (name or "").split()[0] if name else ""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"


def _ts(iso: str) -> dt.datetime:
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.fromtimestamp(0, dt.timezone.utc)
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def nice_date(iso: str) -> str:
    return _ts(iso).strftime("%a %b %d").replace(" 0", " ")


# --------------------------------------------------------------------------- users
def norm(u) -> str:
    return str(u or "").strip().lstrip("@").lower()


def load_user(u: str) -> dict | None:
    return store.get(f"users/{u}.json") if USER_RE.match(u or "") else None


def save_user(user: dict):
    store.put(f"users/{user['username']}.json", user)


def mode_of(user: dict) -> str:
    m = (user.get("settings") or {}).get("mode", "ask")
    return "auto" if m == "auto" and guard.live() else "ask"


def public(user: dict) -> dict:
    return {"username": user["username"], "name": user["name"], "first": first(user["name"]), "profile": user.get("profile"),
            "team": user.get("team", []), "mode": mode_of(user)}


def current() -> dict | None:
    u = session.get("u")
    if not u:
        g.signout = None
        return None
    user = load_user(u)
    # A password change bumps pw_ver, which signs out every other device. Only a newer pw_ver than the session's signs
    # out: an older one means the read was stale, never that the password changed.
    if not user or user.get("pw_ver", 0) > session.get("pv", 0):
        g.signout = "password_changed" if user else "account_missing"
        print(f"signed out @{u}: {g.signout} (store: {store.mode()})", flush=True)
        session.clear()
        return None
    return user


def start_session(user: dict):
    u = user["username"]
    session.clear()
    session.permanent = True
    session["u"] = u
    session["pv"] = user.get("pw_ver", 0)
    session["sid"] = f"lt-{u}-{secrets.token_hex(6)}"  # one PRISM session per sign-in, so traces group into a conversation


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current()
        if not user:
            return jsonify({"error": "signin", "reason": g.get("signout")}), 401
        if not session.get("sid"):
            session["sid"] = f"lt-{user['username']}-{secrets.token_hex(6)}"
        return fn(user, *a, **kw)
    return wrapper


# --------------------------------------------------------------------------- PRISM tracing (Auto mode only)
def agent_of(user: dict) -> tuple[str, str]:
    return f"{first(user['name'])}'s agent", f"last-touch-{user['username']}"


def traced(user: dict) -> bool:
    return mode_of(user) == "auto"


def trace_tool(user: dict, tool: str, inp: str, out: str, t0: float, status: str = "success"):
    if traced(user):
        name, aid = agent_of(user)
        ix.TRACER.tool(name, aid, session.get("sid") or aid, tool, inp, out, int((time.time() - t0) * 1000), status)


@app.teardown_request
def _flush_traces(exc):
    ix.TRACER.flush()


# --------------------------------------------------------------------------- profile
def build_profile(a: dict, name: str) -> dict:
    focus = str(a.get("focus") or "")
    fl = focus.lower()
    sectors = []
    if re.search(r"\bai\b", fl) or any(k in fl for k in ("devtool", "dev tool", "infra", "developer")):
        sectors += ["ai", "devtools"]
    if any(k in fl for k in ("consumer", "cpg", "brand", "food", "retail")):
        sectors += ["consumer", "cpg"]
    if any(k in fl for k in ("fintech", "payments", "finance")):
        sectors.append("fintech")
    for k in ("climate", "health"):
        if k in fl:
            sectors.append(k)
    sectors = sectors or list(ALL_SECTORS)
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
    w = WEIGHTS[style]
    return {
        "name": name, "first": first(name), "role": str(a.get("role") or "Investor")[:80], "focus": focus[:80] or "Generalist",
        "sectors": sectors, "stages": stages, "signal": str(a.get("signal") or "")[:100], "style": style, "weights": w,
        "nos": str(a.get("nos") or "No hard no's")[:80],
        "weights_text": ", ".join(f"{LABEL[k]} {v}%" for k, v in sorted(w.items(), key=lambda kv: -kv[1]) if v),
    }


# --------------------------------------------------------------------------- notes -> founders
def extract(user: dict, text: str, when: dt.date) -> tuple[dict, str]:
    """Read a note for founder, company, event and next step. Nicrron when configured, keyword rules otherwise."""
    if ix.LLM_CLIENT.live:
        name, aid = agent_of(user)
        got = ix.LLM_CLIENT.json(engine.NOTE_SYSTEM.format(today=when.isoformat(), weekday=when.strftime("%A")), text[:4000],
                                 name, session_id=session.get("sid"), trace=traced(user), agent_id=aid)
        if got:
            return engine.clean_note(got), "Nicrron"
    return engine.clean_note(engine.heuristic_note(text, when)), "keyword rules"


def notes_of(u: str, limit: int = 200) -> list[dict]:
    """A user's notes, newest first."""
    paths = store.list_paths(f"notes/{u}/")[-limit:]
    return [n for n in (store.get(p) for p in reversed(paths)) if n]


def _key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def founders_from(notes: list[dict]) -> list[dict]:
    """Merge notes about the same founder (or company) into one record. Later notes update earlier ones."""
    recs, index = [], {}
    for n in sorted(notes, key=lambda n: n.get("created_at", "")):
        x = n.get("extracted") or engine.clean_note(engine.heuristic_note(n.get("text", ""), _ts(n.get("created_at")).date()))
        keys = [k for k in (_key(x.get("founder")), _key(x.get("company"))) if k]
        if not keys:
            continue
        rec = next((index[k] for k in keys if k in index), None)
        if rec is None:
            rec = {**engine.EMPTY_NOTE, "metrics": {}, "background": [], "notes": [], "first_met": n.get("created_at")}
            recs.append(rec)
        for k in keys:
            index[k] = rec
        for f in ("founder", "company", "sector", "stage", "one_liner", "raising_usd"):
            if x.get(f):
                rec[f] = x[f]
        rec["event"] = rec.get("event") or x.get("event")
        rec["metrics"].update(x.get("metrics") or {})
        rec["background"] = list(dict.fromkeys(rec["background"] + (x.get("background") or [])))[:6]
        rec["next_step"], rec["next_due"] = x.get("next_step"), x.get("next_due")  # the latest note sets what's next
        rec["last_touch"] = n.get("created_at")
        rec["notes"].append({"id": n.get("id"), "text": n.get("text", ""), "created_at": n.get("created_at")})
        if n.get("akta"):
            rec["akta"] = n["akta"]
    for rec in recs:
        rec["id"] = _key(rec.get("founder") or rec.get("company")).replace(" ", "-")
    return recs


def status_of(rec: dict, today: dt.date) -> tuple[str, str]:
    if rec.get("next_step"):
        if rec.get("next_due"):
            d = dt.date.fromisoformat(rec["next_due"])
            if d < today:
                return "overdue", f"Overdue since {nice_date(rec['next_due'])}"
            if d == today:
                return "due", "Due today"
            if d == today + dt.timedelta(days=1):
                return "due", "Due tomorrow"
            return "scheduled", f"Due {nice_date(rec['next_due'])}"
        age = (today - _ts(rec["last_touch"]).date()).days
        return ("overdue", f"Open for {age} days") if age > 7 else ("due", "Open")
    return "warm", "No next step"


def describe_rec(rec: dict) -> str:
    who = rec.get("founder") or rec.get("company")
    co = f" ({rec['company']})" if rec.get("founder") and rec.get("company") else ""
    return f"{who}{co}"


# --------------------------------------------------------------------------- firm memory (team)
def team_memory(user: dict) -> list[dict]:
    """What each teammate's agent shares. Only people who added each other share."""
    out = []
    for m in user.get("team", []):
        mate = load_user(m)
        if not mate or user["username"] not in mate.get("team", []):
            continue
        notes = notes_of(m, 100)
        out.append({"username": m, "name": mate["name"], "first": first(mate["name"]),
                    "focus": (mate.get("profile") or {}).get("focus"), "notes": notes, "founders": founders_from(notes)})
    return out


def with_team_facts(rec: dict, mem: list[dict]) -> dict:
    """Fill what the user's notes don't say (sector, stage, traction, raise) from teammates' notes on the same founder."""
    keys = {k for k in (_key(rec.get("founder")), _key(rec.get("company"))) if k}
    out, filled = {**rec, "metrics": dict(rec.get("metrics") or {})}, []
    for mate in mem:
        for r in mate["founders"]:
            if not keys & {_key(r.get("founder")), _key(r.get("company"))}:
                continue
            for f in ("sector", "stage", "raising_usd", "founder", "company", "one_liner"):
                if not out.get(f) and r.get(f):
                    out[f] = r[f]
                    filled.append(mate["first"])
            for k, v in (r.get("metrics") or {}).items():
                if k not in out["metrics"]:
                    out["metrics"][k] = v
                    filled.append(mate["first"])
            out["background"] = list(dict.fromkeys((out.get("background") or []) + (r.get("background") or [])))
    out["filled_from"] = sorted(set(filled))
    return out


def who_knows(mem: list[dict], names: list[str]) -> list[dict]:
    keys = [_key(n) for n in names if n and len(_key(n)) >= 3]
    if not keys:
        return []
    hits = []
    for mate in mem:
        hit = None
        for rec in mate["founders"]:
            if _key(rec.get("founder")) in keys or _key(rec.get("company")) in keys:
                met = f" at {rec['event']}" if rec.get("event") else ""
                nxt = f" {mate['first']}'s next step: {rec['next_step']}." if rec.get("next_step") else " No open next step."
                hit = {"who": mate["first"], "username": mate["username"],
                       "text": f"{mate['first']} met {describe_rec(rec)}{met} on {nice_date(rec['first_met'])}, last noted "
                               f"{nice_date(rec['last_touch'])}.{nxt}"}
                break
        if not hit:
            for n in mate["notes"]:
                low = _key(n.get("text"))
                if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys):
                    hit = {"who": mate["first"], "username": mate["username"],
                           "text": f"{mate['first']}'s note from {nice_date(n['created_at'])}: \"{n['text'][:220]}\""}
                    break
        if hit:
            hits.append(hit)
    return hits


# --------------------------------------------------------------------------- saving a note (no approval needed)
def log_action(user: dict, act: dict):
    """Every write is logged, so Settings can show what the agent did and what PRISM decided."""
    store.put(f"actions/{user['username']}/{act['id']}.json", act)


def note_markdown(user: dict, note: dict) -> str:
    x = note["extracted"]
    lines = [f"# {describe_rec(x) if (x.get('founder') or x.get('company')) else 'Note'}", "", note["text"], "",
             f"Recorded by {user['name']} (@{user['username']}) on {nice_date(note['created_at'])}."]
    for k, label in (("event", "Met at"), ("next_step", "Next step"), ("next_due", "Due")):
        if x.get(k):
            lines.append(f"- {label}: {x[k]}")
    return "\n".join(lines)


@app.post("/api/notes")
@login_required
def api_note_save(user):
    t0 = time.time()
    p = request.get_json(silent=True) or {}
    raw = str(p.get("text") or "").strip()
    if not raw:
        return jsonify({"error": "Empty note"}), 400
    text, redacted = guard.redact(raw[:guard.MAX_NOTE])
    extracted, reader = extract(user, text, dt.date.today())
    mode = mode_of(user)
    act = {"id": new_id(), "type": "save_note", "mode": mode, "created_at": now(), "summary": "Save a note to the firm memory",
           "payload": {"text": text, "source": "dictation" if p.get("source") == "dictation" else "typed", "extracted": extracted}}
    if redacted:
        act["redacted"] = redacted
    if mode == "auto":
        name, aid = agent_of(user)
        act["review"] = guard.review(name, aid, session.get("sid") or aid, act)
    if act.get("review", {}).get("decision") == "block":
        act["status"], act["reason"] = "blocked", f"PRISM blocked this note: {act['review']['reason']}"
    else:
        note = {"id": act["id"], "text": text, "source": act["payload"]["source"], "extracted": extracted, "created_at": act["created_at"]}
        if ix.AKTA.live and (extracted.get("company") or extracted.get("one_liner")):
            t1, ix.AKTA.error = time.time(), None
            note["akta"] = ix.AKTA.lookup(extracted.get("company"), extracted.get("one_liner"))
            trace_tool(user, "akta.lookup", f"{extracted.get('company')} | {extracted.get('one_liner')}",
                       json.dumps(note["akta"])[:500], t1, "error" if ix.AKTA.error and not note["akta"]["match"] else "success")
        try:
            store.put(f"notes/{user['username']}/{act['id']}.json", note)
            act["status"] = "done"
            act["akta"] = note.get("akta")
        except Exception as e:
            act["status"], act["reason"] = "failed", scrub(f"Could not save: {e}")[:180]
        if act["status"] == "done" and ix.ROUNDTABLE.live:
            act["roundtable"] = bool(ix.ROUNDTABLE.save_note(f"Last Touch · {describe_rec(extracted) if (extracted.get('founder') or extracted.get('company')) else 'Note'}",
                                                             note_markdown(user, note), f"lt-{user['username']}-{act['id']}"))
    if act.get("review") and act["review"]["decision"] == "unknown" and act["status"] == "done":
        act["reason"] = f"Saved. PRISM could not decide ({act['review']['reason']})."
    log_action(user, act)
    hits = who_knows(team_memory(user), [extracted.get("founder"), extracted.get("company")]) if act["status"] == "done" else []
    trace_tool(user, "save_note", text[:500], f"{act['status']}; read with {reader}; {len(hits)} teammate matches", t0,
               "error" if act["status"] == "failed" else "success")
    return jsonify({**view(act), "hits": hits, "reader": reader})


def view(act: dict) -> dict:
    return {k: act.get(k) for k in ("id", "type", "summary", "payload", "status", "reason", "review", "result", "mode",
                                    "created_at", "redacted", "akta", "roundtable")}


@app.get("/api/actions")
@login_required
def api_actions(user):
    paths = store.list_paths(f"actions/{user['username']}/")[-12:]
    return jsonify({"actions": [view(a) for a in (store.get(p) for p in reversed(paths)) if a]})


# --------------------------------------------------------------------------- account
@app.get("/api/username/<u>")
def api_username(u):
    u = norm(u)
    if not USER_RE.match(u):
        return jsonify({"valid": False, "message": "Use 2 to 30 letters, numbers, dots, dashes or underscores."})
    exists = load_user(u) is not None
    return jsonify({"valid": True, "available": not exists, "exists": exists})


def password_problem(pw: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters for your password."
    if len(pw) > 200:
        return "That password is too long."
    return None


def set_password(user: dict, pw: str):
    user["password"] = generate_password_hash(pw)
    user["pw_ver"] = user.get("pw_ver", 0) + 1
    user.pop("login", None)


@app.post("/api/signup")
def api_signup():
    b = request.get_json(silent=True) or {}
    name, u, pw = " ".join(str(b.get("name") or "").split())[:60], norm(b.get("username")), str(b.get("password") or "")
    if not name:
        return jsonify({"error": "Enter your name."}), 400
    if not USER_RE.match(u):
        return jsonify({"error": "Use 2 to 30 letters, numbers, dots, dashes or underscores."}), 400
    if password_problem(pw):
        return jsonify({"error": password_problem(pw)}), 400
    user = {"username": u, "name": name, "created_at": now(), "team": [], "settings": {"mode": "ask"}}
    set_password(user, pw)
    try:
        store.put(f"users/{u}.json", user, overwrite=False)
    except store.Exists:
        return jsonify({"error": f"@{u} is taken."}), 409
    start_session(user)
    return jsonify({"user": public(user)})


@app.post("/api/signin")
def api_signin():
    b = request.get_json(silent=True) or {}
    u, pw = norm(b.get("username")), str(b.get("password") or "")
    user = load_user(u)
    if not user:
        return jsonify({"error": "Wrong username or password."}), 403
    login = user.get("login") or {}
    locked = login.get("locked_until")
    if locked and _ts(locked) > dt.datetime.now(dt.timezone.utc):
        return jsonify({"error": f"Too many wrong passwords. Try again after {_ts(locked).strftime('%H:%M')} UTC."}), 429
    if not user.get("password"):
        # Accounts made before passwords existed: prove it's you with the account's name, then choose a password.
        name = str(b.get("name") or "")
        if not name:
            return jsonify({"error": "setup", "message": "This account doesn't have a password yet. Enter the name on the account "
                                                         "and choose a password."}), 409
        if " ".join(name.lower().split()) != " ".join(user["name"].lower().split()) or password_problem(pw):
            return jsonify({"error": password_problem(pw) or "That name doesn't match this username."}), 403
        set_password(user, pw)
        save_user(user)
        start_session(user)
        return jsonify({"user": public(user)})
    if not check_password_hash(user["password"], pw):
        fails = login.get("fails", 0) + 1
        user["login"] = {"fails": fails}
        if fails >= MAX_FAILS:
            user["login"] = {"fails": 0, "locked_until": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=LOCK_MINUTES)).isoformat()}
        save_user(user)
        return jsonify({"error": "Wrong username or password."}), 403
    if login:
        user.pop("login", None)
        save_user(user)
    start_session(user)
    return jsonify({"user": public(user)})


@app.post("/api/password")
@login_required
def api_password(user):
    b = request.get_json(silent=True) or {}
    if user.get("password") and not check_password_hash(user["password"], str(b.get("current") or "")):
        return jsonify({"error": "Your current password is wrong."}), 403
    pw = str(b.get("new") or "")
    if password_problem(pw):
        return jsonify({"error": password_problem(pw)}), 400
    set_password(user, pw)
    save_user(user)
    start_session(user)  # keep this device signed in; every other device is signed out
    return jsonify({"ok": True})


@app.post("/api/signout")
def api_signout():
    user, sid = current(), session.get("sid")
    if user and sid and traced(user):
        name, aid = agent_of(user)
        ix.TRACER.end_session(sid, aid, name)
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    user = current()
    return jsonify({"user": public(user) if user else None})


@app.post("/api/profile")
@login_required
def api_profile(user):
    keys = {q["key"] for q in INTERVIEW}
    answers = {k: str(v)[:200] for k, v in (request.get_json(silent=True) or {}).items() if k in keys}
    user["answers"] = answers
    user["profile"] = build_profile(answers, user["name"])
    save_user(user)
    return jsonify({"user": public(user)})


@app.post("/api/settings")
@login_required
def api_settings(user):
    b = request.get_json(silent=True) or {}
    if b.get("mode") in ("ask", "auto"):
        if b["mode"] == "auto" and not guard.live():
            return jsonify({"error": "Auto mode needs PRISM (PRISMTRACE_API_KEY on the server)."}), 400
        user.setdefault("settings", {})["mode"] = b["mode"]
        save_user(user)
    return jsonify({"user": public(user)})


@app.get("/api/health")
def api_health():
    """Non-secret setup check: which storage and session key this deployment is using."""
    return jsonify({"store": store.KIND, "store_mode": store.mode(), "session_key": "SECRET_KEY" if env("SECRET_KEY") else
                    "shared key in the store", "prism": guard.live(), "akta": ix.AKTA.live, "nicrron": ix.LLM_CLIENT.live,
                    "roundtable": ix.ROUNDTABLE.live})


@app.get("/api/integrations")
@login_required
def api_integrations(user):
    out = {"store": store.mode(),
           "nicrron": ix.LLM_CLIENT.live, "akta": {"live": ix.AKTA.live, "paid": ix.AKTA.use_paid, "error": ix.AKTA.error},
           "roundtable": {"live": ix.ROUNDTABLE.live, "error": ix.ROUNDTABLE.error}, "prism": {"configured": guard.live()}}
    if guard.live():
        out["prism"].update(ix.TRACER.status())
    return jsonify(out)


# --------------------------------------------------------------------------- agent skills (reads, always allowed)
def hard_no(profile: dict, rec: dict) -> str | None:
    """The user's hard no's from the interview, checked against what their notes and akta.pro say."""
    nos = (profile.get("nos") or "").lower()
    ak = rec.get("akta") or {}
    text = " ".join([n["text"] for n in rec.get("notes", [])] + [str(rec.get("one_liner") or ""),
                    str((ak.get("industry") or {}).get("name") or ""), str((ak.get("match") or {}).get("description") or "")]).lower()
    if ("crypto" in nos or "token" in nos) and re.search(r"\b(token|crypto|web3|blockchain|nft|defi)\b", text):
        return "Your hard no: token or crypto"
    if ("agenc" in nos or "service" in nos) and re.search(r"\b(agency|consultancy|services business|dev shop)\b", text):
        return "Your hard no: agency or services business"
    return None


def akta_line(ak: dict | None) -> str | None:
    if not ak or not (ak.get("match") or ak.get("industry") or ak.get("similar")):
        return None
    bits = []
    if ak.get("match"):
        m = ak["match"]
        bits.append(m["name"] + (f" ({m['website']})" if m.get("website") else "") + (f", {m['status']}" if m.get("status") else ""))
    if ak.get("industry"):
        bits.append(f"industry: {ak['industry']['name']}")
    if ak.get("similar"):
        bits.append("similar: " + ", ".join(c["name"] for c in ak["similar"][:4]))
    return "; ".join(bits)


@app.get("/api/founders")
@login_required
def api_founders(user):
    """Everyone the user has recorded notes about: what's next, and how they score on the user's criteria."""
    t0 = time.time()
    notes = notes_of(user["username"])
    recs = founders_from(notes)
    profile = user.get("profile")
    member = {"sectors": profile["sectors"], "stages": profile["stages"], "weights": profile["weights"]} if profile else None
    mem = team_memory(user)
    today = dt.date.today()
    out = []
    for r in recs:
        st, due = status_of(r, today)
        row = {"id": r["id"], "founder": r.get("founder"), "company": r.get("company"), "event": r.get("event"),
               "sector": r.get("sector"), "stage": r.get("stage"), "one_liner": r.get("one_liner"),
               "next_step": r.get("next_step"), "status": st, "due": due, "first_met": nice_date(r["first_met"]),
               "last_touch": r["last_touch"], "when": nice_date(r["last_touch"]), "notes": len(r["notes"]),
               "latest": r["notes"][-1]["text"][:240], "traction": engine.metrics_line(r.get("metrics")),
               "raise": engine.money(r.get("raising_usd")), "background": "; ".join(r.get("background") or []),
               "known": who_knows(mem, [r.get("founder"), r.get("company")]), "akta": akta_line(r.get("akta"))}
        if member:
            facts = with_team_facts(r, mem)
            row.update(sector=facts.get("sector"), stage=facts.get("stage"), traction=engine.metrics_line(facts.get("metrics")),
                       filled_from=facts["filled_from"])
            row["raise"] = engine.money(facts.get("raising_usd"))
            s = engine.score_member({**facts, "warm_intro": bool(row["known"])}, member)
            top = sorted(((k, v) for k, v in s["parts"].items() if v), key=lambda kv: -kv[1])[:2]
            no = hard_no(profile, r)
            row.update(score=s["score"], verdict=(no if no else "Outside your thesis" if s["features"]["thesis"] == 0 else
                                                  "Take the next call" if s["score"] >= 65 else
                                                  "Keep warm" if s["score"] >= 45 else "Pass"),
                       why=", ".join(f"{LABEL[k]} {v:g}/{member['weights'][k]}" for k, v in top) or "little to go on")
        out.append(row)
    out.sort(key=lambda r: r["last_touch"], reverse=True)
    trace_tool(user, "list_founders", f"{len(notes)} notes", f"{len(out)} founders, "
               f"{sum(r['status'] in ('due', 'overdue') for r in out)} need a follow-up", t0)
    return jsonify({"founders": out, "notes": len(notes), "unlinked": len(notes) - sum(r["notes"] for r in out)})


@app.get("/api/source")
@login_required
def api_source(user):
    """Find new companies on akta.pro that match the user's focus, and say which ones the team already knows."""
    t0 = time.time()
    profile = user.get("profile")
    if not profile:
        return jsonify({"error": "interview", "message": "Finish the interview first."}), 409
    if not ix.AKTA.live:
        return jsonify({"error": "akta", "message": "akta.pro isn't connected (AKTA_API_KEY on the server)."}), 409
    query = str(request.args.get("q") or "").strip()[:120] or f"{profile['focus']} startups"
    ix.AKTA.error = None
    if ix.AKTA.use_paid:
        rows = ix.AKTA.companies_like(query, 10)
    else:
        rows = ix.AKTA.company_search(query)
    mine = {k for r in founders_from(notes_of(user["username"])) for k in (_key(r.get("company")), _key(r.get("founder"))) if k}
    mem = team_memory(user)
    out = []
    for c in rows[:12]:
        name = ix._name(c)
        out.append({"name": name, "website": c.get("website") or c.get("domain"),
                    "description": str(c.get("description") or c.get("short_description") or "")[:200] or None,
                    "mine": _key(name) in mine, "known": who_knows(mem, [name])})
    trace_tool(user, "akta.source", query, f"{len(out)} companies" + (f"; {ix.AKTA.error}" if ix.AKTA.error and not out else ""), t0,
               "error" if ix.AKTA.error and not out else "success")
    return jsonify({"query": query, "companies": out, "paid": ix.AKTA.use_paid, "error": ix.AKTA.error if not out else None})


@app.get("/api/lookup")
@login_required
def api_lookup(user):
    """akta.pro lookup for one company, plus what the team already knows about it."""
    t0 = time.time()
    q = str(request.args.get("q") or "").strip()[:80]
    if not q:
        return jsonify({"error": "Say which company to look up."}), 400
    if not ix.AKTA.live:
        return jsonify({"error": "akta", "message": "akta.pro isn't connected (AKTA_API_KEY on the server)."}), 409
    ix.AKTA.error = None
    ak = ix.AKTA.lookup(q, None)
    trace_tool(user, "akta.lookup", q, json.dumps(ak)[:500], t0)
    return jsonify({"query": q, "akta": ak, "line": akta_line(ak), "known": who_knows(team_memory(user), [q]),
                    "error": ix.AKTA.error if not ak.get("match") else None})


@app.get("/api/notes")
@login_required
def api_notes(user):
    return jsonify({"notes": notes_of(user["username"], 20)})


@app.get("/api/team")
@login_required
def api_team(user):
    out = []
    for m in user.get("team", []):
        mate = load_user(m)
        if mate:
            out.append({"username": m, "name": mate["name"], "focus": (mate.get("profile") or {}).get("focus"),
                        "mutual": user["username"] in mate.get("team", []), "ready": bool(mate.get("profile"))})
    return jsonify({"team": out})


@app.post("/api/team")
@login_required
def api_team_add(user):
    u = norm((request.get_json(silent=True) or {}).get("username"))
    if u == user["username"]:
        return jsonify({"error": "That's you."}), 400
    mate = load_user(u)
    if not mate:
        return jsonify({"error": f"No one is called @{u}."}), 404
    if u in user.get("team", []):
        return jsonify({"error": f"@{u} is already on your team."}), 409
    user.setdefault("team", []).append(u)
    save_user(user)
    return jsonify({"user": public(user), "mate": {"username": u, "name": mate["name"], "mutual": user["username"] in mate.get("team", [])}})


@app.delete("/api/team/<u>")
@login_required
def api_team_remove(user, u):
    u = norm(u)
    user["team"] = [x for x in user.get("team", []) if x != u]
    save_user(user)
    return jsonify({"user": public(user)})


@app.get("/api/team/activity")
@login_required
def api_team_activity(user):
    t0 = time.time()
    mem = team_memory(user)
    today = dt.date.today()
    team = []
    for m in mem:
        recent = sorted(m["founders"], key=lambda r: r["last_touch"], reverse=True)
        team.append({"username": m["username"], "name": m["name"], "focus": m["focus"], "count": len(m["founders"]),
                     "founders": [{"who": describe_rec(r), "event": r.get("event"), "when": nice_date(r["last_touch"]),
                                   "next_step": r.get("next_step"), "status": status_of(r, today)[0]} for r in recent[:4]],
                     "latest": m["notes"][0]["text"][:200] if m["notes"] else None})
    trace_tool(user, "team_activity", f"{len(user.get('team', []))} teammates", f"{len(team)} agents shared", t0)
    return jsonify({"team": team, "pending": [u for u in user.get("team", []) if u not in {m["username"] for m in mem}]})


@app.get("/api/team/search")
@login_required
def api_team_search(user):
    t0 = time.time()
    q = str(request.args.get("q") or "").strip()[:80]
    hits = who_knows(team_memory(user), [q])
    trace_tool(user, "who_knows", q, f"{len(hits)} teammate replies", t0)
    return jsonify({"hits": hits})


# --------------------------------------------------------------------------- page
ICONS = {
    "spark": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    "tool": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    "db": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    "agent": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
    "settings": '<line x1="21" y1="4" x2="14" y2="4"/><line x1="10" y1="4" x2="3" y2="4"/><line x1="21" y1="12" x2="12" y2="12"/><line x1="8" y1="12" x2="3" y2="12"/><line x1="21" y1="20" x2="16" y2="20"/><line x1="12" y1="20" x2="3" y2="20"/><line x1="14" y1="2" x2="14" y2="6"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="16" y1="18" x2="16" y2="22"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "userplus": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "note": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
}
PAGE = (Path(__file__).parent / "page.html").read_text()


@app.get("/")
def index():
    data = {"icons": ICONS, "interview": INTERVIEW, "prism": guard.live(), "llm": ix.LLM_CLIENT.live, "akta": ix.AKTA.live,
            "store": store.mode()}
    blob = json.dumps(data).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob)


if __name__ == "__main__":
    app.run(debug=True, port=8000)
