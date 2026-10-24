"""Last Touch (Track 1, sourcing). Flask app for Vercel (exports `app`).

Every person at the firm signs in with a name and a username and gets a personal agent. The agent interviews them,
reads their Google account (Gmail, Google Calendar, Google Chat), sources founders from their inbox with their own
criteria, and shares who they are talking to with their team through the firm memory on Vercel Blob.
Every write the agent makes goes through guard.py: Ask mode asks the user, Auto mode lets PRISM decide.

Run locally: flask --app app run
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import datetime as dt  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
from functools import wraps  # noqa: E402
from pathlib import Path  # noqa: E402

from flask import Flask, jsonify, redirect, request, session, url_for  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

import engine  # noqa: E402
import google_api as g  # noqa: E402
import guard  # noqa: E402
import integrations as ix  # noqa: E402
import store  # noqa: E402

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# Sessions must survive across serverless instances, so the key has to be stable. Falls back to one derived from the
# Blob token (also a server secret) so a deploy works before SECRET_KEY is set.
app.secret_key = os.getenv("SECRET_KEY") or (hashlib.sha256(("last-touch:" + store.TOKEN).encode()).hexdigest() if store.TOKEN
                                             else secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))

USER_RE = re.compile(r"^[a-z0-9_.-]{2,30}$")
INTERVIEW = [
    {"key": "role", "q": "Hi {name}, I'm your new agent. I'll learn how you work so I can source the way you do. What's your role, and where does most of your week go?",
     "chips": ["Associate: sourcing and first calls", "Partner: leading deals", "Talent partner: founders and hiring"]},
    {"key": "focus", "q": "Which sectors and stages do you source in?",
     "chips": ["AI and devtools, pre-seed to seed", "Consumer and CPG, seed", "Fintech, seed to Series A"]},
    {"key": "signal", "q": "What makes you take a first call? Tell me the signal you trust most.",
     "chips": ["Technical founders with open-source traction", "Real revenue and repeat purchase", "A warm intro from someone I trust"]},
    {"key": "nos", "q": "Last one. Any hard no's I should filter out for you?",
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
    return f"{int(dt.datetime.now().timestamp() * 1000)}-{secrets.token_hex(3)}"


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
    gg = user.get("google") or {}
    return {"username": user["username"], "name": user["name"], "first": first(user["name"]), "profile": user.get("profile"),
            "team": user.get("team", []), "mode": mode_of(user),
            "google": {"connected": bool(gg.get("refresh_token")), "email": gg.get("email")}}


def current() -> dict | None:
    u = session.get("u")
    user = load_user(u) if u else None
    if u and not user:
        session.clear()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current()
        if not user:
            return jsonify({"error": "signin"}), 401
        return fn(user, *a, **kw)
    return wrapper


def google_token(user: dict) -> str:
    return g.access_token(user["username"], user.get("google"))


def needs_google():
    return jsonify({"error": "google", "message": "Connect your Google account in Settings first."}), 409


def build_profile(a: dict, name: str) -> dict:
    focus = str(a.get("focus") or "")
    fl = focus.lower()
    sectors = []
    if any(k in fl for k in ("ai", "devtool", "dev tool", "infra", "developer")):
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
        "sectors": sectors, "stages": stages, "signal": str(a.get("signal") or "")[:100], "style": style,
        "nos": str(a.get("nos") or "No hard no's")[:80], "weights": w,
        "weights_text": ", ".join(f"{LABEL[k]} {v}%" for k, v in sorted(w.items(), key=lambda kv: -kv[1]) if v),
    }


# --------------------------------------------------------------------------- firm memory (team)
def notes_of(u: str, limit: int = 20) -> list[dict]:
    paths = store.list_paths(f"notes/{u}/")[-limit:]
    return [n for n in (store.get(p) for p in reversed(paths)) if n]


def team_memory(user: dict) -> list[dict]:
    """What each teammate's agent has shared: notes, sourced founders and who they talk to."""
    out = []
    for m in user.get("team", []):
        mate = load_user(m)
        if not mate or user["username"] not in mate.get("team", []):
            continue
        out.append({"username": m, "name": mate["name"], "first": first(mate["name"]),
                    "focus": (mate.get("profile") or {}).get("focus"),
                    "notes": notes_of(m, 15),
                    "pipeline": (store.get(f"pipeline/{m}.json") or {}).get("items", []),
                    "contacts": (store.get(f"contacts/{m}.json") or {}).get("items", [])})
    return out


