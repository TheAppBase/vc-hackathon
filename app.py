"""Last Touch (Track 1, sourcing). Flask app for Vercel (exports `app`).

Every person at the firm signs in with a username and password and gets a personal AI agent. After an event or a meeting
they record a quick note (dictated or typed); the agent writes it up, reads out the founder, company, where they met and
the next step, checks the company on akta.pro, keeps track of who they have been talking to and what's overdue, ranks
founders with their own criteria, and shares it all with their team through the firm memory on Vercel Blob.
Notes save without approval (secrets are redacted). In Auto mode PRISM watches the agent and reviews each note; in Ask
mode nothing is sent to PRISM.

Run locally: flask --app app run
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import contextlib  # noqa: E402
import datetime as dt  # noqa: E402
import math  # noqa: E402
import hashlib  # noqa: E402
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
            key = env("SECRET_KEY")
            if not key and store.LIVE:
                try:
                    key = store.shared_secret()
                except Exception as e:  # storage hiccup: don't break every page, and try the store again next request
                    print(scrub(f"session key: store unavailable ({e}); using a temporary key"), flush=True)
                    return super().get_signing_serializer(type("Tmp", (), {"secret_key": _TMP_KEY, "config": app.config})())
            app.secret_key = key or _TMP_KEY  # local development without storage: one key per process
        return super().get_signing_serializer(app)


_TMP_KEY = secrets.token_hex(32)


app.session_interface = SharedKeySessions()


@app.before_request
def _needs_storage():
    # On Vercel, a missing Blob store would mean accounts on one server's temporary disk that vanish: refuse instead.
    if store.ON_VERCEL and not store.LIVE and request.path.startswith("/api/") and request.path != "/api/health":
        return jsonify({"error": "storage", "message": "Storage isn't set up: connect a private Vercel Blob store to this "
                                                        "project and redeploy."}), 503
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))

USER_RE = re.compile(r"^(?!.*\.\.)[a-z0-9][a-z0-9_.-]{1,29}$")
MIN_PASSWORD = 8
ATTEMPT_SECONDS = 10  # one password attempt per username per 10 seconds, and every attempt takes 10 seconds
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


def tz_offset() -> dt.timedelta:
    """The user's UTC offset, from the browser (X-TZ-Offset, minutes as in getTimezoneOffset, so west is positive)."""
    try:
        minutes = int(request.headers.get("X-TZ-Offset", ""))
    except (ValueError, RuntimeError):
        return dt.timedelta(0)
    return -dt.timedelta(minutes=minutes) if -14 * 60 <= minutes <= 14 * 60 else dt.timedelta(0)


def local_dt(iso: str) -> dt.datetime:
    return _ts(iso) + tz_offset()


def local_today() -> dt.date:
    """The user's own date, so "due today" matches their day, not the server's UTC day."""
    utc = dt.datetime.now(dt.timezone.utc)
    try:
        if request.headers.get("X-TZ-Offset"):
            return (utc + tz_offset()).date()
        d = dt.date.fromisoformat(request.headers.get("X-Local-Date", "")[:10])
    except (ValueError, RuntimeError):
        return utc.date()
    return d if abs((d - utc.date()).days) <= 1 else utc.date()


def nice_date(iso: str) -> str:
    s = str(iso)
    # A date alone ("2026-10-30", a due date) is already the user's own day; only timestamps move to their time zone.
    d = dt.date.fromisoformat(s) if len(s) == 10 else local_dt(s)
    return d.strftime("%a %b %d").replace(" 0", " ")


# --------------------------------------------------------------------------- users
def norm(u) -> str:
    return str(u or "").strip().lstrip("@").lower()


def load_user(u: str) -> dict | None:
    """The account plus its password hash and version, which live in their own blob (auth/<username>.json): only sign-up
    and a password change write it, so a settings, team or profile save can never undo a password change."""
    if not USER_RE.match(u or ""):
        return None
    user, auth = store.get_many([f"users/{u}.json", f"auth/{u}.json"])
    if not user:
        return None
    newer = user.get("password") and (not auth or user.get("pw_ver", 0) > auth.get("pw_ver", 0))
    if auth and not newer:
        user.update(password=auth.get("password"), pw_ver=auth.get("pw_ver", 0), _auth=True)
    elif newer:  # from before passwords had their own blob, or a sign-up whose auth/ write failed: move it there
        with contextlib.suppress(Exception):
            store.put(f"auth/{u}.json", {"password": user["password"], "pw_ver": user.get("pw_ver", 0)}, overwrite=bool(auth))
            user["_auth"] = True
    return user


def save_user(user: dict):
    """Write the account (name, profile, team, settings). The password is never written here."""
    drop = ("_auth",) + (("password", "pw_ver") if user.get("_auth") else ())  # without its own blob yet, keep it here
    store.put(f"users/{user['username']}.json", {k: v for k, v in user.items() if k not in drop})


def save_password(user: dict, pw: str):
    """Set a new password. The version goes up, which signs out every other device."""
    path = f"auth/{user['username']}.json"
    cur = store.get(path) or {}
    user["password"] = generate_password_hash(pw)
    user["pw_ver"] = max(user.get("pw_ver", 0), cur.get("pw_ver", 0)) + 1
    store.put(path, {"password": user["password"], "pw_ver": user["pw_ver"]})
    user["_auth"] = True


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


