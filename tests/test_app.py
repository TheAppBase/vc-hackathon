import os, sys, json, tempfile, threading, time
os.environ["LAST_TOUCH_RUNTIME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import app, store, guard, integrations as ix
app.ATTEMPT_SECONDS = 2
fails = []
def ok(cond, msg):
    if not cond: fails.append(msg)
def signup(c, u, name=None):
    c.post('/api/signup', json={'name': name or u.title(), 'username': u, 'password': 'correct horse'})
    c.post('/api/profile', json={'role': 'Associate', 'focus': 'AI and devtools, pre-seed to seed', 'signal': 'Technical founders', 'nos': 'Token or crypto raises'})
c = app.app.test_client(); signup(c, 'alex', 'Alex Frankel')
# --- F1 / F2 / F10 / F9 with a stand-in AI that records inputs
seen = []
class AI:
    live = True; error = None; model = 'm'; fast_model = 'f'
    def __init__(s, router=None, note=None): s.router, s.note = router, note
    def json(s, system, user, agent, **k):
        seen.append(user)
        if 'Decide what their message' in system: return s.router
        return s.note(user) if callable(s.note) else s.note
ix.LLM_CLIENT = AI(router={'intent': 'save_note', 'query': '', 'reply': ''})
c.post('/api/route', json={'text': 'Met Ana, wifi password: hunter2! key ghp_abcdefghijklmnopqrstuvwxyz123456'})
ok('hunter2' not in seen[-1] and 'ghp_' not in seen[-1], f"F1 router saw secrets: {seen[-1]}")
for bad in [{'intent': ['source']}, {'intent': 'source', 'query': ['x'], 'reply': {'a': 1}}, 'nonsense', None]:
    ix.LLM_CLIENT = AI(router=bad)
    r = c.post('/api/route', json={'text': 'source founders in climate'})
    ok(r.status_code == 200, f"F9 router crashed on {bad!r}: {r.status_code}")
long_note = "Met Maya Okafor from Quillstack. " + "She described their roadmap in detail. " * 150 + "Final fact: raising $3M seed, next step: intro to Cara by Monday."
ix.LLM_CLIENT = AI(note=lambda u: {'note': 'Met Maya Okafor from Quillstack.', 'founder': 'Maya Okafor', 'company': 'Quillstack', 'next_step': 'call sk-abcdefghijklmnopqrstuvwxyz pw: x9!kk'})
r = c.post('/api/notes', json={'text': long_note}).json
ok('Final fact' in seen[-1], "F2 AI didn't see the end of a long note")
ok('Final fact' in r['payload']['text'], "F2 a much-shorter rewrite replaced a long note")
ok('sk-abc' not in json.dumps(r['payload']['extracted']) and 'x9!kk' not in json.dumps(r['payload']['extracted']), f"F10 secret in extracted fields: {r['payload']['extracted']}")
ix.LLM_CLIENT = type('Off', (), {'live': False, 'error': None, 'model': 'm', 'fast_model': 'f'})()
# --- #10 same person at the same company; co-founders stay separate
for t in ['Met Maya Okafor from Quillstack. Next step: send the deck by Friday.', 'Met Maya from Quillstack again, $25k MRR now.', 'Met Tom Lee from Quillstack, the CTO.']:
    c.post('/api/notes', json={'text': t})
fs = {f['founder']: f for f in c.get('/api/founders').json['founders']}
ok('Maya Okafor' in fs and 'Maya' not in fs and fs['Maya Okafor']['next_step'] == 'send the deck by Friday', f"#10 merge: {list(fs)}")
ok('Tom Lee' in fs, f"#10 co-founder merged: {list(fs)}")
# --- #11 time zone: a step due 'tomorrow' saved late evening Pacific
r = c.post('/api/notes', json={'text': 'Met Lia Wu from Kite. Next step: email her tomorrow.'}, headers={'X-TZ-Offset': '420'}).json
import datetime as dt
pac = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=420)).date()
ok(r['payload']['extracted']['next_due'] == (pac + dt.timedelta(days=1)).isoformat(), f"#11 due date not in user's zone: {r['payload']['extracted']['next_due']}")
# --- #12 the index: a teammate's notes are read from one blob
b = app.app.test_client(); signup(b, 'bob', 'Bob Jones')
b.post('/api/team', json={'username': 'alex'}); c.post('/api/team', json={'username': 'bob'})
for i in range(30): b.post('/api/notes', json={'text': f'Met Founder{i} Person from Co{i}.'})
calls = {'get': 0}
orig_get = store.get
def counting_get(p):
    calls['get'] += 1; return orig_get(p)
