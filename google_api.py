"""Google: OAuth sign-in, Gmail (read, and send only after the guard allows it), Google Calendar and Google Chat (read).

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from an OAuth client (type "Web application") in Google Cloud, with
the Gmail, Google Calendar and Google Chat APIs enabled. Authorized redirect URI: <your site>/auth/google/callback.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from urllib.parse import urlencode

import httpx

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
CONFIGURED = bool(CLIENT_ID and CLIENT_SECRET)
SCOPES = ["openid", "email", "profile",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/calendar.readonly",
          "https://www.googleapis.com/auth/chat.spaces.readonly",
          "https://www.googleapis.com/auth/chat.messages.readonly"]
SIGNIN_SCOPES = ["openid", "email"]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


class NotConnected(Exception):
    pass


# --------------------------------------------------------------------------- OAuth
def auth_url(redirect_uri: str, state: str, scopes: list[str], email_hint: str | None = None) -> str:
    q = {"client_id": CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes),
         "state": state, "include_granted_scopes": "true"}
    if scopes == SCOPES:
        q.update(access_type="offline", prompt="consent")
    else:
        q["prompt"] = "select_account"
    if email_hint:
        q["login_hint"] = email_hint
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q)


def exchange(code: str, redirect_uri: str) -> dict:
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"})
    r.raise_for_status()
    tok = r.json()
    info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15,
                     headers={"authorization": f"Bearer {tok['access_token']}"})
    info.raise_for_status()
    tok["userinfo"] = info.json()
    return tok


def revoke(refresh_token: str):
    try:
        httpx.post("https://oauth2.googleapis.com/revoke", params={"token": refresh_token}, timeout=10)
    except httpx.HTTPError:
        pass


def remember(username: str, access_token: str, expires_in: int):
    with _lock:
        _tokens[username] = (access_token, time.time() + int(expires_in) - 60)


def forget(username: str):
    with _lock:
        _tokens.pop(username, None)


def access_token(username: str, google: dict | None) -> str:
    if not google or not google.get("refresh_token"):
        raise NotConnected()
    with _lock:
        tok = _tokens.get(username)
    if tok and tok[1] > time.time():
        return tok[0]
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "refresh_token": google["refresh_token"],
        "grant_type": "refresh_token"})
    if r.status_code in (400, 401):
        raise NotConnected()
    r.raise_for_status()
    j = r.json()
    remember(username, j["access_token"], j.get("expires_in", 3600))
    return j["access_token"]


def _get(token: str, url: str, **params) -> dict:
    r = httpx.get(url, params=params, headers={"authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(f"{r.status_code}: {str(msg)[:200]}")
    return r.json()


# --------------------------------------------------------------------------- parsing helpers
def _hdr(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _when(msg: dict) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, dt.timezone.utc)


def _body(payload: dict) -> str:
    plain, rich = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), p.get("body", {}).get("data")
        if data and mt in ("text/plain", "text/html"):
            txt = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if mt == "text/plain" else rich).append(txt)
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    else:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    # Drop quoted replies so only the new message is scored.
    text = re.split(r"\nOn .{5,80}wrote:\n|\n-----Original Message-----", text)[0]
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


# --------------------------------------------------------------------------- Gmail
def gmail_threads(token: str, me: str, days: int = 60, limit: int = 40) -> list[dict]:
    """Recent person-to-person threads: who wrote last, when, subject and snippet."""
    q = f"newer_than:{days}d -category:promotions -category:social -category:updates -category:forums"
    ids = [t["id"] for t in _get(token, f"{GMAIL}/threads", q=q, maxResults=limit).get("threads", [])]

    def one(tid):
        t = _get(token, f"{GMAIL}/threads/{tid}", format="metadata",
                 metadataHeaders=["From", "To", "Cc", "Subject"])
        msgs = t.get("messages", [])
        if not msgs:
            return None
        last = msgs[-1]
        people = {}
        for m in msgs:
            for name, email in getaddresses([_hdr(m, "From"), _hdr(m, "To"), _hdr(m, "Cc")]):
                email = email.lower()
                if email and email != me and "noreply" not in email and "no-reply" not in email:
                    people.setdefault(email, name or email)
        from_email = parseaddr(_hdr(last, "From"))[1].lower()
        return {"id": tid, "subject": _hdr(msgs[0], "Subject") or "(no subject)", "snippet": html.unescape(last.get("snippet", "")),
                "when": _when(last).isoformat(), "last_from_me": from_email == me, "people": people, "count": len(msgs)}

    with ThreadPoolExecutor(8) as ex:
        return [t for t in ex.map(one, ids) if t and t["people"]]


def gmail_inbound(token: str, me: str, days: int = 30, limit: int = 20) -> list[dict]:
    """Recent inbound primary-inbox emails with their text, for sourcing."""
    q = f"in:inbox category:primary newer_than:{days}d -from:me"
    ids = [m["id"] for m in _get(token, f"{GMAIL}/messages", q=q, maxResults=limit).get("messages", [])]

    def one(mid):
        m = _get(token, f"{GMAIL}/messages/{mid}", format="full")
        name, email = parseaddr(_hdr(m, "From"))
        if not email or email.lower() == me:
            return None
        return {"id": mid, "thread": m.get("threadId"), "from_name": name or email, "from_email": email.lower(),
                "subject": _hdr(m, "Subject"), "text": _body(m.get("payload", {}))[:4000] or m.get("snippet", ""),
                "when": _when(m).isoformat()}

    with ThreadPoolExecutor(8) as ex:
        return [m for m in ex.map(one, ids) if m]


def gmail_send(token: str, sender: str, to: list[str], subject: str, body: str, thread_id: str | None = None) -> dict:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(to), subject
    msg.set_content(body)
    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    r = httpx.post(f"{GMAIL}/messages/send", json=payload, headers={"authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail send failed ({r.status_code}): {r.text[:200]}")
    return r.json()


# --------------------------------------------------------------------------- Calendar
def calendar_events(token: str, me: str, back_days: int = 30, ahead_days: int = 14) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    j = _get(token, "https://www.googleapis.com/calendar/v3/calendars/primary/events",
             timeMin=(now - dt.timedelta(days=back_days)).isoformat(), timeMax=(now + dt.timedelta(days=ahead_days)).isoformat(),
             singleEvents="true", orderBy="startTime", maxResults=250)
    out = []
    for e in j.get("items", []):
        if e.get("status") == "cancelled":
            continue
        people = {a["email"].lower(): a.get("displayName") or a["email"] for a in e.get("attendees", [])
                  if a.get("email") and a["email"].lower() != me and not a.get("resource") and not a["email"].endswith("calendar.google.com")}
        if not people:
            continue
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
        out.append({"id": e["id"], "title": e.get("summary") or "(untitled meeting)", "start": start, "people": people,
                    "link": e.get("htmlLink")})
    return out


# --------------------------------------------------------------------------- Chat
def chat_recent(token: str, limit_spaces: int = 20) -> list[dict]:
    spaces = _get(token, "https://chat.googleapis.com/v1/spaces", pageSize=limit_spaces).get("spaces", [])

    def one(sp):
        try:
            msgs = _get(token, f"https://chat.googleapis.com/v1/{sp['name']}/messages", pageSize=5, orderBy="createTime desc").get("messages", [])
        except RuntimeError:
            return None
        if not msgs:
            return None
        m = msgs[0]
        name = sp.get("displayName") or ("Direct message" if sp.get("spaceType") == "DIRECT_MESSAGE" else "Group chat")
        return {"space": name, "type": sp.get("spaceType"), "text": (m.get("text") or "")[:240],
                "sender": (m.get("sender") or {}).get("displayName") or "", "when": m.get("createTime")}

    with ThreadPoolExecutor(6) as ex:
        out = [c for c in ex.map(one, spaces) if c]
    return sorted(out, key=lambda c: c["when"] or "", reverse=True)
