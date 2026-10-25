import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import guard, integrations as ix
fails = []
keep = ["Pinecone and Pinterest", "pin down the date", "password reset flow is broken", "uses a password manager", "Call +1 415 555 0100 or +44 20 7946 0958",
        "raising 1,200,000 at 12000000 post", "the password is the same as before", "order 0123456789012345 shipped",
        "the password is weak.", "PIN is required.", "they ditch password 2FA flows",
        "Let's pin 10:30am Friday for the partner call.", "Pin v2.1.0 of the SDK", "Mastodon https://mastodon.social:443/@ana is her handle"]
gone = ["wifi password: hunter2!", "pin is 4455", "pw = x9!k", "card 4111 1111 1111 1111", "token ghp_abcdefghijklmnopqrstuvwxyz123456",
        "Bearer abcdefghijklmnopqrstuvwxyz", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        "password hunter22 for the dataroom", "postgres://admin:S3cret@db.example.com/app", "ASIAABCDEFGHIJKLMNOP",
        "Dataroom pw: Tr!cky for the dataroom, she said.", "rediss://:S3cretPass99@cache.beta.io:6380"]
for t in keep:
    if guard.redact(t)[0] != t: fails.append(f"over-redacted: {t!r} -> {guard.redact(t)[0]!r}")
for t in gone:
    if not guard.redact(t)[1]: fails.append(f"missed: {t!r}")
cases = [({"status": "completed", "dimensions": {"task_completion": {"passed": False, "status": "failed"}, "guardrails": {"passed": True, "violations": []}}}, "allow"),
         ({"dimensions": {"guardrails_policy": {"passed": False, "violations": [{"rule": "pii"}]}}}, "block"),
         ({"status": "failed", "error": "evaluator crashed"}, "unknown"), ({"status": "pending"}, "pending"),
         ({"decision": "block", "reason": "harassment"}, "block"), ({"evaluation": {"verdict": "fail", "score": 30}}, "unknown"),
         ({"issues": ["slow step"], "score": 55}, "unknown"), ({"safety": {"risk_level": "high"}}, "block"), ({"checks": {"pii": {"flagged": True}}}, "block"),
         ({"guardrails": {"pii": "triggered"}}, "block"), ({"safety": "unsafe"}, "block"), ({"toxicity": {"detected": True}}, "block"),
         ({"results": {"pharmacy_score": {"passed": False}}}, "unknown"), ({"metrics": {"harmony": {"status": "failed"}}}, "unknown"),
         ({"status": "completed", "guardrails": {"status": "pending"}}, "pending"), ({"reliability": 78, "guardrail_trigger_rate": 0.0}, "unknown"),
         ({"safety": {"unsafe": True}}, "block"), ({"guardrails": {"pii": False}}, "allow"), ({"checks": [{"name": "pii_leak", "status": "failed"}]}, "block"),
         ({"status": "completed", "metrics": {"reliability": {"status": "pending"}}, "guardrails": {"passed": True}}, "allow"),
         ({"evaluation": {"decision": "block"}}, "block"), ({"data": {"status": "running"}}, "pending"), ({"scores": {"safety": False}}, "block"),
         ({"checks": {"unsafe_tool_use": {"triggered": True}}}, "block"), ({"checks": {"instruction_compliance": {"passed": False}}}, "unknown"),
         ({"status": "completed", "guardrails": {"evaluation_status": "failed", "error": "timeout"}}, "unknown")]
for ev, want in cases:
    got = guard._decide(ev)[0]
    if got != want: fails.append(f"_decide {ev}: {got}, want {want}")
# overall time budget, even if PRISM hangs
class Hang:
    def submit_trajectory(self, *a, **k): time.sleep(30)
ix.TRACER.pt = Hang(); guard.TRACER.pt = ix.TRACER.pt
t = time.time(); r = guard.review("a", "a", "s", {"id": "1", "type": "save_note", "summary": "x", "payload": {"text": "hi"}}, budget=2)
if time.time() - t > 3 or r["decision"] != "unknown": fails.append(f"hang not bounded: {time.time()-t:.1f}s {r}")
class Pend:
    n = 0
    def submit_trajectory(self, *a, **k): return {"id": "t"}
    def get_trajectory_evaluation(self, t):
        Pend.n += 1
        return {"status": "completed", "guardrails": {"status": "pending"}} if Pend.n < 2 else {"status": "completed", "guardrails": {"passed": False, "violations": ["pii"]}}
guard.TRACER.pt = Pend()
r = guard.review("a", "a", "s", {"id": "1", "type": "save_note", "summary": "x", "payload": {"text": "hi"}}, budget=6)
if r["decision"] != "block": fails.append(f"stopped polling while guardrail pending: {r}")
class Steps:
    got = []
    def submit_trajectory(self, steps, **k): Steps.got = steps; return {"id": "t"}
    def get_trajectory_evaluation(self, t): return {"status": "completed"}
class Late:
    n = 0
    def submit_trajectory(self, *a, **k): return {"id": "t"}
    def get_trajectory_evaluation(self, t):
        Late.n += 1
        return {"trajectory_id": "t", "results": []} if Late.n < 2 else {"guardrails": {"passed": False}}
guard.TRACER.pt = Late()
r = guard.review("a", "a", "s", {"id": "1", "type": "save_note", "summary": "x", "payload": {"text": "hi"}}, budget=6)
if r["decision"] != "block": fails.append(f"stopped polling before PRISM answered: {r}")
guard.TRACER.pt = Steps()
guard.review("a", "a", "s", {"id": "1", "type": "save_note", "summary": "x", "payload": {"text": "Met Ana.", "original": "met ana " + "x " * 3000 + "END"}})
if not any(str(s.get("input_summary", "")).endswith("END") for s in Steps.got): fails.append("PRISM didn't see the whole original note")
print(f"{len(fails)} failures"); print("\n".join(fails))
