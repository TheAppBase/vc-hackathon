# Last Touch

Flask app for Vercel. Data comes from the event notes people record and from akta.pro; firm memory is a private
Vercel Blob store (optionally mirrored to Roundtable). Notes save without approval; secrets are redacted (`guard.py`).
Users in Auto mode are traced to PRISM and PRISM reviews each note; users in Ask mode send nothing to PRISM. See README.md.

## PRISM tracing (do not remove)

This project sends traces to PRISM. Env vars: `PRISMTRACE_API_KEY`,
`PRISMTRACE_PROJECT_ID`, `PRISMTRACE_HOST`.

Tracing is currently wired at: integrations.py (Tracer: trace_llm, tool trajectories, end_session, flush, setup doctor; Nicrron LLM calls), guard.py (PRISM review of every write), app.py (per-sign-in session ids, trace_tool on every agent skill incl. akta.pro lookups, flush on teardown, end_session on sign-out, /api/integrations status)

**Standing rule.** Whenever you add or change an agent, chain, graph, tool,
retriever, or any entry point that calls a model, wire it to PRISM before you
finish. Unwired code is invisible in the dashboard. If you are unsure whether
something is covered, assume it is not and wire it.