def who_knows(mem: list[dict], *, email: str | None = None, names: list[str] = ()) -> list[dict]:
    email = (email or "").lower()
    names = [n.lower() for n in names if n and len(n) >= 3]
    hits = []
    for mate in mem:
        for c in mate["contacts"]:
            if (email and c.get("email") == email) or (c.get("name") or "").lower() in names:
                hits.append({"who": mate["first"], "username": mate["username"], "kind": "contact",
                             "text": f"{mate['first']} last talked to {c.get('name') or c.get('email')} on {str(c.get('last_touch', ''))[:10]} "
                                     f"via {', '.join(c.get('channels', []))}."})
                break
        for p in mate["pipeline"]:
            if (email and p.get("email") == email) or any(x and x.lower() in names for x in (p.get("company"), p.get("founder"))):
                hits.append({"who": mate["first"], "username": mate["username"], "kind": "pipeline",
                             "text": f"{p.get('company') or p.get('founder')} is in {mate['first']}'s pipeline "
                                     f"({p.get('score')}/100 on {mate['first']}'s thesis, {str(p.get('verdict', '')).lower()})."})
                break
        for n in mate["notes"]:
            low = n.get("text", "").lower()
            if any(re.search(rf"\b{re.escape(x)}\b", low) for x in names) or (email and email in low):
                hits.append({"who": mate["first"], "username": mate["username"], "kind": "note",
                             "text": f"{mate['first']}'s note from {str(n.get('created_at', ''))[:10]}: \"{n['text'][:220]}\""})
                break
    return hits


# --------------------------------------------------------------------------- actions (every write goes through here)
def describe(kind: str, p: dict) -> str:
    if kind == "send_email":
        return f"Send an email to {', '.join(p.get('to', []))}: \"{p.get('subject', '')}\""
    if kind == "save_note":
        return "Save this note to your firm memory"
    if kind == "share_pipeline":
        return f"Share {len(p.get('items', []))} sourced founders with your team's agents"
    if kind == "share_contacts":
        return f"Share who you're talking to ({len(p.get('items', []))} people, no message content) with your team's agents"
    return kind


def clean_payload(kind: str, p: dict) -> dict:
    if kind == "send_email":
        to = p.get("to") or []
        to = [str(x).strip() for x in (to if isinstance(to, list) else str(to).split(",")) if str(x).strip()]
        return {"to": to, "subject": str(p.get("subject") or "")[:300], "body": str(p.get("body") or "")[:6000],
                "thread": str(p.get("thread") or "")[:40] or None}
    if kind == "save_note":
        return {"text": str(p.get("text") or "")[:9000], "source": "dictation" if p.get("source") == "dictation" else "typed"}
    if kind == "share_pipeline":
        keep = ("company", "founder", "email", "score", "verdict", "sector", "stage", "subject", "when")
        return {"items": [{k: i.get(k) for k in keep} for i in (p.get("items") or [])[:200] if isinstance(i, dict)]}
    if kind == "share_contacts":
        keep = ("name", "email", "last_touch", "channels", "status")
        return {"items": [{k: i.get(k) for k in keep} for i in (p.get("items") or [])[:200] if isinstance(i, dict)]}
    raise ValueError(kind)


def sent_today(user: dict) -> int:
    s = user.get("sent") or {}
    return s.get("n", 0) if s.get("day") == dt.date.today().isoformat() else 0


