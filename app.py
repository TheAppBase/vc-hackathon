"""Last Touch: a firm-wide team of deal agents. Flask UI for Vercel (exports `app`).

Run locally: flask --app app run   (the original Streamlit UI is in streamlit_app.py)
The page opens straight into the demo run on mock data. Every view is a URL: &tab=... picks a view,
&brief=<note id> picks a founder brief, &closed=1 marks the fund closed, &signals=1 checks Akta signals,
&live=1 uses live AI extraction when NICRRON_API_KEY is set.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import copy  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from html import escape  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

import markdown  # noqa: E402
from flask import Flask, request  # noqa: E402

import engine  # noqa: E402
import integrations as ix  # noqa: E402

app = Flask(__name__)

# Line icons (Lucide, ISC license), drawn with currentColor.
ICONS = {
    "today": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
    "channel": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "triage": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "briefs": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "watch": '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    "memory": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
    "filter": '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    "hot": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "park": '<circle cx="12" cy="12" r="10"/><line x1="10" y1="15" x2="10" y2="9"/><line x1="14" y1="15" x2="14" y2="9"/>',
    "warm": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "pass": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "signal": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
    "spark": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "route": '<path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="m15 15 6 6"/><path d="M4 4l5 5"/>',
    "logo": '<path d="M7 11V7a5 5 0 0 1 10 0v4"/><path d="M5 11h14v6a7 7 0 0 1-14 0z"/><path d="M12 15v3"/>',
}

TAG = {"hot": "Hot", "park": "Parked", "warm": "Warm", "pass": "Pass"}
TABS = [("today", "Today"), ("channel", "Firm channel"), ("triage", "Inbox triage"),
        ("briefs", "Briefs"), ("watch", "Watchlist"), ("memory", "Memory")]
HUE = {"ava": "#0a84ff", "ben": "#30b0c7", "cara": "#bf5af2"}

# Runs are deterministic on mock data, so one result per (live, signals, closed) is kept per server instance.
_STATES: dict = {}
_lock = threading.Lock()


def get_state(live: bool, signals: bool, closed: bool) -> dict:
    key = (live, signals, closed)
    with _lock:
        if key in _STATES:
            return _STATES[key]
    if signals or closed:
        state = copy.deepcopy(get_state(live, False, False))
        before = len(ix.TRACER.local)
        if signals:
            state = engine.watcher_signals(state)
        if closed:
            state = engine.watcher_fund_closed(state)
        state["traces"] = state["traces"] + copy.deepcopy(ix.TRACER.local[before:])
    else:
        state = engine.run_firm(live_ai=live)
        state["traces"] = copy.deepcopy(ix.TRACER.local)
    with _lock:
        _STATES[key] = state
    return state


def e(x) -> str:
    return escape("" if x is None else str(x))


def icon(name: str, size: int = 18) -> str:
    return (f"<svg class='ic' width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            f"stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>{ICONS[name]}</svg>")


def first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def avatar(member: dict, size: int = 40) -> str:
    initials = "".join(p[0] for p in member["name"].split()[:2])
    return (f"<span class='avatar' style='--hue:{HUE.get(member['id'], '#8e8e93')};width:{size}px;height:{size}px;"
            f"font-size:{size * 0.38:.0f}px'>{e(initials)}</span>")


def agent_avatar(agent: str, members: dict) -> str:
    who = agent.split("'")[0].split()[0]
    m = next((m for m in members.values() if first(m["name"]) == who), None)
    if m:
        return avatar(m, 34)
    return f"<span class='avatar sys'>{icon('route' if who == 'Router' else 'watch', 16)}</span>"


def tag(status: str) -> str:
    return f"<span class='tag {e(status)}'>{icon(status, 12)}{e(TAG.get(status, status))}</span>"


def link(params: dict, **changes) -> str:
    p = {**params, **changes}
    return "?" + urlencode({k: v for k, v in p.items() if v not in (None, "", 0, False)})


CSS = """
:root{--text:#1d1d1f;--muted:rgba(29,29,31,.62);--glass:rgba(255,255,255,.42);--glass-strong:rgba(255,255,255,.62);
--edge:rgba(255,255,255,.7);--line:rgba(29,29,31,.1);--shadow:0 10px 40px rgba(16,44,52,.18),0 1px 2px rgba(16,44,52,.08);
--blue:#0a84ff;--hot:#ff3b30;--park:#0a84ff;--warm:#ff9500;--pass:#8e8e93;--ok:#34c759;
--sky:linear-gradient(180deg,#a7cdd2 0%,#bcd6d3 30%,#cfdcd4 48%,#8fae9f 66%,#55776d 82%,#2f4a47 100%)}
@media (prefers-color-scheme:dark){:root{--text:#f5f5f7;--muted:rgba(245,245,247,.62);--glass:rgba(28,34,38,.48);
--glass-strong:rgba(36,42,46,.7);--edge:rgba(255,255,255,.16);--line:rgba(255,255,255,.1);--shadow:0 10px 40px rgba(0,0,0,.45);
--sky:linear-gradient(180deg,#0e2a33 0%,#16343a 35%,#1f3d3b 55%,#13282a 80%,#0a1516 100%)}}
*{box-sizing:border-box}html{background:#2f4a47}
body{margin:0;min-height:100vh;color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased;background:var(--sky);background-attachment:fixed;position:relative}
body:before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:
radial-gradient(60% 40% at 80% 70%,rgba(64,110,96,.55),transparent 70%),radial-gradient(50% 30% at 15% 75%,rgba(214,120,72,.28),transparent 70%),
radial-gradient(70% 45% at 50% 0%,rgba(255,255,255,.35),transparent 70%)}
a{color:inherit;text-decoration:none}.ic{flex:none;vertical-align:-3px}
.glass{background:var(--glass);border:1px solid var(--edge);border-radius:22px;box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.55);
-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--warm);box-shadow:0 0 0 2px rgba(255,149,0,.18)}
.dot.live{background:var(--ok);box-shadow:0 0 0 2px rgba(52,199,89,.2)}
.page{max-width:1240px;margin:0 auto;padding:26px 24px 140px}
.hero{padding:28px 6px 22px;display:grid;grid-template-columns:1fr auto;gap:20px;align-items:end}
.hero h1{font-size:40px;line-height:1.05;letter-spacing:-.025em;margin:0 0 6px;font-weight:700}
.hero p{margin:0;color:var(--muted);font-size:16px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:999px;font-weight:600;font-size:14px;
background:var(--glass-strong);border:1px solid var(--edge);box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 4px 14px rgba(16,44,52,.12);transition:transform .15s}
.btn:hover{transform:translateY(-1px)}.btn.primary{background:linear-gradient(180deg,#3d9bff,#0a84ff);color:#fff;border-color:rgba(255,255,255,.35)}
.btn.off{opacity:.45;pointer-events:none}.actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.metrics{display:grid;grid-template-columns:repeat(6,1fr);margin:0 0 16px;padding:14px 8px}
.metric{text-align:center;padding:2px 6px}.metric+.metric{border-left:1px solid var(--line)}
.metric .k{font-size:12.5px;color:var(--muted)}.metric .v{font-size:26px;font-weight:600;letter-spacing:-.02em}
.metric.hot .ic{color:var(--hot)}.metric.park .ic{color:var(--park)}.metric.warm .ic{color:var(--warm)}.metric.pass .ic{color:var(--pass)}
.section{padding:22px 24px}.section h2{margin:0 0 16px;font-size:19px;font-weight:600;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 16px}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.card{background:var(--glass-strong);border:1px solid var(--edge);border-radius:18px;padding:16px 18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.5)}
.who{display:flex;align-items:center;gap:12px;margin-bottom:10px}.who b{display:block;font-size:17px}.who small{color:var(--muted);font-size:12.5px;line-height:1.35;display:block}
.avatar{display:inline-grid;place-items:center;flex:none;border-radius:50%;color:#fff;font-weight:700;width:34px;height:34px;
background:linear-gradient(160deg,color-mix(in srgb,var(--hue) 70%,#fff),var(--hue));box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 2px 8px rgba(0,0,0,.15)}
.avatar.sys{background:linear-gradient(160deg,#aeaeb2,#636366)}
.act{padding:12px 0;border-top:1px solid var(--line)}.act .t{font-size:14.5px}.act .t b{font-weight:600}
.meta{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px;margin-top:4px}
.tag{display:inline-flex;align-items:center;gap:4px;padding:2px 9px 2px 7px;border-radius:999px;font-size:11.5px;font-weight:700;margin-right:6px;vertical-align:1px}
.tag.hot{color:var(--hot);background:rgba(255,59,48,.13)}.tag.park{color:var(--park);background:rgba(10,132,255,.13)}
.tag.warm{color:#c26a00;background:rgba(255,149,0,.16)}.tag.pass{color:var(--pass);background:rgba(142,142,147,.16)}
@media (prefers-color-scheme:dark){.tag.warm{color:#ffb340}}
.callout{display:flex;gap:10px;padding:12px 14px;border-radius:14px;margin:4px 0 8px;font-size:14px;background:rgba(255,149,0,.14);border:1px solid rgba(255,149,0,.3)}
.callout .ic{color:var(--warm);margin-top:2px}.callout.info{background:rgba(10,132,255,.12);border-color:rgba(10,132,255,.28)}.callout.info .ic{color:var(--blue)}
details summary{cursor:pointer;color:var(--blue);font-size:13px;margin-top:6px;list-style:none;display:inline-flex;align-items:center;gap:6px}
details summary::-webkit-details-marker{display:none}
pre{background:rgba(255,255,255,.35);border:1px solid var(--line);padding:12px 14px;border-radius:12px;white-space:pre-wrap;font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace;margin:8px 0;overflow-x:auto}
@media (prefers-color-scheme:dark){pre{background:rgba(0,0,0,.25)}}
.msg{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.msg:first-of-type{border-top:0}
.msg .time{color:var(--muted);font-size:12px;margin-left:6px;font-weight:400}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;white-space:nowrap}
.seg{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.seg a{padding:6px 12px;border-radius:999px;font-size:13px;background:var(--glass-strong);border:1px solid var(--edge)}
.seg a.on{background:linear-gradient(180deg,#3d9bff,#0a84ff);color:#fff;border-color:transparent}
.brief h3{margin:0 0 10px;font-size:20px}.brief p{margin:8px 0}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}ul.plain{list-style:none;padding:0;margin:10px 0}
ul.plain li{display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
code{font:12px ui-monospace,"SF Mono",Menlo,monospace}
.dock{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:20;display:flex;gap:6px;padding:8px;border-radius:26px}
.dock a{position:relative;display:grid;place-items:center;width:58px;height:58px;border-radius:16px;color:var(--text);transition:transform .18s}
.dock a:hover{transform:translateY(-6px) scale(1.08)}
.dock a .tile{display:grid;place-items:center;width:52px;height:52px;border-radius:14px;background:var(--glass-strong);border:1px solid var(--edge);
box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 4px 12px rgba(16,44,52,.15)}
.dock a.on .tile{background:linear-gradient(180deg,#3d9bff,#0a84ff);color:#fff;border-color:rgba(255,255,255,.4)}
.dock a.on:after{content:"";position:absolute;bottom:-5px;width:4px;height:4px;border-radius:50%;background:var(--text);opacity:.7}
.dock a .lbl{position:absolute;bottom:72px;white-space:nowrap;font-size:12px;padding:4px 10px;border-radius:8px;opacity:0;pointer-events:none;
background:var(--glass-strong);border:1px solid var(--edge);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);transition:opacity .15s}
.dock a:hover .lbl{opacity:1}
@media (max-width:980px){.metrics{grid-template-columns:repeat(3,1fr);row-gap:12px}.metric:nth-child(4){border-left:0}.cols,.split{grid-template-columns:1fr}.hero{grid-template-columns:1fr}.actions{justify-content:flex-start}}
@media (max-width:620px){.page{padding:16px 16px 120px}.hero{padding:22px 20px}.hero h1{font-size:32px}.hero{padding:18px 4px 16px}.section{padding:18px 16px}
.metric .v{font-size:22px}.dock{gap:2px;padding:6px;bottom:10px}
.dock a{width:50px;height:50px}.dock a .tile{width:44px;height:44px;border-radius:12px}}
"""


def tab_today(state, leads, members):
    cols = []
    for mid in ["ava", "ben", "cara"]:
        mem = members[mid]
        h = [f"<div class='card'><div class='who'>{avatar(mem)}<div><b>{e(mem['name'])}</b><small>{e(mem['role'])}</small></div></div>"]
        for n in leads:
            if n.get("prior") and n["prior"]["who"] == mid:
                h.append(f"<div class='callout'>{icon('alert', 16)}<div>You met <b>{e(n['founder'])}</b> ({e(n['company'])}) "
                         f"{e(n['prior']['days'])} days ago at {e(n['prior']['where'])}. No follow-up was sent. They just wrote to Ava.</div></div>")
        mine = [(n, a) for n in leads for a in n.get("actions", []) if a["who"] == mid and a["kind"] not in ("second_touch", "recheck")]
        if not mine:
            h.append("<p class='sub'>Nothing due.</p>")
        for n, a in mine:
            label = n.get("company") or n.get("founder")
            when = a.get("due") or (a.get("trigger", "").replace("_", " ").replace(" | ", " or "))
            h.append(f"<div class='act'><div class='t'>{tag(n['status'])}<b>{e(label)}</b>: {e(a['what'])}</div>"
                     f"<div class='meta'>{icon('clock', 13)}{e(when)}</div>")
            if a.get("draft"):
                h.append(f"<details><summary>{icon('send', 13)}Draft</summary><pre>{e(a['draft'])}</pre></details>")
            h.append("</div>")
        h.append("</div>")
        cols.append("".join(h))
    return section("today", "Today's actions", "What each person at the firm should do next. Drafts are never sent automatically.",
                   f"<div class='cols'>{''.join(cols)}</div>")


def tab_channel(state, members):
    out = []
    for f in state["feed"]:
        out.append(f"<div class='msg'>{agent_avatar(f['agent'], members)}<div><b>{e(f['agent'])}</b>"
                   f"<span class='time'>{e(f['t'])}</span><div>{e(f['text'])}</div></div></div>")
    return section("channel", "Firm channel", "What the agents told each other. Every note lives in the firm's shared memory (Roundtable).",
                   f"<div class='card'>{''.join(out)}</div>")


def tab_triage(state, leads, members):
    by_msg = {n.get("msg_id"): n for n in leads if n.get("msg_id")}
    rows = []
    for r in state["triage"]:
        n = by_msg.get(r["id"], {})
        if n.get("routed_to"):
            oe = f"{first(members[n['owner']]['name'])} → {first(members[n['routed_to']]['name'])}"
        else:
            oe = first(members[n["owner"]]["name"]) if n else "-"
        rows.append(f"<tr><td>{e(r['id'])}</td><td>{e(r['channel'])}</td><td>{e(r['from'])}</td><td>{e(r['preview'])}</td>"
                    f"<td>{'Lead' if r['triage'] == 'keep' else 'Dropped'}</td><td>{tag(n['status']) if n.get('status') else '-'}</td>"
                    f"<td>{e(oe)}</td><td>{e(n.get('score', ''))}</td><td>{e(n.get('reason') or r['reason'])}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in ["ID", "Channel", "From", "Message", "Decision", "Status", "Owner → evaluator", "Score", "Why"])
    return section("triage", "Inbox triage", "Every inbound message, the agent's decision and the reason.",
                   f"<div class='card scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def tab_briefs(state, leads, members, params):
    choices = [n for n in leads if n.get("status") != "pass"] + [n for n in leads if n.get("status") == "pass"]
    pick = next((n for n in choices if n["id"] == params.get("brief")), choices[0])
    seg = "".join(f"<a class='{'on' if n is pick else ''}' href='{link(params, brief=n['id'])}'>{e(n.get('company') or n.get('founder'))}</a>"
                  for n in choices)
    body = markdown.markdown(escape(engine.render_brief(pick, members), quote=False))
    return section("briefs", "Founder briefs", "Every claim is tagged with its source: [message id] = the founder's own message, "
                   "[firm memory] = Roundtable note history, [Akta] = outside data.",
                   f"<div class='seg'>{seg}</div><div class='card brief'>{tag(pick['status'])}{body}</div>")


def tab_watch(state, leads, params):
    closed = state["fund"]["status"] == "closed"
    btns = (f"<div class='actions' style='justify-content:flex-start;margin-bottom:14px'>"
            f"<a class='btn {'off' if closed else ''}' href='{link(params, closed=1, tab='watch')}'>{icon('flag', 16)}Mark {e(state['fund']['label'])} as closed</a>"
            f"<a class='btn' href='{link(params, signals=1, tab='watch')}'>{icon('signal', 16)}Check Akta signals</a></div>")
    watched = [n for n in leads if n["status"] == "park" or n.get("was_parked")]
    cards = []
    for n in watched:
        c = [f"<div class='card'><div class='t'>{tag(n['status'])}<b>{e(n.get('company'))}</b>: {e(n.get('one_liner', ''))}</div>"
             f"<p class='sub' style='margin:6px 0 0'>{e(n.get('reason', ''))}</p>"]
        for s in n.get("signals", []):
            c.append(f"<div class='callout info' style='margin-top:10px'>{icon('signal', 16)}<div>Signal: {e(s['text'])} [{e(s['source'])}]</div></div>")
        for act in n.get("actions", []):
            if act.get("draft") and act["kind"] == "reengage":
                c.append(f"<pre>{e(act['draft'])}</pre>")
        c.append("</div>")
        cards.append("".join(c))
    return section("watch", "Watchlist", "Parked founders resurface when the fund closes or a new signal appears.",
                   btns + ("".join(cards) or "<p class='sub'>No parked founders.</p>"))


def tab_memory(state, notes):
    mem = [{k: n.get(k) for k in ("id", "company", "founder", "status", "owner", "routed_to", "score", "touches")} for n in notes]
    traces = "".join(f"<li>{icon('signal', 15)}<span><b>{e(t['agent'])}</b>: {len(t['steps'])} steps"
                     + (f" · PRISM id <code>{e(t['prism_id'])}</code>" if t.get("prism_id") else " · local trace") + "</span></li>"
                     for t in state.get("traces", []))
    status = "".join(f"<li><i class='dot {'live' if mode == 'live' else ''}'></i><span>{e(name)}: {e(mode)}</span></li>"
                     for name, (mode, _err) in ix.status().items())
    return section("memory", "Memory", f"One shared note per founder, read and written by every agent. Run {e(state['run_id'])}.",
                   f"<div class='card' style='margin-bottom:14px'><b>Integrations</b><ul class='plain'>{status}</ul></div>"
                   f"<div class='split'><div class='card'><b>Firm memory</b><details><summary>Show notes JSON</summary>"
                   f"<pre>{e(json.dumps(mem, indent=1, default=str))}</pre></details></div>"
                   f"<div class='card'><b>Agent traces</b> (PRISM trajectories)<ul class='plain'>{traces}</ul>"
                   f"<details><summary>Raw steps</summary><pre>{e(json.dumps(state.get('traces', []), indent=1, default=str))}</pre></details></div></div>")


def section(name, title, sub, body):
    return f"<section class='glass section'><h2>{e(title)}</h2>{body}</section>"


@app.route("/")
def index():
    params = {k: request.args.get(k) for k in ("live", "closed", "signals", "tab", "brief")}
    params["live"] = 1 if (params.get("live") == "1" and ix.LLM_CLIENT.live) else 0
    tab = params.get("tab") if params.get("tab") in dict(TABS) else "today"
    state = get_state(bool(params["live"]), params.get("signals") == "1", params.get("closed") == "1")

    notes, members = state["notes"], state["members"]
    leads = [n for n in notes if n.get("status")]
    count = lambda s: sum(1 for n in leads if n["status"] == s)  # noqa: E731
    dropped = sum(1 for r in state["triage"] if r["triage"] == "drop")

    live_btn = ""
    if ix.LLM_CLIENT.live:
        live_btn = (f"<a class='btn' href='{link(params, live=0 if params['live'] else 1)}'>{icon('spark', 16)}"
                    f"Live AI: {'on' if params['live'] else 'off'}</a>")
    hero = (f"<header class='hero'><div><h1>Last Touch</h1><p>One agent per person. One shared firm memory.</p></div>"
            f"<div class='actions'>{live_btn}<a class='btn primary' href='{link({'live': params['live'], 'tab': tab})}'>"
            f"{icon('play', 15)}Run agents</a></div></header>")

    metrics = [("Inbound", len(state["triage"])), ("Dropped", dropped), ("Hot", count("hot")),
               ("Parked", count("park")), ("Warm", count("warm")), ("Pass", count("pass"))]
    m = "".join(f"<div class='metric'><div class='v'>{v}</div><div class='k'>{k}</div></div>" for k, v in metrics)

    views = {"today": lambda: tab_today(state, leads, members), "channel": lambda: tab_channel(state, members),
             "triage": lambda: tab_triage(state, leads, members), "briefs": lambda: tab_briefs(state, leads, members, params),
             "watch": lambda: tab_watch(state, leads, params), "memory": lambda: tab_memory(state, notes)}
    dock = "".join(f"<a class='{'on' if t == tab else ''}' href='{link(params, tab=t)}' aria-label='{label}'>"
                   f"<span class='tile'>{icon(t, 24)}</span><span class='lbl'>{label}</span></a>" for t, label in TABS)

    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Last Touch</title><style>{CSS}</style></head><body><main class='page'>{hero}"
            f"<div class='glass metrics'>{m}</div>{views[tab]()}</main><nav class='glass dock'>{dock}</nav></body></html>")


if __name__ == "__main__":
    app.run(debug=True, port=8000)
