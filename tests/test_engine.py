import os, sys, time, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import engine
T = dt.date(2026, 10, 24)  # Saturday
H = lambda n: engine.heuristic_note(n, T)
fails = []
def check(note, **want):
    x = H(note)
    for k, v in want.items():
        if x.get(k) != v:
            fails.append(f"{note!r}: {k}={x.get(k)!r}, want {v!r}")
# founders / companies
check("met jonah at demo day, next step: send him the term sheet by Monday", founder="Jonah", company=None, event="demo day", next_due="2026-10-26")
check("met priya at demo day, fintech", founder="Priya", company=None, sector="fintech")
check("met maya from quillstack, ai agents for finance", founder="Maya", company="Quillstack")
check("met lots of founders today", founder=None)
check("met tons of founders at the summit", founder=None)
check("met plenty of people at the dinner", founder=None)
check("met maya last week at the summit", founder=None)
check("Met up with Maya Okafor from Quillstack.", founder="Maya Okafor", company="Quillstack")
check("met up with maya from quillstack", founder="Maya", company="Quillstack")
check("Met Dr. Sean O'Brien from Kelpworks", founder="Sean O'Brien", company="Kelpworks")
check("Coffee with Jamie McDonald from Tally", founder="Jamie McDonald")
check("Met DeShawn Carter from Loop", founder="DeShawn Carter")
check("Met Monday with Maya Okafor from Quillstack", founder="Maya Okafor")
check("Met Tuesday at the summit with Raj Patel", founder="Raj Patel")
check("Met José Álvarez from Lumo", founder="José Álvarez")
check("Met Łukasz Nowak from Kraken", founder="Łukasz Nowak")
check("Met YC founder Maya Okafor from Quillstack", founder="Maya Okafor")
check("Met Stripe alum Maya Okafor", founder="Maya Okafor")
check("Met Ana de la Cruz from Loop", founder="Ana de la Cruz")
check("had lunch with maya okafor from quillstack", founder="Maya Okafor", company="Quillstack")
check("had lunch with the founder lena kim from solaris", founder="Lena Kim", company="Solaris")
check("met the team at the offsite", founder=None)
# sectors
check("Met Ana Silva from Loop. Rapidly growing consumer brand, raised capital last year.", sector="consumer")
check("Met Omar Haddad from Dinely. Super high energy founder, sells to restaurants.", sector="consumer")
check("Met Lena Park from Snackbox, a snack brand building a better API for grocery stores and retailers", sector="cpg")
check("Met Kim from Ledger, payments infrastructure for banks and banking", sector="fintech")
check("Met Kim Park from Arbor, a former director in the healthcare sector.", background=[])
check("Met Ravi from Volt. Engineers from Tesla, researchers from MIT.", background=["engineer", "research"])
# next steps
check("Met Leo Marchetti from Tracewell. Raising $1.2M. Next step: email leo@tracewell.dev by Friday.", next_step="email leo@tracewell.dev by Friday", raising_usd=1200000.0)
check("Met Raj via Dr. Smith. Intro Raj to Cara by Friday.", next_step="Intro Raj to Cara by Friday")
check("Met Priya Raman from Halftone. She will send the deck tomorrow. I'll follow up Friday.", next_step="I'll follow up Friday", next_due="2026-10-30")
check("Met Lena Park. She has a lead, so send her the term sheet by Friday.", next_step="She has a lead, so send her the term sheet by Friday", next_due="2026-10-30")
check("Met Maya Okafor. Maya is going to send the data room.", next_step=None)
check("met with sam lee today he is building batteries for trucks I need to follow up friday with the deck", next_due="2026-10-30")
check("met sam today send him the deck friday", next_step="send him the deck friday", next_due="2026-10-30")
check("Sent Priya Raman from Halftone the intro to Ben, done.", next_step=None, step_done=True)
check("Maya Okafor from Quillstack already has $25k MRR.", step_done=False)
check("Maya Okafor from Quillstack booked $25k MRR this month.", step_done=False)
check("Maya Okafor from Quillstack completed their SOC2 audit.", step_done=False)
check("I introduced Maya to Cara yesterday.", step_done=True)
check("Met Ravi Rao from Volt building next-gen batteries", next_step=None)
# money
check("Met Ana from Loop, raising $2-3M seed", raising_usd=2000000.0)
check("Met Ana from Loop, raising 1.5 million", raising_usd=1500000.0)
check("Met Ana from Loop raising $800k pre-seed", raising_usd=800000.0, stage="pre-seed")
# events
check("Met Ben Ode from Kite, fairly early, will reconnect at some point next week", event=None)
check("Met Lia Wu at the Deal Flow hackathon", event="Deal Flow hackathon")
check("At the Deal Flow hackathon I met Maya Okafor.", event="Deal Flow hackathon")
check("met maya at the deal flow hackathon", event="deal flow hackathon")
check("Met Ana at Climate Week", event="Climate Week")
check("Met Ana at the career fair", event="career fair")
check("Raising at a fair valuation", event=None)
check("Let's catch up at dinner sometime.", event=None)
check("Looking at an event-driven architecture.", event=None)
check("Meet again at lunch next week.", event=None)
# router
R = engine.route_rules
for q, intent, query in [("Best founders I met at YC demo day: Patchbay", "save_note", ""), ("Jonah from Patchbay said his team working on payments", "save_note", ""),
                         ("Did I meet Jonah Weiss from Patchbay?", "help", ""), ("did i meet jonah", "help", ""), ("what did maya say about pricing", "help", ""),
                         ("Find climate startups", "source", "climate"), ("Find me AI startups", "source", "AI"), ("source fintech founders", "source", "fintech"),
                         ("find new startups in robotics", "source", "robotics"), ("search for Quillstack", "lookup", "Quillstack"), ("Who is overdue?", "conversations", ""),
                         ("source founders in climate tech", "source", "climate tech"), ("Met Jonah Weiss from Patchbay, find time next week", "save_note", "")]:
    r = R(q)
    if (r["intent"], r["query"]) != (intent, query):
        fails.append(f"route {q!r}: {r}, want {intent}/{query}")
# clean_note
c = engine.clean_note({"founder": ["Maya Okafor", "Raj Patel"], "company": "Not specified", "event": "N/A.", "raising_usd": float("nan"),
                       "metrics": {"mrr_usd": True, "users": float("inf")}, "background": ["Unknown.", "ex-Stripe"]})
if c["founder"] != "Maya Okafor" or c["company"] or c["event"] or c["raising_usd"] or c["metrics"] or "ex-Stripe" not in c["background"] or not any("Raj" in b for b in c["background"]):
    fails.append(f"clean_note: {c}")
# speed (#46)
t = time.time(); H("1" * 3000 + " stars " + "2,3" * 2000); el = time.time() - t
if el > 0.5:
    fails.append(f"slow regex: {el:.2f}s")
print(f"{len(fails)} failures") ; print("\n".join(fails))