def execute(user: dict, kind: str, p: dict) -> dict:
    u = user["username"]
    if kind == "send_email":
        res = g.gmail_send(google_token(user), user["google"]["email"], p["to"], p["subject"], p["body"], p.get("thread"))
        user["sent"] = {"day": dt.date.today().isoformat(), "n": sent_today(user) + 1}
        save_user(user)
        return {"message_id": res.get("id"), "text": f"Sent to {', '.join(p['to'])}."}
    if kind == "save_note":
        nid = new_id()
        store.put(f"notes/{u}/{nid}.json", {"id": nid, "text": p["text"], "source": p["source"], "created_at": now()})
        return {"note_id": nid, "text": "Saved to your firm memory. Your teammates' agents can see it."}
    if kind == "share_pipeline":
        store.put(f"pipeline/{u}.json", {"updated": now(), "items": p["items"]})
        return {"text": "Shared. Your teammates' agents will see these founders are in your pipeline."}
    if kind == "share_contacts":
        store.put(f"contacts/{u}.json", {"updated": now(), "items": p["items"]})
        return {"text": "Shared. Your teammates' agents now know who you're talking to."}
    raise ValueError(kind)


def run_action(user: dict, act: dict):
    try:
        act["result"] = execute(user, act["type"], act["payload"])
        act["status"] = "done"
    except g.NotConnected:
        act["status"], act["reason"] = "failed", "Google is not connected"
    except Exception as e:
        act["status"], act["reason"] = "failed", str(e)[:200]


def view(act: dict) -> dict:
    return {k: act.get(k) for k in ("id", "type", "summary", "payload", "status", "reason", "review", "result", "mode", "created_at")}


@app.post("/api/actions")
@login_required
def api_action_propose(user):
    body = request.get_json(silent=True) or {}
    kind = body.get("type")
    try:
        p = clean_payload(kind, body.get("payload") or {})
    except ValueError:
        return jsonify({"error": "Unknown action"}), 400
    if kind == "send_email" and not (user.get("google") or {}).get("refresh_token"):
        return needs_google()
    mode = mode_of(user)
    act = {"id": new_id(), "type": kind, "payload": p, "summary": describe(kind, p), "mode": mode, "created_at": now()}
    why = guard.hard_rules(kind, p, sent_today(user))
    if why:
        act["status"], act["reason"] = "blocked", why
    elif mode == "auto":
        act["review"] = guard.review(f"{first(user['name'])}'s agent", user["username"], act)
        d = act["review"]["decision"]
        if d == "allow":
            run_action(user, act)
        elif d == "block":
            act["status"], act["reason"] = "blocked", f"PRISM blocked this: {act['review']['reason']}"
        else:
            act["status"], act["reason"] = "needs_approval", f"PRISM could not decide ({act['review']['reason']}), so I'm asking you."
    else:
        act["status"] = "needs_approval"
    store.put(f"actions/{user['username']}/{act['id']}.json", act)
    return jsonify(view(act))


@app.post("/api/actions/<aid>")
@login_required
def api_action_decide(user, aid):
    if not re.fullmatch(r"[0-9a-f-]{8,40}", aid):
        return jsonify({"error": "Not found"}), 404
    path = f"actions/{user['username']}/{aid}.json"
    act = store.get(path)
    if not act:
        return jsonify({"error": "Not found"}), 404
    if act["status"] != "needs_approval":
        return jsonify(view(act))
    if (request.get_json(silent=True) or {}).get("approve"):
        why = guard.hard_rules(act["type"], act["payload"], sent_today(user))
        if why:
            act["status"], act["reason"] = "blocked", why
        else:
            act["approved_by"] = user["username"]
            run_action(user, act)
    else:
        act["status"] = "denied"
    act["decided_at"] = now()
    store.put(path, act)
    return jsonify(view(act))


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
    user = load_user(u)
    return jsonify({"valid": True, "available": user is None, "exists": user is not None,
                    "google": bool(user and (user.get("google") or {}).get("refresh_token"))})