def chat_sid(user: dict) -> str:
    """The PRISM session id: one per open tab (the browser sends X-Chat-Id, new on each page load and after 30 minutes
    idle), so each conversation is its own PRISM session."""
    cid = request.headers.get("X-Chat-Id", "")
    if re.fullmatch(r"[A-Za-z0-9-]{8,40}", cid):
        return f"lt-{user['username']}-{cid[:12]}"
    return session.get("sid") or f"lt-{user['username']}"


def start_session(user: dict):
    u = user["username"]
    session.clear()
    session.permanent = True
    session["u"] = u
    session["pv"] = user.get("pw_ver", 0)
    session["sid"] = f"lt-{u}-{secrets.token_hex(6)}"  # PRISM session when the browser sends no X-Chat-Id


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
        ix.TRACER.tool(name, aid, chat_sid(user), tool, inp, out, int((time.time() - t0) * 1000), status)


@app.teardown_request
def _flush_traces(exc):
    ix.TRACER.flush()


@app.errorhandler(Exception)
def _error(e):
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        return e
    with contextlib.suppress(Exception):
        user = current()
        if user:
            trace_tool(user, request.path, request.method, scrub(f"{type(e).__name__}: {e}")[:300], time.time(), "error")
    print(scrub(f"error on {request.path}: {type(e).__name__}: {e}"), flush=True)
    return jsonify({"error": "Something went wrong on our side. Try again."}), 500


# --------------------------------------------------------------------------- profile
FOCUS_FILLER = {"and", "or", "plus", "the", "a", "an", "in", "of", "on", "to", "for", "with", "mostly", "mainly",
                "seed", "pre", "preseed", "pre-seed", "series", "b", "c", "stage", "stages", "early", "earlier", "late", "later",
                "growth", "idea", "generalist", "agnostic", "sector", "sectors", "startups", "startup", "companies", "company",
                "founders", "anything", "everything", "all", "any", "etc", "focus", "invest", "i", "we", "tech", "technology",
                "at", "from", "through", "across", "into", "by", "via", "up", "re", "ve", "investor", "investors", "investing",
                "usually", "typically", "primarily", "broadly", "round", "rounds", "fund", "vc", "post", "first", "multi",
                "check", "checks", "but", "no", "not", "except", "lead", "leads", "agnostic", "neutral"}
GENERALIST = re.compile(r"\bgeneralists?\b|(?<!stage[- ])\bagnostic\b|\b(?:any|all|every) sectors?\b|\banything\b|\beverything\b|"
                        r"sector[- ]neutral", re.I)


def extra_focus(focus: str) -> list[str]:
    """Focus words outside the known sectors ("robotics", "edtech"), so they count as the person's thesis."""
    if GENERALIST.search(focus):
        return []  # "generalist at seed", "sector-agnostic": every sector
    words = re.findall(r"[a-z][a-z0-9+&]*", focus.lower())  # "early-stage" -> "early", "stage"
    known = ("devtool", "dev tool", "infra", "developer", "consumer", "cpg", "brand", "food", "retail", "fintech", "payments",
             "finance", "climate", "health")
    return list(dict.fromkeys(w for w in words if w not in FOCUS_FILLER and w != "ai" and len(w) > 1
                              and not any(k in w for k in known)))[:6]


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
    extra = extra_focus(focus)
    if GENERALIST.search(focus):
        sectors = list(ALL_SECTORS)  # "generalist, mostly fintech" still considers every sector
    sectors = sectors or ([] if extra else list(ALL_SECTORS))  # "robotics and edtech" is a focus, not "everything"
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
        "sectors": sectors, "extra_focus": extra, "stages": stages, "signal": str(a.get("signal") or "")[:100], "style": style, "weights": w,
        "nos": str(a.get("nos") or "No hard no's")[:80],
        "weights_text": ", ".join(f"{LABEL[k]} {v}%" for k, v in sorted(w.items(), key=lambda kv: -kv[1]) if v),
    }


# --------------------------------------------------------------------------- notes -> founders
def redact_fields(fields: dict, found: list | None = None) -> dict:
    """Secrets must not survive in any extracted field either (next step, background, one-liner...). What was removed
    is added to `found`."""
    found = found if found is not None else []

    def clean(x):
        if not isinstance(x, str):
            return x
        x, why = guard.redact(x)
        found.extend(why)
        return x

    return {k: [clean(x) for x in v] if isinstance(v, list) else clean(v) for k, v in fields.items()}


