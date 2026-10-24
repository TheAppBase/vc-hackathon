"""Last Touch: a firm-wide team of deal agents. Run: streamlit run app.py"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import engine  # noqa: E402
import integrations as ix  # noqa: E402

st.set_page_config(page_title="Last Touch: firm-wide deal agents", page_icon="🤝", layout="wide")

st.markdown(
    """
<style>
.block-container {padding-top: 1.6rem; max-width: 1300px;}
.pill {display:inline-block; padding:3px 10px; margin:0 6px 6px 0; border-radius:999px; font-size:0.78rem;
       border:1px solid rgba(128,128,128,0.35);}
.dot-live {color:#16a34a;} .dot-demo {color:#d97706;}
.tag {display:inline-block; padding:1px 8px; border-radius:6px; font-size:0.75rem; font-weight:600; margin-right:6px;}
.hot {background:rgba(220,38,38,0.12); color:#dc2626;} .park {background:rgba(37,99,235,0.12); color:#2563eb;}
.warm {background:rgba(217,119,6,0.14); color:#d97706;} .pass {background:rgba(100,116,139,0.15); color:#64748b;}
.small {font-size:0.82rem; opacity:0.8;}
</style>
""",
    unsafe_allow_html=True,
)

AVATAR = {"Ava": "🔎", "Ben": "📈", "Cara": "🚀", "Router": "🔀", "Watcher": "👀"}
TAG = {"hot": "HOT", "park": "PARKED", "warm": "WARM", "pass": "PASS"}


def tag(status):
    return f"<span class='tag {status}'>{TAG.get(status, status.upper())}</span>"


firm = engine.load("firm.json")
fund = firm["fund"]

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.subheader(fund["name"])
    st.caption(f"Thesis: {', '.join(fund['sectors'])} · {', '.join(fund['stages'])}")
    for m in firm["members"]:
        st.markdown(f"{AVATAR.get(m['name'].split()[0], '•')} **{m['name']}**, {m['role']}  \n<span class='small'>{m['persona']}</span>",
                    unsafe_allow_html=True)
    st.divider()
    live_ai = st.toggle("Live AI extraction (Nicrron)", value=ix.LLM_CLIENT.live, disabled=not ix.LLM_CLIENT.live,
                        help="Off = cached demo extractions. Set NICRRON_API_KEY to enable.")
    if st.button("Clear cache (force fresh API calls)"):
        ix.clear_cache()
        st.toast("Cache cleared")
    st.divider()
    st.caption("Sponsor integrations")
    for name, (mode, err) in ix.status().items():
        dot = "dot-live" if mode == "live" else "dot-demo"
        st.markdown(f"<span class='{dot}'>●</span> {name}: {mode}", unsafe_allow_html=True)
        if err:
            st.caption(f"⚠️ {err}")

# ------------------------------------------------------------------ header
st.title("Last Touch")
st.markdown("**Every person at the firm gets an agent. The agents share one firm memory and talk to each other**, "
            "so no great founder is dropped between the inbox, the event and the partner meeting.")

inbox = engine.load("inbox.json")
c1, c2 = st.columns([1, 3])
with c1:
    run = st.button(f"▶ Run agents ({len(inbox)} new)", type="primary", use_container_width=True)
with c2:
    st.caption(f"{fund['label']} status: **{st.session_state.get('state', {}).get('fund', fund)['status']}** · "
               "Agents: Ava (associate) triages and briefs · Ben and Cara (partners) score against their own thesis · "
               "Router assigns · Watcher remembers parked founders")

if run:
    with st.spinner("Agents working: extracting, triaging, enriching with Akta, scoring, routing…"):
        st.session_state["state"] = engine.run_firm(live_ai=live_ai)

state = st.session_state.get("state")
if not state:
    st.info("Press **Run** to watch the agents process 14 messages: LinkedIn DMs, emails and an event note.")
    st.stop()

notes = state["notes"]
members = state["members"]
leads = [n for n in notes if n.get("status")]
count = lambda s: sum(1 for n in leads if n["status"] == s)  # noqa: E731
dropped = [r for r in state["triage"] if r["triage"] == "drop"]

m = st.columns(6)
m[0].metric("Inbound", len(state["triage"]))
m[1].metric("Dropped", len(dropped))
m[2].metric("🔥 Hot", count("hot"))
m[3].metric("🅿️ Parked", count("park"))
m[4].metric("🌤 Warm", count("warm"))
m[5].metric("Pass + referral", count("pass"))

tabs = st.tabs(["✅ Today's actions", "💬 Firm channel", "📥 Inbox triage", "📄 Briefs", "👀 Watchlist", "🧾 Memory & traces"])

# ------------------------------------------------------------------ today
with tabs[0]:
    cols = st.columns(3)
    for col, mid in zip(cols, ["ava", "ben", "cara"]):
        mem = members[mid]
        with col:
            with st.container(border=True):
                st.markdown(f"### {AVATAR.get(mem['name'].split()[0], '')} {mem['name']}")
                st.caption(mem["persona"])
                for n in leads:
                    if n.get("prior") and n["prior"]["who"] == mid:
                        st.warning(f"You met **{n['founder']}** ({n['company']}) {n['prior']['days']} days ago at "
                                   f"{n['prior']['where']}. No follow-up was sent. They just wrote to Ava.")
                mine = [(n, a) for n in leads for a in n.get("actions", []) if a["who"] == mid
                        and a["kind"] not in ("second_touch", "recheck")]
                if not mine:
                    st.caption("Nothing due.")
                for n, a in mine:
                    label = n.get("company") or n.get("founder")
                    when = a.get("due") or a.get("trigger", "")
                    st.markdown(f"{tag(n['status'])} **{label}**: {a['what']}  \n<span class='small'>⏰ {when}</span>",
                                unsafe_allow_html=True)
                    if a.get("draft"):
                        with st.expander("Draft message (not sent)"):
                            st.code(a["draft"], language=None, wrap_lines=True)

# ------------------------------------------------------------------ firm channel
with tabs[1]:
    st.caption("What the agents told each other. Every note lives in the firm's shared memory (Roundtable notes).")
    for f in state["feed"]:
        who = f["agent"].split("'")[0].split()[0]
        with st.chat_message(f["agent"], avatar=AVATAR.get(who, "🤖")):
            st.markdown(f"**{f['agent']}** · <span class='small'>{f['t']}</span><br>{f['text']}", unsafe_allow_html=True)

# ------------------------------------------------------------------ triage table
with tabs[2]:
    by_msg = {n.get("msg_id"): n for n in leads if n.get("msg_id")}
    rows = []
    for r in state["triage"]:
        n = by_msg.get(r["id"], {})
        rows.append({"id": r["id"], "channel": r["channel"], "from": r["from"], "message": r["preview"],
                     "agent decision": "Lead" if r["triage"] == "keep" else "Dropped",
                     "status": n.get("status", "-"), "owner → evaluator": (
                         f"{members[n['owner']]['name'].split()[0]} → {members[n['routed_to']]['name'].split()[0]}"
                         if n.get("routed_to") else (members[n['owner']]['name'].split()[0] if n else "-")),
                     "score": n.get("score"), "why": n.get("reason") or r["reason"], "extraction": r["extraction"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ briefs
with tabs[3]:
    choices = [n for n in leads if n.get("status") != "pass"] + [n for n in leads if n.get("status") == "pass"]
    pick = st.selectbox("Founder", choices, format_func=lambda n: f"{TAG.get(n['status'])} · {n.get('company') or n.get('founder')}")
    if pick:
        md = engine.render_brief(pick, members)
        with st.container(border=True):
            st.markdown(md)
        st.download_button("Download brief (.md)", md, file_name=f"{(pick.get('company') or pick.get('founder')).replace(' ', '_')}_brief.md")
        st.caption("Every claim is tagged with its source: [message id] = the founder's own message · [firm memory] = "
                   "Roundtable note history · [Akta…] = outside data.")

# ------------------------------------------------------------------ watchlist
with tabs[4]:
    a, b = st.columns(2)
    with a:
        if st.button(f"🎉 Mark {state['fund']['label']} as closed", disabled=state["fund"]["status"] == "closed"):
            st.session_state["state"] = engine.watcher_fund_closed(state)
            st.rerun()
    with b:
        if st.button("📡 Check Akta signals on watched founders"):
            st.session_state["state"] = engine.watcher_signals(state)
            st.rerun()
    watched = [n for n in leads if n["status"] == "park" or n.get("was_parked")]
    if not watched:
        st.caption("No parked founders.")
    for n in watched:
        with st.container(border=True):
            st.markdown(f"{tag(n['status'])} **{n.get('company')}**: {n.get('one_liner', '')}", unsafe_allow_html=True)
            st.caption(n.get("reason", ""))
            for s in n.get("signals", []):
                st.info(f"Signal: {s['text']}  [{s['source']}]")
            for act in n.get("actions", []):
                if act.get("draft") and act["kind"] == "reengage":
                    st.code(act["draft"], language=None, wrap_lines=True)

# ------------------------------------------------------------------ memory & traces
with tabs[5]:
    l, r = st.columns(2)
    with l:
        st.markdown("**Firm memory**: one note per founder, shared by every agent")
        st.caption(f"Saved to runtime/notes.json and mirrored as Markdown in runtime/roundtable_outbox/ for Roundtable. "
                   f"Run id {state['run_id']}.")
        st.json([{k: n.get(k) for k in ("id", "company", "founder", "status", "owner", "routed_to", "score", "touches")}
                 for n in notes], expanded=False)
    with r:
        st.markdown("**Agent traces** (PRISM trajectories)")
        for t in ix.TRACER.local:
            st.markdown(f"- **{t['agent']}**: {len(t['steps'])} steps"
                        + (f" · PRISM id `{t['prism_id']}`" if t.get("prism_id") else " · local only (set PRISMTRACE_* to send)"))
        with st.expander("Raw steps"):
            st.json(ix.TRACER.local, expanded=False)