@app.post("/api/signup")
def api_signup():
    b = request.get_json(silent=True) or {}
    name, u = str(b.get("name") or "").strip()[:60], norm(b.get("username"))
    if not name:
        return jsonify({"error": "Enter your name."}), 400
    if not USER_RE.match(u):
        return jsonify({"error": "Use 2 to 30 letters, numbers, dots, dashes or underscores."}), 400
    user = {"username": u, "name": name, "created_at": now(), "team": [], "settings": {"mode": "ask"}}
    try:
        store.put(f"users/{u}.json", user, overwrite=False)
    except store.Exists:
        return jsonify({"error": f"@{u} is taken."}), 409
    session.clear()
    session.permanent = True
    session["u"] = u
    return jsonify({"user": public(user)})


@app.post("/api/signin")
def api_signin():
    b = request.get_json(silent=True) or {}
    name, u = str(b.get("name") or "").strip(), norm(b.get("username"))
    user = load_user(u)
    if not user:
        return jsonify({"error": f"No account called @{u}."}), 404
    if (user.get("google") or {}).get("refresh_token"):
        # Linked accounts prove who they are with Google.
        return jsonify({"redirect": url_for("google_start", mode="signin", u=u)})
    if " ".join(name.lower().split()) != " ".join(user["name"].lower().split()):
        return jsonify({"error": "That name doesn't match this username."}), 403
    session.clear()
    session.permanent = True
    session["u"] = u
    return jsonify({"user": public(user)})


@app.post("/api/signout")
def api_signout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    user = current()
    return jsonify({"user": public(user) if user else None})


@app.post("/api/profile")
@login_required
def api_profile(user):
    answers = {k: str(v)[:200] for k, v in (request.get_json(silent=True) or {}).items() if k in {q["key"] for q in INTERVIEW}}
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
            return jsonify({"error": "Auto mode needs PRISM (PRISM_API_KEY and PRISM_PROJECT_ID)."}), 400
        user.setdefault("settings", {})["mode"] = b["mode"]
        save_user(user)
    return jsonify({"user": public(user)})


# --------------------------------------------------------------------------- Google OAuth
def redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI") or url_for("google_callback", _external=True)


@app.get("/auth/google")
def google_start():
    if not g.CONFIGURED:
        return redirect("/?google_error=not_configured")
    mode = request.args.get("mode", "connect")
    state = secrets.token_urlsafe(24)
    hint = None
    if mode == "connect":
        user = current()
        if not user:
            return redirect("/")
        target, scopes = user["username"], g.SCOPES
    elif mode == "signin":
        target = norm(request.args.get("u"))
        user = load_user(target)
        if not user:
            return redirect("/")
        hint, scopes = (user.get("google") or {}).get("email"), g.SIGNIN_SCOPES
    else:
        mode, target, scopes = "login", None, g.SIGNIN_SCOPES
    session["oauth"] = {"state": state, "mode": mode, "u": target}
    return redirect(g.auth_url(redirect_uri(), state, scopes, hint))