def extract(user: dict, text: str, when: dt.date) -> tuple[dict, str, str, list[str]]:
    """Rewrite a note professionally and read the founder, company, event and next step out of it.

    Returns (fields, reader, rewritten, secrets the AI's output still had and that were removed). Nicrron when configured; otherwise keyword rules and a light tidy-up. The AI sees
    the whole note; a rewrite that is far shorter than the note (facts dropped) or far longer is not used."""
    if ix.LLM_CLIENT.live:
        name, aid = agent_of(user)
        got = ix.LLM_CLIENT.json(engine.NOTE_SYSTEM.format(today=when.isoformat(), weekday=when.strftime("%A")), text[:guard.MAX_NOTE],
                                 name, session_id=chat_sid(user), trace=traced(user), agent_id=aid,
                                 max_tokens=min(8000, max(900, len(text) // 3 + 400)))
        if isinstance(got, dict):
            rewritten = str(got.pop("note", "") or "").strip()
            if not rewritten or len(rewritten) > 2 * len(text) + 300 or (len(text) > 200 and len(rewritten) < 0.4 * len(text)):
                rewritten = engine.tidy(text)
            rewritten, found = guard.redact(rewritten)
            return redact_fields(engine.clean_note(got), found), "Nicrron", rewritten, found
    return redact_fields(engine.clean_note(engine.heuristic_note(text, when))), "keyword rules", engine.tidy(text), []


ROUTES = {"save_note", "conversations", "rank", "source", "lookup", "who_knows", "team", "help", "chat"}


@app.post("/api/route")
@login_required
def api_route(user):
    """Decide what a typed or dictated message asks for. The LLM decides when configured; phrase matching otherwise."""
    t0 = time.time()
    text = guard.redact(str((request.get_json(silent=True) or {}).get("text") or "").strip()[:2000])[0]  # before the AI or PRISM
    if not text:
        return jsonify({"intent": "help", "query": "", "reply": "", "engine": "none"})
    if ix.LLM_CLIENT.live:
        name, aid = agent_of(user)
        focus = (user.get("profile") or {}).get("focus") or "not set yet"
        got = ix.LLM_CLIENT.json(engine.ROUTER_SYSTEM.format(focus=focus), text, name, session_id=chat_sid(user),
                                 trace=traced(user), agent_id=aid, max_tokens=300, fast=True)
        if isinstance(got, dict) and isinstance(got.get("intent"), str) and got["intent"] in ROUTES:
            q, rp = got.get("query"), got.get("reply")
            out = {"intent": got["intent"], "query": q.strip()[:120] if isinstance(q, str) else "",
                   "reply": rp.strip()[:800] if isinstance(rp, str) else "", "engine": "Nicrron"}
            trace_tool(user, "route", text[:300], json.dumps(out)[:300], t0)
            return jsonify(out)
    out = {**engine.route_rules(text), "reply": "", "engine": "rules"}
    trace_tool(user, "route", text[:300], json.dumps(out)[:300], t0)
    return jsonify(out)


INDEX_SIZE = 300


def _compact(n: dict) -> dict:
    return {k: n.get(k) for k in ("id", "text", "original", "created_at", "local_date", "extracted", "akta") if n.get(k) is not None}


def notes_from(u: str, paths: list[str], index: dict | None, limit: int) -> list[dict]:
    """A user's newest notes from their index blob; any note the index is missing is read and the index repaired."""
    paths = paths[-limit:]
    have = {n["id"]: n for n in (index or {}).get("notes", []) if isinstance(n, dict) and n.get("id")}
    ids = [p.rsplit("/", 1)[-1].removesuffix(".json") for p in paths]
    missing = [p for p, i in zip(paths, ids) if i not in have]
    if missing:
        for n in store.get_many(missing):
            if n and n.get("id"):
                have[n["id"]] = _compact(n)
        keep = sorted(have.values(), key=lambda n: (n.get("created_at") or "", n.get("id") or ""))[-INDEX_SIZE:]
        with contextlib.suppress(Exception):
            store.put(f"index/{u}.json", {"notes": keep})
    return [have[i] for i in reversed(ids) if i in have]


def notes_of(u: str, limit: int = 200) -> list[dict]:
    """A user's notes, newest first: one list call and one index read instead of one read per note."""
    return notes_from(u, store.list_paths(f"notes/{u}/"), store.get(f"index/{u}.json"), limit)


def index_add(u: str, note: dict):
    """Add a new note to the user's index (if two saves race, the next read repairs the index)."""
    idx = store.get(f"index/{u}.json") or {"notes": []}
    notes = [n for n in idx.get("notes", []) if isinstance(n, dict) and n.get("id") != note["id"]] + [_compact(note)]
    store.put(f"index/{u}.json", {"notes": notes[-INDEX_SIZE:]})


def _key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def same_person(a: str, b: str) -> bool:
    """"maya" and "maya okafor" are the same person (at the same company); "maya okafor" and "tom lee" are not."""
    ta, tb = a.split(), b.split()
    return ta == tb or (min(len(ta), len(tb)) == 1 and bool(ta) and bool(tb) and ta[0] == tb[0])


CO_SUFFIX = {"ai", "inc", "labs", "lab", "hq", "io", "app", "co", "corp", "ltd", "llc", "the"}


def same_company(a: str, b: str) -> bool:
    """"quillstack ai", "quillstack" and "quill stack" are one company; "quillstack" and "kite" are not."""
    ca = "".join(w for w in a.split() if w not in CO_SUFFIX) or a.replace(" ", "")
    cb = "".join(w for w in b.split() if w not in CO_SUFFIX) or b.replace(" ", "")
    return ca == cb or (min(len(ca), len(cb)) >= 4 and (ca.startswith(cb) or cb.startswith(ca)))


def _fuller(a: str | None, b: str | None) -> str | None:
    return a if (a and (not b or len(a.split()) >= len(b.split()))) else b


def _merge(rec: dict, other: dict):
    """Fold record `other` into `rec` (one note linked them, e.g. "Maya Okafor from Quillstack")."""
    later = other if other["last_touch"] > rec["last_touch"] else rec
    for f in ("company", "sector", "stage", "one_liner", "raising_usd", "event", "akta"):
        rec[f] = rec.get(f) or other.get(f)
    rec["founder"] = _fuller(rec.get("founder"), other.get("founder"))
    rec["metrics"] = {**other["metrics"], **rec["metrics"]}
    rec["background"] = list(dict.fromkeys(rec["background"] + other["background"]))[:6]
    earlier = rec if later is other else other
    src = later if later.get("next_step") else earlier  # the latest open step, not a newer record's "nothing"
    rec["next_step"], rec["next_due"] = src.get("next_step"), src.get("next_due")
    rec["first_met"], rec["last_touch"] = min(rec["first_met"], other["first_met"]), max(rec["last_touch"], other["last_touch"])
    rec["notes"] = sorted(rec["notes"] + other["notes"], key=lambda n: (n.get("created_at") or "", n.get("id") or ""))


def founders_from(notes: list[dict]) -> list[dict]:
    """Merge notes about the same founder (or company) into one record. Later notes update earlier ones.

    A shared company never joins two different founders (co-founders stay separate), and a note that names both a
    founder and a company links their records."""
    recs, index = [], {}
    for n in sorted(notes, key=lambda n: (n.get("created_at") or "", n.get("id") or "")):
        when = dt.date.fromisoformat(n["local_date"]) if n.get("local_date") else local_dt(n.get("created_at")).date()
        x = n.get("extracted") or engine.clean_note(engine.heuristic_note(n.get("original") or n.get("text", ""), when))
        fk, ck = _key(x.get("founder")), _key(x.get("company"))
        if not (fk or ck):
            continue
        hits = []
        for k, is_company in ((fk, False), (ck, True)):
            r = index.get(k) if k else None
            if r is None or any(r is h for h in hits):
                continue
            if is_company and fk and r.get("founder") and not same_person(_key(r["founder"]), fk):
                continue
            if not is_company and ck and " " not in fk and r.get("company") and not same_company(_key(r["company"]), ck):
                continue  # "Maya from Kite" is not the "Maya" from Quillstack
            hits.append(r)
        if not hits:
            rec = {**engine.EMPTY_NOTE, "metrics": {}, "background": [], "notes": [], "first_met": n.get("created_at"),
                   "last_touch": n.get("created_at")}
            recs.append(rec)
        else:
            rec = hits[0]
            for other in hits[1:]:
                _merge(rec, other)
                recs.remove(other)
                for k, v in list(index.items()):
                    if v is other:
                        index[k] = rec
        for k in (fk, ck):
            if k:
                index[k] = rec
        for f in ("company", "sector", "stage", "one_liner", "raising_usd"):
            if x.get(f):
                rec[f] = x[f]
        rec["founder"] = _fuller(rec.get("founder"), x.get("founder"))  # "Maya" never replaces "Maya Okafor"
        rec["event"] = rec.get("event") or x.get("event")
        rec["metrics"].update(x.get("metrics") or {})
        rec["background"] = list(dict.fromkeys(rec["background"] + (x.get("background") or [])))[:6]
        # The latest note sets what's next. A note with no next step keeps the open one, unless it says it's done.
        if x.get("next_step"):
            rec["next_step"], rec["next_due"] = x["next_step"], x.get("next_due")
        elif x.get("step_done"):
            rec["next_step"], rec["next_due"] = None, None
        rec["last_touch"] = max(rec.get("last_touch") or "", n.get("created_at") or "")
        rec["notes"].append({"id": n.get("id"), "text": n.get("text", ""), "original": n.get("original"),
                             "created_at": n.get("created_at")})
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
        age = (today - local_dt(rec["last_touch"]).date()).days
        return ("overdue", f"Open for {age} days") if age > 7 else ("due", "Open")
    return "warm", "No next step"


def describe_rec(rec: dict) -> str:
    who = rec.get("founder") or rec.get("company")
    co = f" ({rec['company']})" if rec.get("founder") and rec.get("company") else ""
    return f"{who}{co}"


# --------------------------------------------------------------------------- firm memory (team)
def team_memory(user: dict) -> list[dict]:
    """What each teammate's agent shares. Only people who added each other share. Read once per request."""
    cache = g.setdefault("team_memory", {})
    if user["username"] in cache:
        return cache[user["username"]]
    team = user.get("team", [])
    mates = [m for m in store.get_many([f"users/{m}.json" for m in team if USER_RE.match(m)])
             if m and user["username"] in m.get("team", [])]
    names = [m["username"] for m in mates]
    listings = store.list_many([f"notes/{u}/" for u in names])
    indexes = store.get_many([f"index/{u}.json" for u in names])
    out = []
    for mate, paths, idx in zip(mates, listings, indexes):
        notes = notes_from(mate["username"], paths, idx, 100)
        out.append({"username": mate["username"], "name": mate["name"], "first": first(mate["name"]),
                    "focus": (mate.get("profile") or {}).get("focus"), "notes": notes, "founders": founders_from(notes)})
    cache[user["username"]] = out
    return out


def with_team_facts(rec: dict, mem: list[dict]) -> dict:
    """Fill what the user's notes don't say (sector, stage, traction, raise) from teammates' notes on the same founder."""
    fk, ck = _key(rec.get("founder")), _key(rec.get("company"))
    out, filled = {**rec, "metrics": dict(rec.get("metrics") or {})}, []
    for mate in mem:
        for r in mate["founders"]:
            # Only a sure match: same company, or the same full name (a first name alone may be someone else).
            if not ((ck and ck == _key(r.get("company"))) or (fk and " " in fk and fk == _key(r.get("founder")))):
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
    """Which teammates' agents have this founder or company. A match on a single first name only is reported as a
    possible match, because it may be a different person."""
    keys = [_key(n) for n in names if n and len(_key(n)) >= 3]
    if not keys:
        return []
    hits = []
    for mate in mem:
        hit = None
        for rec in mate["founders"]:
            fk, ck = _key(rec.get("founder")), _key(rec.get("company"))
            if fk in keys or ck in keys:
                sure = ck in keys or " " in fk
                met = f" at {rec['event']}" if rec.get("event") else ""
                nxt = f" {mate['first']}'s next step: {rec['next_step']}." if rec.get("next_step") else " No open next step."
                lead = (f"{mate['first']} met {describe_rec(rec)}" if sure else
                        f"{mate['first']} also noted someone called {describe_rec(rec)} (could be a different person)")
                hit = {"who": mate["first"], "username": mate["username"], "sure": sure,
                       "text": f"{lead}{met} on {nice_date(rec['first_met'])}, last noted {nice_date(rec['last_touch'])}.{nxt}"}
                break
        if not hit:
            for n in mate["notes"]:
                low = _key(f"{n.get('text')} {n.get('original') or ''}")
                if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys if " " in k or len(keys) == 1):
                    hit = {"who": mate["first"], "username": mate["username"], "sure": False,
                           "text": f"{mate['first']}'s note from {nice_date(n['created_at'])} mentions it: \"{n['text'][:220]}\""}
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
    original, redacted = guard.redact(raw[:guard.MAX_NOTE])
    extracted, reader, text, also = extract(user, original, local_today())
    redacted = list(dict.fromkeys(redacted + also))
    mode = mode_of(user)
    act = {"id": new_id(), "type": "save_note", "mode": mode, "created_at": now(), "summary": "Save a note to the firm memory",
           "payload": {"text": text, "original": original, "source": "dictation" if p.get("source") == "dictation" else "typed",
                       "extracted": extracted}}
    if redacted:
        act["redacted"] = redacted
    if len(raw) > guard.MAX_NOTE:
        act["truncated"] = f"Only the first {guard.MAX_NOTE:,} characters were kept."
    if mode == "auto":
        name, aid = agent_of(user)
        act["review"] = guard.review(name, aid, chat_sid(user), act)
    if act.get("review", {}).get("decision") == "block":
        act["status"], act["reason"] = "blocked", f"PRISM blocked this note: {act['review']['reason']}"
    else:
        note = {"id": act["id"], "text": text, "original": original, "source": act["payload"]["source"], "extracted": extracted,
                "created_at": act["created_at"], "local_date": local_today().isoformat()}
        if ix.AKTA.live and extracted.get("company"):
            t1, ix.AKTA.error = time.time(), None
            try:  # enrichment must never stop a note from saving
                note["akta"] = ix.AKTA.lookup(extracted["company"], timeout=6)  # free Company Search; no credits per note
            except Exception as e:
                ix.AKTA.error, note["akta"] = scrub(f"akta.pro lookup failed: {e}")[:200], None
            trace_tool(user, "akta.lookup", extracted["company"], json.dumps(note.get("akta"))[:500], t1,
                       "error" if ix.AKTA.error and not (note.get("akta") or {}).get("match") else "success")
        try:
            store.put(f"notes/{user['username']}/{act['id']}.json", note)
            act["status"] = "done"
            with contextlib.suppress(Exception):
                index_add(user["username"], note)
            act["akta"] = note.get("akta")
        except Exception as e:
            act["status"], act["reason"] = "failed", scrub(f"Could not save: {e}")[:180]
        if act["status"] == "done" and ix.ROUNDTABLE.live:
            act["roundtable"] = False
            t2, rid = time.time(), None
            title = describe_rec(extracted) if (extracted.get("founder") or extracted.get("company")) else "Note"
            with contextlib.suppress(Exception):
                rid = ix.ROUNDTABLE.save_note(f"Last Touch · {title}", note_markdown(user, note), f"lt-{user['username']}-{act['id']}")
            act["roundtable"] = bool(rid)
            trace_tool(user, "roundtable.save_note", title, str(rid or ix.ROUNDTABLE.error or "failed")[:300], t2,
                       "success" if rid else "error")
    if act.get("review") and act["review"]["decision"] == "unknown" and act["status"] == "done":
        act["reason"] = f"Saved. PRISM could not decide ({act['review']['reason']})."
    log_action(user, act)
    hits = []
    if act["status"] == "done":
        with contextlib.suppress(Exception):
            hits = who_knows(team_memory(user), [extracted.get("founder"), extracted.get("company")])
    trace_tool(user, "save_note", text[:500], f"{act['status']}; read with {reader}; {len(hits)} teammate matches", t0,
               "error" if act["status"] == "failed" else "success")
    return jsonify({**view(act), "hits": hits, "reader": reader})


def view(act: dict) -> dict:
    out = {k: act.get(k) for k in ("id", "type", "summary", "payload", "status", "reason", "review", "mode",
                                   "created_at", "redacted", "truncated", "akta", "roundtable")}
    if out.get("review"):
        out["review"] = {k: v for k, v in out["review"].items() if k != "raw"}  # the raw evaluation stays in the log
    return out


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


TRIES_PER_MINUTE = 6  # per account, from devices that have never signed in to it
DEVICE_COOKIE = "lt_device"


def client_key() -> str:
    """Which device is trying (hashed IP; Vercel puts the caller's address in x-real-ip / x-forwarded-for)."""
    ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.remote_addr or "?"
    return hashlib.sha256(("lt-ip:" + ip).encode()).hexdigest()[:16]


def _device_signer():
    from itsdangerous import URLSafeTimedSerializer

    app.session_interface.get_signing_serializer(app)  # makes sure the shared signing key is loaded
    return URLSafeTimedSerializer(app.secret_key, salt="lt-device") if app.secret_key else None


def _device() -> dict:
    s = _device_signer()
    try:
        d = s.loads(request.cookies.get(DEVICE_COOKIE, ""), max_age=365 * 86400) if s else {}
    except Exception:
        return {}
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) else {}


def _device_tag(user: dict) -> str:
    return f"{user['username']}:{user.get('pw_ver', 0)}"  # a password change ends every browser's "known" status


def known_device(user: dict) -> str | None:
    """This browser's id if it has signed in to this account with its current password (a signed cookie set at
    sign-in), so strangers' tries on the account never use up the owner's tries."""
    d = _device()
    tags = d.get("users") or []
    legacy = user.get("pw_ver", 0) <= 1 and user["username"] in tags  # remembered before tags existed, never changed since
    return d["id"] if _device_tag(user) in tags or legacy else None


def remember_device(resp, user: dict):
    s = _device_signer()
    if not s:
        return resp
    d = _device()
    users = [x for x in d.get("users") or [] if str(x).split(":")[0] != user["username"]][-4:] + [_device_tag(user)]
    resp.set_cookie(DEVICE_COOKIE, s.dumps({"id": d.get("id") or secrets.token_hex(8), "users": users}), max_age=365 * 86400,
                    httponly=True, samesite="Lax", secure=bool(os.getenv("VERCEL")))
    return resp


def _reserve(path: str, t: float) -> bool:
    """Create the reservation blob. Every try deletes its own after ATTEMPT_SECONDS; one much older than that belongs to
    a request that died, and only then is it replaced (deleting a live one would let two tries through at once)."""
    for _ in range(2):
        try:
            store.put(path, {"t": t}, overwrite=False)
            return True
        except store.Exists:
            cur = store.get(path)
            if cur is None:
                continue  # its holder just finished: try once more
            if t - cur.get("t", 0) < 3 * ATTEMPT_SECONDS:
                return False
            mark = f"{path[:-5]}.x{cur.get('t', 0)}.json"
            try:  # only the one request that claims this stale blob's takeover marker may delete it
                store.put(mark, {"t": t}, overwrite=False)
            except store.Exists:
                if t - (store.get(mark) or {"t": t}).get("t", t) >= 3 * ATTEMPT_SECONDS:
                    store.delete(mark)  # that takeover failed or died before its delete: let the next try claim it
                return False
            store.delete(path)
    return False


def claim_attempt(u: str, slot: str, per_account: bool = True) -> tuple[int, str]:
    """Reserve one password try for this username from this device (or session) for ATTEMPT_SECONDS, and, for sign-in
    from an unknown device, one of the account's TRIES_PER_MINUTE. Returns (0, "") if reserved, else (seconds to wait,
    "device" or "account"). Reservations are blobs created with overwrite=False, so many requests sent at once still get
    one try."""
    t = time.time()
    path = f"attempts/{u}/{slot}.json"
    if not _reserve(path, t):
        age = t - (store.get(path) or {"t": t}).get("t", t)
        return max(1, math.ceil((ATTEMPT_SECONDS if age < ATTEMPT_SECONDS + 2 else 3 * ATTEMPT_SECONDS) - age)), "device"
    g.attempt = path
    if per_account:
        minute = int(t // 60)
        for k in range(TRIES_PER_MINUTE):
            try:
                store.put(f"attempts/{u}/m{minute}-{k}.json", {"t": t}, overwrite=False)
                return 0, ""
            except store.Exists:
                continue
        store.delete(path)
        g.attempt = None
        return max(1, 60 - int(t % 60)), "account"
    return 0, ""


def finish_attempt(started: float):
    """Make every attempt take ATTEMPT_SECONDS, right or wrong, then drop this try's reservation and old minute slots."""
    time.sleep(max(0.0, ATTEMPT_SECONDS - (time.time() - started)))
    path = g.get("attempt")
    if path:
        with contextlib.suppress(Exception):
            store.delete(path)
            u, minute = path.split("/")[1], int(time.time() // 60)
            for p in store.list_paths(f"attempts/{u}/m"):
                m = re.match(r".*/m(\d+)-\d+\.json$", p)
                if m and int(m.group(1)) < minute - 1:
                    store.delete(p)


def too_soon(wait: int, why: str = "device"):
    msg = (f"Too many sign-in tries on this account from other devices in the last minute. Wait {wait}s and try again."
           if why == "account" else f"One try every {ATTEMPT_SECONDS} seconds. Wait {wait}s and try again.")
    return jsonify({"error": msg, "retry_after": wait}), 429


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
    prev = store.get(f"auth/{u}.json") or {}  # left by a removed account: its open sessions must not carry over
    user = {"username": u, "name": name, "created_at": now(), "team": [], "settings": {"mode": "ask"},
            "password": generate_password_hash(pw), "pw_ver": prev.get("pw_ver", 0) + 1}
    try:
        # One write claims the username (even if two people sign up at once) and holds the hash, so a failure after it
        # never leaves an account nobody can sign in to.
        store.put(f"users/{u}.json", user, overwrite=False)
    except store.Exists:
        return jsonify({"error": f"@{u} is taken."}), 409
    with contextlib.suppress(Exception):  # then the hash's own blob; if this fails, load_user moves it there later
        store.put(f"auth/{u}.json", {"password": user["password"], "pw_ver": user["pw_ver"]})
        user["_auth"] = True
    start_session(user)
    return remember_device(jsonify({"user": public(user)}), user)


@app.post("/api/signin")
def api_signin():
    started = time.time()
    b = request.get_json(silent=True) or {}
    u, pw = norm(b.get("username")), str(b.get("password") or "")
    user = load_user(u)
    if not user or not user.get("password"):
        # Nothing to guess, so nothing to reserve or write; it still takes as long as a real try.
        time.sleep(max(0.0, ATTEMPT_SECONDS - (time.time() - started)))
        if user:  # accounts from before passwords can't be claimed with just a name (the name is visible to anyone)
            return jsonify({"error": "This account was made before passwords existed. Create a new account."}), 403
        return jsonify({"error": "Wrong username or password."}), 403
    dev = known_device(user)
    wait, why = claim_attempt(u, "d-" + dev, per_account=False) if dev else claim_attempt(u, client_key())
    if wait:
        return too_soon(wait, why)
    try:
        if not check_password_hash(user["password"], pw):
            return jsonify({"error": "Wrong username or password."}), 403
        start_session(user)
        return remember_device(jsonify({"user": public(user)}), user)
    finally:
        finish_attempt(started)


@app.post("/api/password")
@login_required
def api_password(user):
    b = request.get_json(silent=True) or {}
    pw = str(b.get("new") or "")
    if password_problem(pw):
        return jsonify({"error": password_problem(pw)}), 400
    # Keyed to this signed-in session (an id the server put in the signed cookie), so strangers' sign-in tries never block
    # the owner, and a stolen cookie still gets only one guess at the current password every 10 seconds.
    wait, why = claim_attempt(user["username"], "s-" + hashlib.sha256(session["sid"].encode()).hexdigest()[:16], per_account=False)
    if wait:
        return too_soon(wait, why)
    started = time.time()
    try:
        if user.get("password") and not check_password_hash(user["password"], str(b.get("current") or "")):
            return jsonify({"error": "Your current password is wrong."}), 403
        save_password(user, pw)
        trace_tool(user, "change_password", "(hidden)", "changed", started)
        start_session(user)  # keep this device signed in; every other device is signed out
        return remember_device(jsonify({"ok": True}), user)  # and the only browser still "known" for the new password
    finally:
        finish_attempt(started)


@app.post("/api/signout")
def api_signout():
    user = current()
    sid = chat_sid(user) if user else None
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
    if user.get("profile"):
        return jsonify({"error": "Your agent is already built from your interview."}), 409
    keys = {q["key"] for q in INTERVIEW}
    answers = {k: str(v)[:200] for k, v in (request.get_json(silent=True) or {}).items() if k in keys}
    user["answers"] = answers
    user["profile"] = build_profile(answers, user["name"])
    save_user(user)
    trace_tool(user, "learn_thesis", json.dumps(answers)[:500], user["profile"]["weights_text"], time.time())
    return jsonify({"user": public(user)})


@app.post("/api/settings")
@login_required
def api_settings(user):
    b = request.get_json(silent=True) or {}
    if b.get("mode") in ("ask", "auto"):
        if b["mode"] == "auto" and not guard.live():
            return jsonify({"error": f"Auto mode needs PRISM, which couldn't start: {ix.TRACER.error}" if ix.TRACER.key else
                            "Auto mode needs PRISM (PRISMTRACE_API_KEY on the server)."}), 400
        user.setdefault("settings", {})["mode"] = b["mode"]
        save_user(user)
        trace_tool(user, "set_mode", b["mode"], "Auto mode: PRISM watches the agent", time.time())
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
           "nicrron": {"live": ix.LLM_CLIENT.live, "model": ix.LLM_CLIENT.model, "fast_model": ix.LLM_CLIENT.fast_model,
                       "error": ix.LLM_CLIENT.error}, "akta": {"live": ix.AKTA.live, "paid": ix.AKTA.use_paid,
                    "error": f"last call returned HTTP {ix.AKTA.last_status}" if (ix.AKTA.last_status or 0) >= 400 else None},
           "roundtable": {"live": ix.ROUNDTABLE.live, "error": ix.ROUNDTABLE.error}, "prism": {"configured": guard.live()}}
    if guard.live() and traced(user):  # Ask mode never contacts PRISM, not even for a status check
        out["prism"].update(ix.TRACER.status())
    elif guard.live():
        out["prism"].update(credential="unused", detail="Ask mode: PRISM is off for you")
    elif ix.TRACER.key:
        out["prism"].update(credential="fail", detail=ix.TRACER.error or "PRISM could not start")
    return jsonify(out)


# --------------------------------------------------------------------------- agent skills (reads, always allowed)
def hard_no(profile: dict, rec: dict) -> str | None:
    """The user's hard no's from the interview, checked against what their notes and akta.pro say."""
    nos = (profile.get("nos") or "").lower()
    ak = rec.get("akta") or {}
    text = " ".join([f"{n['text']} {n.get('original') or ''}" for n in rec.get("notes", [])] + [str(rec.get("one_liner") or ""),
                    str((ak.get("industry") or {}).get("name") or ""), str((ak.get("match") or {}).get("description") or "")]).lower()
    if ("crypto" in nos or "token" in nos) and re.search(r"\b(crypto|cryptocurrency|web3|blockchain|nfts?|defi|ico|"
                                                         r"token (?:sale|presale|launch|raise|generation event)|tokenomics|memecoin)\b", text):
        return "Your hard no: token or crypto"
    if ("agenc" in nos or "service" in nos) and re.search(r"\b(agency|consultancy|services business|dev shop)\b", text):
        return "Your hard no: agency or services business"
    return None


AKTA_PAID_PER_DAY = int(env("AKTA_PAID_PER_DAY", default="30") or 30)
AKTA_PAID_FIRM_PER_DAY = int(env("AKTA_PAID_FIRM_PER_DAY", default="300") or 300)


def _take_slot(prefix: str, cap: int) -> str | None:
    """Claim one of `cap` numbered slots (blobs created with overwrite=False), so parallel calls can't overshoot.
    Returns the slot's path, or None when all are taken."""
    used = len(store.list_paths(prefix))
    for n in range(used, cap):
        try:
            store.put(f"{prefix}{n}.json", {"t": time.time()}, overwrite=False)
            return f"{prefix}{n}.json"
        except store.Exists:
            continue
    return None


def paid_ok(user: dict) -> bool:
    """Paid akta.pro calls per person and for the whole firm per day (server's UTC date: the browser can't reset it).
    The person's own cap is checked first, so someone over it never uses up the firm's."""
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    mine = _take_slot(f"usage/{user['username']}/{day}/", AKTA_PAID_PER_DAY)
    if not mine:
        return False
    if _take_slot(f"usage/_firm/{day}/", AKTA_PAID_FIRM_PER_DAY):  # "_firm" can't be a username
        return True
    with contextlib.suppress(Exception):
        store.delete(mine)  # the firm is out for today: give the person their slot back
    return False


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
    member = {"sectors": profile["sectors"], "stages": profile["stages"], "weights": profile["weights"],
              "extra_focus": profile.get("extra_focus") or []} if profile else None
    mem = team_memory(user)
    today = local_today()
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
            s = engine.score_member({**facts, "warm_intro": any(h.get("sure") for h in row["known"])}, member)
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
    asked = str(request.args.get("q") or "").strip()[:120]
    query = f"early-stage {asked} startups" if asked else f"startups in {profile['focus']}"
    if not re.search(r"seed|series|stage|idea", query, re.I):
        query += " at " + (" or ".join(s.replace("-", " ") for s in profile.get("stages", [])) or "early") + " stage"
    ix.AKTA.error = None
    if not ix.AKTA.use_paid:
        return jsonify({"error": "akta", "message": "Sourcing uses akta.pro List Generation, which is turned off (AKTA_USE_PAID=0)."}), 409
    if not paid_ok(user):
        return jsonify({"error": "akta", "message": "Today's akta.pro sourcing limit is used up (per person or for the firm). "
                                                    "Try again tomorrow."}), 429
    rows = ix.AKTA.companies_like(query, 10)
    mine = {k for r in founders_from(notes_of(user["username"])) for k in (_key(r.get("company")), _key(r.get("founder"))) if k}
    mem = team_memory(user)
    out = []
    for c in rows[:12]:
        name = ix._name(c)
        out.append({"name": name, "website": c.get("website") or c.get("domain"),
                    "description": str(c.get("product_category") or c.get("description") or "")[:200] or None,
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
    ak = ix.AKTA.lookup(q)  # free Company Search
    if ak.get("match") and ix.AKTA.use_paid and paid_ok(user):  # similar companies cost credits: only for a real match
        ak = ix.AKTA.lookup(q, similar=True)
    trace_tool(user, "akta.lookup", q, json.dumps(ak)[:500], t0, "error" if ix.AKTA.error and not ak.get("match") else "success")
    return jsonify({"query": q, "akta": ak, "line": akta_line(ak), "known": who_knows(team_memory(user), [q]),
                    "error": ix.AKTA.error if not ak.get("match") else None})


@app.get("/api/team")
@login_required
def api_team(user):
    out = []
    for m in user.get("team", []):
        mate = load_user(m)
        if mate:
            mutual = user["username"] in mate.get("team", [])
            out.append({"username": m, "name": mate["name"], "mutual": mutual, "ready": bool(mate.get("profile")) if mutual else None,
                        "focus": (mate.get("profile") or {}).get("focus") if mutual else None})
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
    trace_tool(user, "team_add", u, "added", time.time())
    return jsonify({"user": public(user), "mate": {"username": u, "name": mate["name"], "mutual": user["username"] in mate.get("team", [])}})


@app.delete("/api/team/<u>")
@login_required
def api_team_remove(user, u):
    u = norm(u)
    user["team"] = [x for x in user.get("team", []) if x != u]
    save_user(user)
    trace_tool(user, "team_remove", u, "removed", time.time())
    return jsonify({"user": public(user)})


@app.get("/api/team/activity")
@login_required
def api_team_activity(user):
    t0 = time.time()
    mem = team_memory(user)
    today = local_today()
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
    data = {"icons": ICONS, "interview": INTERVIEW, "prism": guard.live(),
            "prism_error": ix.TRACER.error if ix.TRACER.key and not guard.live() else None}
    blob = json.dumps(data).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob)


if __name__ == "__main__":
    app.run(debug=True, port=8000)
