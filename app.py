"""Last Touch: a firm-wide team of deal agents. Flask UI for Vercel (exports `app`).

Run locally: flask --app app run   (the original Streamlit UI is in streamlit_app.py)
Every page is a URL: ?run=1 runs the agents, &live=1 uses live AI, &closed=1 marks the fund closed,
&signals=1 checks Akta signals, &tab=... picks a view, &brief=<note id> picks a founder brief.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import copy  # noqa: E402
import threading  # noqa: E402
from html import escape  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

import markdown  # noqa: E402
from flask import Flask, request  # noqa: E402

import engine  # noqa: E402
import integrations as ix  # noqa: E402

app = Flask(__name__)

AVATAR = {"Ava": "🔎", "Ben": "📈", "Cara": "🚀", "Router": "🔀", "Watcher": "👀"}
TAG = {"hot": "HOT", "park": "PARKED", "warm": "WARM", "pass": "PASS"}
TABS = [("today", "✅ Today's actions"), ("channel", "💬 Firm channel"), ("triage", "📥 Inbox triage"),
        ("briefs", "📄 Briefs"), ("watch", "👀 Watchlist"), ("memory", "🧾 Memory & traces")]

# Runs are deterministic on cached data, so one result per (live, signals, closed) is kept per server instance.
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


def first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def tag(status: str) -> str:
    return f"<span class='tag {e(status)}'>{e(TAG.get(status, status.upper()))}</span>"


def link(params: dict, **changes) -> str:
    p = {**params, **changes}
    return "?" + urlencode({k: v for k, v in p.items() if v not in (None, "", 0, False)})


CSS = """
:root{--bg:#fafaf9;--panel:#fff;--side:#f1f0ee;--text:#1c1917;--muted:#78716c;--line:#e7e5e4;--accent:#dc2626;
--hot:#dc2626;--hotbg:#fee2e2;--park:#2563eb;--parkbg:#dbeafe;--warm:#b45309;--warmbg:#fef3c7;--pass:#64748b;--passbg:#e2e8f0;
--note:#fefce8;--notetext:#854d0e;--info:#eff6ff;--infotext:#1e40af;--code:#f5f5f4}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--panel:#262220;--side:#292524;--text:#f5f5f4;--muted:#a8a29e;--line:#3f3a36;
--hotbg:#450a0a;--hot:#fca5a5;--parkbg:#172554;--park:#93c5fd;--warmbg:#451a03;--warm:#fcd34d;--passbg:#334155;--pass:#cbd5e1;
--note:#422006;--notetext:#fde68a;--info:#172554;--infotext:#bfdbfe;--code:#1f1b19}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit}.wrap{display:flex;min-height:100vh}
aside{width:290px;flex:none;background:var(--side);padding:28px 22px;border-right:1px solid var(--line)}
aside h2{font-size:17px;margin:0 0 4px}.small{font-size:13px;color:var(--muted)}
.member{margin:14px 0}.dot-live{color:#16a34a}.dot-demo{color:#d97706}
main{flex:1;min-width:0;padding:32px 40px;max-width:1300px}
h1{font-size:40px;margin:0 0 8px}.lede{margin:0 0 20px;max-width:900px}
.btn{display:inline-block;padding:9px 18px;border-radius:8px;border:1px solid var(--line);background:var(--panel);text-decoration:none;font-weight:600;font-size:14px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.off{opacity:.45;pointer-events:none}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:22px}
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:8px 0 22px}
.metric .v{font-size:32px;font-weight:600}.metric .k{font-size:14px;color:var(--muted)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:20px;flex-wrap:wrap}
.tabs a{padding:8px 12px;text-decoration:none;font-size:14px;border-bottom:2px solid transparent}
.tabs a.on{border-color:var(--accent);color:var(--accent)}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:14px}
.card h3{margin:0 0 4px;font-size:22px}
.tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;font-weight:700;margin-right:6px}
.tag.hot{background:var(--hotbg);color:var(--hot)}.tag.park{background:var(--parkbg);color:var(--park)}
.tag.warm{background:var(--warmbg);color:var(--warm)}.tag.pass{background:var(--passbg);color:var(--pass)}
.act{margin:14px 0}.note{background:var(--note);color:var(--notetext);padding:12px 14px;border-radius:8px;margin:12px 0}
.info{background:var(--info);color:var(--infotext);padding:10px 14px;border-radius:8px;margin:10px 0}
pre{background:var(--code);padding:12px;border-radius:8px;white-space:pre-wrap;font-size:13px;margin:8px 0}
details summary{cursor:pointer;font-size:14px;color:var(--muted);margin-top:6px}
.msg{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}.msg .av{font-size:22px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.scroll{overflow-x:auto}
.picker a{display:inline-block;margin:0 6px 8px 0;padding:4px 10px;border:1px solid var(--line);border-radius:999px;text-decoration:none;font-size:13px}
.picker a.on{border-color:var(--accent);color:var(--accent)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:900px){.wrap{flex-direction:column}aside{width:auto;border-right:0;border-bottom:1px solid var(--line)}
main{padding:20px 16px}.metrics{grid-template-columns:repeat(3,1fr)}.cols,.split{grid-template-columns:1fr}h1{font-size:30px}}
"""


def sidebar(firm: dict, params: dict) -> str:
    fund = firm["fund"]
    out = [f"<h2>{e(fund['name'])}</h2><div class='small'>Thesis: {e(', '.join(fund['sectors']))} · {e(', '.join(fund['stages']))}</div>"]
    for m in firm["members"]:
        out.append(f"<div class='member'>{AVATAR.get(first(m['name']), '•')} <b>{e(m['name'])}</b>, {e(m['role'])}"
                   f"<div class='small'>{e(m['persona'])}</div></div>")
    out.append("<hr style='border:0;border-top:1px solid var(--line);margin:18px 0'>")
    if ix.LLM_CLIENT.live:
        on = bool(params.get("live"))
        out.append(f"<a class='btn' href='{link(params, live=0 if on else 1)}'>Live AI extraction (Nicrron): {'ON' if on else 'OFF'}</a>")
    else:
        out.append("<div class='small'>Live AI extraction (Nicrron) is off: set NICRRON_API_KEY to enable.</div>")
    out.append("<hr style='border:0;border-top:1px solid var(--line);margin:18px 0'><div class='small'>Sponsor integrations</div>")
    for name, (mode, err) in ix.status().items():
        dot = "dot-live" if mode == "live" else "dot-demo"
        out.append(f"<div><span class='{dot}'>●</span> {e(name)}: {e(mode)}</div>")
        if err:
            out.append(f"<div class='small'>⚠️ {e(err)}</div>")
    return "".join(out)


def tab_today(state, leads, members):
    cols = []
    for mid in ["ava", "ben", "cara"]:
        mem = members[mid]
        h = [f"<div class='card'><h3>{AVATAR.get(first(mem['name']), '')} {e(mem['name'])}</h3><div class='small'>{e(mem['persona'])}</div>"]
        for n in leads:
            if n.get("prior") and n["prior"]["who"] == mid:
                h.append(f"<div class='note'>You met <b>{e(n['founder'])}</b> ({e(n['company'])}) {e(n['prior']['days'])} days ago at "
                         f"{e(n['prior']['where'])}. No follow-up was sent. They just wrote to Ava.</div>")
        mine = [(n, a) for n in leads for a in n.get("actions", []) if a["who"] == mid and a["kind"] not in ("second_touch", "recheck")]
        if not mine:
            h.append("<p class='small'>Nothing due.</p>")
        for n, a in mine:
            label = n.get("company") or n.get("founder")
            when = a.get("due") or a.get("trigger", "")
            h.append(f"<div class='act'>{tag(n['status'])}<b>{e(label)}</b>: {e(a['what'])}<div class='small'>⏰ {e(when)}</div>")
            if a.get("draft"):
                h.append(f"<details><summary>Draft message (not sent)</summary><pre>{e(a['draft'])}</pre></details>")
            h.append("</div>")
        h.append("</div>")
        cols.append("".join(h))
    return f"<div class='cols'>{''.join(cols)}</div>"


def tab_channel(state):
    out = ["<p class='small'>What the agents told each other. Every note lives in the firm's shared memory (Roundtable notes).</p>"]
    for f in state["feed"]:
        who = f["agent"].split("'")[0].split()[0]
        out.append(f"<div class='msg'><div class='av'>{AVATAR.get(who, '🤖')}</div><div><b>{e(f['agent'])}</b> "
                   f"<span class='small'>· {e(f['t'])}</span><div>{e(f['text'])}</div></div></div>")
    return "".join(out)


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
                    f"<td>{e(oe)}</td><td>{e(n.get('score', ''))}</td><td>{e(n.get('reason') or r['reason'])}</td><td>{e(r['extraction'])}</td></tr>")
    head = "".join(f"<th>{h}</th>" for h in ["id", "channel", "from", "message", "agent decision", "status",
                                               "owner → evaluator", "score", "why", "extraction"])
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def tab_briefs(state, leads, members, params):
    choices = [n for n in leads if n.get("status") != "pass"] + [n for n in leads if n.get("status") == "pass"]
    if not choices:
        return "<p class='small'>No founders yet.</p>"
    pick = next((n for n in choices if n["id"] == params.get("brief")), choices[0])
    chips = "".join(f"<a class='{'on' if n is pick else ''}' href='{link(params, brief=n['id'])}'>"
                    f"{e(TAG.get(n['status']))} · {e(n.get('company') or n.get('founder'))}</a>" for n in choices)
    md = engine.render_brief(pick, members)
    body = markdown.markdown(escape(md, quote=False))
    return (f"<div class='picker'>{chips}</div><div class='card'>{body}</div>"
            "<p class='small'>Every claim is tagged with its source: [message id] = the founder's own message · "
            "[firm memory] = Roundtable note history · [Akta…] = outside data.</p>")


def tab_watch(state, leads, params):
    closed = state["fund"]["status"] == "closed"
    out = [f"<div class='bar'><a class='btn {'off' if closed else ''}' href='{link(params, closed=1, tab='watch')}'>"
           f"🎉 Mark {e(state['fund']['label'])} as closed</a>"
           f"<a class='btn' href='{link(params, signals=1, tab='watch')}'>📡 Check Akta signals on watched founders</a></div>"]
    watched = [n for n in leads if n["status"] == "park" or n.get("was_parked")]
    if not watched:
        out.append("<p class='small'>No parked founders.</p>")
    for n in watched:
        out.append(f"<div class='card'>{tag(n['status'])}<b>{e(n.get('company'))}</b>: {e(n.get('one_liner', ''))}"
                   f"<div class='small'>{e(n.get('reason', ''))}</div>")
        for s in n.get("signals", []):
            out.append(f"<div class='info'>Signal: {e(s['text'])} [{e(s['source'])}]</div>")
        for act in n.get("actions", []):
            if act.get("draft") and act["kind"] == "reengage":
                out.append(f"<pre>{e(act['draft'])}</pre>")
        out.append("</div>")
    return "".join(out)


def tab_memory(state, notes):
    import json
    mem = [{k: n.get(k) for k in ("id", "company", "founder", "status", "owner", "routed_to", "score", "touches")} for n in notes]
    traces = []
    for t in state.get("traces", []):
        pid = f" · PRISM id <code>{e(t['prism_id'])}</code>" if t.get("prism_id") else " · local only (set PRISMTRACE_* to send)"
        traces.append(f"<li><b>{e(t['agent'])}</b>: {len(t['steps'])} steps{pid}</li>")
    return (f"<div class='split'><div><b>Firm memory</b>: one note per founder, shared by every agent"
            f"<div class='small'>Saved to notes.json and mirrored as Markdown for Roundtable. Run id {e(state['run_id'])}.</div>"
            f"<details><summary>Show notes JSON</summary><pre>{e(json.dumps(mem, indent=1, default=str))}</pre></details></div>"
            f"<div><b>Agent traces</b> (PRISM trajectories)<ul>{''.join(traces)}</ul>"
            f"<details><summary>Raw steps</summary><pre>{e(json.dumps(state.get('traces', []), indent=1, default=str))}</pre></details></div></div>")


@app.route("/")
def index():
    firm = engine.load("firm.json")
    inbox = engine.load("inbox.json")
    params = {k: request.args.get(k) for k in ("run", "live", "closed", "signals", "tab", "brief")}
    params["live"] = 1 if (params.get("live") == "1" and ix.LLM_CLIENT.live) else 0
    tab = params.get("tab") or "today"

    state = get_state(bool(params["live"]), params.get("signals") == "1", params.get("closed") == "1") if params.get("run") else None
    status = (state or {}).get("fund", firm["fund"])["status"]
    head = (f"<h1>Last Touch</h1><p class='lede'><b>Every person at the firm gets an agent. The agents share one firm memory and "
            f"talk to each other</b>, so no great founder is dropped between the inbox, the event and the partner meeting.</p>"
            f"<div class='bar'><a class='btn primary' href='{link({'live': params['live']}, run=1)}'>▶ Run agents ({len(inbox)} new)</a>"
            f"<span class='small'>{e(firm['fund']['label'])} status: <b>{e(status)}</b> · Agents: Ava (associate) triages and briefs · "
            f"Ben and Cara (partners) score against their own thesis · Router assigns · Watcher remembers parked founders</span></div>")

    if not state:
        content = (f"<div class='info'>Press <b>Run agents</b> to watch the agents process {len(inbox)} messages: "
                   "LinkedIn DMs, emails and an event note.</div>")
    else:
        notes, members = state["notes"], state["members"]
        leads = [n for n in notes if n.get("status")]
        count = lambda s: sum(1 for n in leads if n["status"] == s)  # noqa: E731
        dropped = [r for r in state["triage"] if r["triage"] == "drop"]
        metrics = [("Inbound", len(state["triage"])), ("Dropped", len(dropped)), ("🔥 Hot", count("hot")),
                   ("🅿️ Parked", count("park")), ("🌤 Warm", count("warm")), ("Pass + referral", count("pass"))]
        m = "".join(f"<div class='metric'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in metrics)
        tabs = "".join(f"<a class='{'on' if t == tab else ''}' href='{link(params, tab=t)}'>{label}</a>" for t, label in TABS)
        views = {"today": lambda: tab_today(state, leads, members), "channel": lambda: tab_channel(state),
                 "triage": lambda: tab_triage(state, leads, members), "briefs": lambda: tab_briefs(state, leads, members, params),
                 "watch": lambda: tab_watch(state, leads, params), "memory": lambda: tab_memory(state, notes)}
        content = f"<div class='metrics'>{m}</div><nav class='tabs'>{tabs}</nav>{views.get(tab, views['today'])()}"

    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Last Touch</title><style>{CSS}</style></head><body><div class='wrap'><aside>{sidebar(firm, params)}</aside>"
            f"<main>{head}{content}</main></div></body></html>")


if __name__ == "__main__":
    app.run(debug=True, port=8000)