@app.get("/auth/google/callback")
def google_callback():
    pending = session.pop("oauth", None) or {}
    if not pending or request.args.get("state") != pending.get("state"):
        return redirect("/?google_error=state")
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/?google_error=" + (request.args.get("error") or "cancelled"))
    try:
        tok = g.exchange(request.args["code"], redirect_uri())
    except Exception:
        return redirect("/?google_error=exchange")
    info = tok["userinfo"]
    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified", True):
        return redirect("/?google_error=email")
    linked = (store.get(f"google/{email}.json") or {}).get("username")
    mode = pending["mode"]
    if mode in ("signin", "login"):
        u = pending.get("u") if mode == "signin" else linked
        user = load_user(u) if u else None
        if not user or (user.get("google") or {}).get("email") != email:
            return redirect("/?google_error=" + ("nomatch" if mode == "signin" else "noaccount"))
        session.clear()
        session.permanent = True
        session["u"] = u
        return redirect("/")
    user = current()
    if not user or user["username"] != pending.get("u"):
        return redirect("/")
    if linked and linked != user["username"]:
        return redirect("/?google_error=linked")
    old = user.get("google") or {}
    refresh = tok.get("refresh_token") or (old.get("refresh_token") if old.get("email") == email else None)
    if not refresh:
        return redirect("/?google_error=offline")
    if old.get("email") and old["email"] != email:
        store.delete(f"google/{old['email']}.json")
    user["google"] = {"email": email, "name": info.get("name"), "refresh_token": refresh, "scope": tok.get("scope"),
                      "connected_at": now()}
    save_user(user)
    store.put(f"google/{email}.json", {"username": user["username"]})
    g.remember(user["username"], tok["access_token"], tok.get("expires_in", 3600))
    return redirect("/?google=connected")


@app.post("/api/google/disconnect")
@login_required
def api_google_disconnect(user):
    gg = user.pop("google", None) or {}
    if gg.get("refresh_token"):
        g.revoke(gg["refresh_token"])
    if gg.get("email"):
        store.delete(f"google/{gg['email']}.json")
    g.forget(user["username"])
    save_user(user)
    return jsonify({"user": public(user)})


# --------------------------------------------------------------------------- agent skills (reads)
def _ts(iso: str) -> dt.datetime:
    """Parse a Google timestamp (RFC 3339 or an all-day date) to UTC."""
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.fromtimestamp(0, dt.timezone.utc)
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def _utc(iso: str) -> str:
    return _ts(iso).isoformat(timespec="seconds")


def _days(iso: str) -> int:
    return (dt.datetime.now(dt.timezone.utc) - _ts(iso)).days


def _nice(iso: str) -> str:
    d = _ts(iso)
    return d.strftime("%a %b %d") + ("" if len(str(iso)) <= 10 or d.strftime("%H:%M:%S") == "00:00:00" else d.strftime(", %H:%M UTC"))


