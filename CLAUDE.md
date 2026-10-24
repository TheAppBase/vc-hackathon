# Last Touch

Flask app for Vercel. Recorded event notes are the only data source; firm memory is a private Vercel Blob store.
Every write the agent makes (saving a note) goes through `guard.py`: Ask mode asks the user, Auto mode lets PRISM decide.
Users in Ask mode send nothing to PRISM. See README.md.

## PRISM tracing (do not remove)

This project sends traces to PRISM. Env vars: `PRISMTRACE_API_KEY`,
`PRISMTRACE_PROJECT_ID`, `PRISMTRACE_HOST`.

Tracing is currently wired at: integrations.py (Tracer: trace_llm, tool trajectories, end_session, flush, setup doctor; Nicrron LLM calls), guard.py (PRISM review of every write), app.py (per-sign-in session ids, trace_tool on every agent skill, flush on teardown, end_session on sign-out, /api/prism status)

**Standing rule.** Whenever you add or change an agent, chain, graph, tool,
retriever, or any entry point that calls a model, wire it to PRISM before you
finish. Unwired code is invisible in the dashboard. If you are unsure whether
something is covered, assume it is not and wire it.