store.get = counting_get
c.get('/api/team/activity')
store.get = orig_get
ok(calls['get'] < 15, f"#12 team view made {calls['get']} reads")
os.remove(os.path.join(store.LOCAL, 'index', 'bob.json'))
ok(len(c.get('/api/team/activity').json['team'][0]['founders']) == 4, "#12 index not repaired after loss")
ok(os.path.exists(os.path.join(store.LOCAL, 'index', 'bob.json')), "#12 index not rewritten")
# --- #13 a stranger can't lock the owner's device out; password change isn't blocked; stale writes keep new passwords
stranger = {'X-Real-IP': '9.9.9.9'}; owner = {'X-Real-IP': '1.1.1.1'}
th = threading.Thread(target=lambda: app.app.test_client().post('/api/signin', json={'username': 'alex', 'password': 'nope nope'}, headers=stranger)); th.start(); time.sleep(0.3)
r = app.app.test_client().post('/api/signin', json={'username': 'alex', 'password': 'correct horse'}, headers=owner)
ok(r.status_code == 200, f"#13 owner locked out by stranger: {r.status_code} {r.json}")
th.join()
th = threading.Thread(target=lambda: app.app.test_client().post('/api/signin', json={'username': 'alex', 'password': 'nope nope'}, headers=stranger)); th.start(); time.sleep(0.3)
r = c.post('/api/password', json={'current': 'correct horse', 'new': 'brand new pass'})
ok(r.status_code == 200, f"#13 password change blocked by stranger: {r.status_code} {r.json}")
th.join()
u = app.load_user('alex'); u['pw_ver'] -= 1; u['password'] = 'stale'; app.save_user(u)
ok(app.load_user('alex')['password'] != 'stale', "#13 stale write undid a password change")
# 40 parallel tries from one device still get one check
res = []
def go(): res.append(app.app.test_client().post('/api/signin', json={'username': 'bob', 'password': 'x' * 9}, headers=stranger).status_code)
ts = [threading.Thread(target=go) for _ in range(20)]; [t.start() for t in ts]; [t.join() for t in ts]
ok(res.count(403) == 1, f"#13 parallel tries: {sorted(res)}")
# --- #45 caps are atomic and use the server date
app.AKTA_PAID_PER_DAY = 3
ok(sum(app.paid_ok(app.load_user('alex')) for _ in range(5)) == 3, "#45 per-person cap")
with app.app.test_request_context(headers={'X-Local-Date': '2020-01-01'}):
    ok(not app.paid_ok(app.load_user('alex')), "#45 cap reset by a fake client date")
# --- #34 warm intro only on sure matches
b.post('/api/notes', json={'text': 'Lunch with Sam, my old roommate.'})
c.post('/api/notes', json={'text': 'met sam at the mixer, building a payments api'})
f = [f for f in c.get('/api/founders').json['founders'] if f['founder'] == 'Sam'][0]
ok(f['known'] and not f['known'][0]['sure'] and 'warm intro' not in f['why'], f"#34 unsure match counted: {f['why']} {f['known']}")
# --- #33 one PRISM session per tab
with app.app.test_request_context(headers={'X-Chat-Id': 'tab-aaaaaaaa'}):
    s1 = app.chat_sid({'username': 'alex'})
with app.app.test_request_context(headers={'X-Chat-Id': 'tab-bbbbbbbb'}):
    s2 = app.chat_sid({'username': 'alex'})
ok(s1 != s2, "#33 tabs share a PRISM session")
# --- round 3: due dates, password blob, first-name merges, paid caps, device cookie
with app.app.test_request_context(headers={'X-TZ-Offset': '420'}):
    ok(app.nice_date('2026-10-30') == 'Fri Oct 30', f"date-only due date shifted by the time zone: {app.nice_date('2026-10-30')}")
raw = json.load(open(os.path.join(store.LOCAL, 'users', 'alex.json')))
ok('password' not in raw and os.path.exists(os.path.join(store.LOCAL, 'auth', 'alex.json')), "password kept in the profile blob")
d = app.app.test_client(); signup(d, 'dana')
for t in ['Met Maya Okafor from Quillstack.', 'Met Maya from Kite, building robots.']:
    d.post('/api/notes', json={'text': t})
fs = [f"{f['founder']}/{f['company']}" for f in d.get('/api/founders').json['founders']]
ok(len(fs) == 2, f"a first name at another company was merged: {fs}")
day = dt.datetime.now(dt.timezone.utc).date().isoformat()
ok(len(store.list_paths(f"usage/_firm/{day}/")) == 3, "someone over their own cap used up the firm's paid calls")
ok(app.extra_focus('Robotics and edtech, seed') == ['robotics', 'edtech'] and app.build_profile({'focus': 'Robotics and edtech, seed'}, 'A B')['sectors'] == [],
   "focus outside the known sectors")
r = app.app.test_client().post('/api/signin', json={'username': 'ghost', 'password': 'whatever1'})
ok(r.status_code == 403 and not os.path.exists(os.path.join(store.LOCAL, 'attempts', 'ghost')), "made-up username left attempt blobs")
ts = [threading.Thread(target=lambda i=i: app.app.test_client().post('/api/signin', json={'username': 'alex', 'password': 'nope nope'},
                                                                       headers={'X-Real-IP': f'10.0.0.{i}'})) for i in range(8)]
[t.start() for t in ts]; time.sleep(0.5)
r = app.app.test_client().post('/api/signin', json={'username': 'alex', 'password': 'brand new pass'}, headers={'X-Real-IP': '10.0.1.1'})
ok(r.status_code == 429 and 'this account' in r.json['error'], f"account cap message: {r.status_code} {r.json}")
r = c.post('/api/signin', json={'username': 'alex', 'password': 'brand new pass'}, headers=owner)
ok(r.status_code == 200, f"owner's known device was locked out by strangers: {r.status_code} {r.json}")
[t.join() for t in ts]
# --- F8 session key: a storage error never breaks pages
store.LIVE = True; app.app.secret_key = None
orig = store.shared_secret; store.shared_secret = lambda: (_ for _ in ()).throw(RuntimeError('blob down'))
r = app.app.test_client().get('/api/health'); ok(r.status_code == 200, f"F8 health broke: {r.status_code}")
store.shared_secret = orig; store.LIVE = False; app.app.secret_key = None
print(f"{len(fails)} failures"); print("\n".join(fails))