@app.get("/api/conversations")
@login_required
def api_conversations(user):
    try:
        tok = google_token(user)
    except g.NotConnected:
        return needs_google()
    me = user["google"]["email"]
    errors, threads, events, chats = {}, [], [], []
    for label, fn in (("Gmail", lambda: g.gmail_threads(tok, me)), ("Google Calendar", lambda: g.calendar_events(tok, me)),
                      ("Google Chat", lambda: g.chat_recent(tok))):
        try:
            res = fn()
        except Exception as e:
            errors[label] = str(e)[:200]
            continue
        if label == "Gmail":
            threads = res
        elif label == "Google Calendar":
            events = res
        else:
            chats = res
    people: dict[str, dict] = {}
    for t in sorted(threads, key=lambda t: t["when"]):
        t["when"] = _utc(t["when"])
        for email, name in t["people"].items():
            p = people.setdefault(email, {"email": email, "name": name, "channels": [], "last_touch": t["when"]})
            p.update(name=p["name"] if p["name"] != email else name, last_touch=max(p["last_touch"], t["when"]))
            if "Gmail" not in p["channels"]:
                p["channels"].append("Gmail")
            p["thread"] = t
    for e in events:
        upcoming = _ts(e["start"]) >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        start = _utc(e["start"])
        for email, name in e["people"].items():
            p = people.setdefault(email, {"email": email, "name": name, "channels": [], "last_touch": start if not upcoming else ""})
            if "Google Calendar" not in p["channels"]:
                p["channels"].append("Google Calendar")
            if upcoming:
                p.setdefault("next_meeting", e)
            else:
                p["last_meeting"] = {**e, "start": start}
                p["last_touch"] = max(p["last_touch"], start)
    rows = []
    for p in people.values():
        t, nm, lm = p.get("thread"), p.get("next_meeting"), p.get("last_meeting")
        fn = first(p["name"]) or p["email"]
        if nm:
            status, due, nxt = "scheduled", _nice(nm["start"]), f"Prep for \"{nm['title']}\""
            summary = f"Meeting booked: {nm['title']}"
        elif t and not t["last_from_me"]:
            age = _days(t["when"])
            status = "overdue" if age > 2 else "due"
            due = f"{age} days without a reply" if age > 2 else "Reply due"
            nxt, summary = f"Reply to {fn}", f"{t['subject']}: {t['snippet'][:140]}"
        elif lm and (not t or t["when"] < lm["start"]):
            status, due, nxt = "due", f"Met {_nice(lm['start'])}", f"Send {fn} a follow-up"
            summary = f"Meeting: {lm['title']}"
        elif t:
            age = _days(t["when"])
            status = "due" if age > 7 else "warm"
            due = f"Quiet for {age} days" if age > 7 else "Waiting on them"
            nxt, summary = (f"Nudge {fn}" if age > 7 else "Nothing yet"), f"{t['subject']}: {t['snippet'][:140]}"
        else:
            continue
        rows.append({"name": p["name"], "email": p["email"], "channels": p["channels"], "last_touch": p["last_touch"],
                     "when": _nice(p["last_touch"]) if p["last_touch"] else "Upcoming", "status": status, "due": due, "next": nxt, "summary": summary,
                     "thread": t["id"] if t else None, "subject": t["subject"] if t else None})
    order = {"overdue": 0, "due": 1, "scheduled": 2, "warm": 3}
    rows.sort(key=lambda r: r["last_touch"], reverse=True)
    rows = sorted(rows, key=lambda r: order.get(r["status"], 9))[:30]
    return jsonify({"people": rows, "chat": chats[:8], "errors": errors, "email": me})


def _nos_filter(profile: dict, msg: dict) -> str | None:
    nos, text = (profile.get("nos") or "").lower(), f"{msg.get('subject', '')} {msg.get('text', '')}".lower()
    if ("crypto" in nos or "token" in nos) and any(w in text for w in ("token", "crypto", "web3", "blockchain", "nft")):
        return "Your hard no: token or crypto raise"
    if ("agenc" in nos or "service" in nos) and any(w in text for w in ("agency", "consultancy", "services business")):
        return "Your hard no: agency or services business"
    return None


@app.get("/api/source")
@login_required
def api_source(user):
    profile = user.get("profile")
    if not profile:
        return jsonify({"error": "interview", "message": "Finish the interview first."}), 409
    try:
        tok = google_token(user)
        me = user["google"]["email"]
        msgs = g.gmail_inbound(tok, me)
    except g.NotConnected:
        return needs_google()
    except Exception as e:
        return jsonify({"error": "gmail", "message": f"Gmail: {str(e)[:200]}"}), 502
    member = {"sectors": profile["sectors"], "stages": profile["stages"], "weights": profile["weights"]}
    trace = mode_of(user) == "auto"
    agent = f"{first(user['name'])}'s agent"
    mem = team_memory(user)
    leads, dropped = [], []
    for m in msgs:
        ext = None
        if ix.LLM_CLIENT.live:
            got = ix.LLM_CLIENT.json(engine.EXTRACT_SYSTEM, f"From: {m['from_name']} <{m['from_email']}>\nSubject: {m['subject']}\n\n{m['text'][:3000]}",
                                     agent, trace=trace)
            ext = {**engine.EMPTY_EXT, **got} if got else None
        ext = ext or engine.heuristic_extract(m)
        decision, reason = engine.triage(m, ext)
        no = _nos_filter(profile, m)
        if decision == "drop" or no:
            dropped.append({"from": m["from_name"], "subject": m["subject"], "reason": no or reason})
            continue
        s = engine.score_member(ext, member)
        score, thesis = s["score"], s["features"]["thesis"]
        verdict = ("Outside your thesis" if thesis == 0 else "Take the first call" if score >= 65
                   else "Keep warm" if score >= 45 else "Pass")
        top = sorted(((k, v) for k, v in s["parts"].items() if v), key=lambda kv: -kv[1])[:2]
        why = ", ".join(f"{LABEL[k]} {v:g}/{member['weights'][k]}" for k, v in top)
        if ext.get("closing_weeks") and ext["closing_weeks"] <= 8:
            why += f". Closing in {ext['closing_weeks']:g} weeks"
        founder = ext.get("founder") or m["from_name"]
        known = who_knows(mem, email=m["from_email"], names=[ext.get("company"), founder])
        leads.append({
            "id": m["id"], "thread": m["thread"], "email": m["from_email"], "founder": founder, "company": ext.get("company"),
            "subject": m["subject"], "one_liner": ext.get("one_liner") or m["subject"], "sector": ext.get("sector") or "unknown",
            "stage": ext.get("stage") or "stage not stated", "score": score, "verdict": verdict, "why": why or "little to go on",
            "traction": engine.metrics_line(ext.get("metrics")), "background": "; ".join(ext.get("background") or []) or "Not stated",
            "raise": engine.money(ext.get("raising_usd")), "when": m["when"][:10], "known": known,
            "draft": f"Hi {first(founder) or 'there'},\n\nThanks for reaching out about {ext.get('company') or 'what you are building'}. "
                     f"I'd love to learn more. Do you have 20 minutes next week?\n\nBest,\n{first(user['name'])}",
        })
    leads.sort(key=lambda lead: (lead["verdict"] == "Outside your thesis", -lead["score"]))
    return jsonify({"scanned": len(msgs), "leads": leads, "dropped": dropped, "extractor": "Nicrron" if ix.LLM_CLIENT.live else "keyword rules"})


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
    mem = team_memory(user)
    return jsonify({"team": [{"username": m["username"], "name": m["name"], "focus": m["focus"], "notes": m["notes"][:3],
                              "pipeline": m["pipeline"][:4], "contacts": len(m["contacts"]),
                              "due": [c for c in m["contacts"] if c.get("status") in ("due", "overdue")][:3]} for m in mem],
                    "pending": [u for u in user.get("team", []) if u not in {m["username"] for m in mem}]})


@app.get("/api/team/search")
@login_required
def api_team_search(user):
    q = str(request.args.get("q") or "").strip()[:80]
    email = request.args.get("email")
    return jsonify({"hits": who_knows(team_memory(user), email=email, names=[q])})


@app.post("/api/notes/check")
@login_required
def api_notes_check(user):
    """Before saving a note, find which founders and companies it mentions that teammates already know."""
    text = str((request.get_json(silent=True) or {}).get("text") or "").lower()
    mem = team_memory(user)
    names = set()
    for m in mem:
        names.update(x for p in m["pipeline"] for x in (p.get("company"), p.get("founder")) if x)
        names.update(c["name"] for c in m["contacts"] if c.get("name") and "@" not in c["name"])
    mentioned = [n for n in names if len(n) >= 3 and re.search(rf"\b{re.escape(n.lower())}\b", text)]
    return jsonify({"hits": who_knows(mem, names=mentioned) if mentioned else []})


@app.get("/api/notes")
@login_required
def api_notes(user):
    return jsonify({"notes": notes_of(user["username"], 20)})


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
    "Gmail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
    "Google Calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
    "Google Chat": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
}
PAGE = (Path(__file__).parent / "page.html").read_text()


@app.get("/")
def index():
    data = {"icons": ICONS, "interview": INTERVIEW, "google": g.CONFIGURED, "prism": guard.live(),
            "llm": ix.LLM_CLIENT.live, "store": "Vercel Blob" if store.LIVE else "local files"}
    blob = json.dumps(data).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob)


if __name__ == "__main__":
    app.run(debug=True, port=8000)
