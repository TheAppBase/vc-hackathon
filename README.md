# Last Touch (Deal Flow hackathon, Track 1: sourcing)

**Vision:** every person at a VC firm gets a personal AI agent. The agent interviews them to learn their domain knowledge, then works the way they do. All the agents share one firm memory and talk to each other, so the firm always knows who is talking to which founder and what happens next.

**Today's MVP (mock data):** one full-screen chat with your personal agent.
1. **Build your agent.** A quick interview: name, role, sectors and stages, the signal you trust, where you talk to founders, hard no's. Your answers become your agent's scoring weights.
2. **Source founders.** The agent scans the inbox (14 mock messages), drops spam and vendors, checks each founder on akta.pro (the agents' mini web search), and ranks them with your own criteria. Scoring is the explainable rules engine in `engine.py`.
3. **Who have I been talking to?** Founder conversations across Gmail, LinkedIn, Google Calendar, WhatsApp and event notes, with next steps and what's overdue.
4. **Voice input and notes.** Tap the mic to talk instead of typing, anywhere in the chat (the interview too), with no time limit: the browser's speech recognition (Chrome, Edge or Safari) fills the message live. Say "Just met …" (or tap *Record a note*) and the agent extracts founder, company and next step (Nicrron when `NICRRON_API_KEY` is set, keyword rules otherwise), checks who at the firm already knows them, and saves the note to the firm memory (Roundtable, when `ROUNDTABLE_MCP_URL` and `ROUNDTABLE_MCP_TOKEN` are set).
5. **Agents talk to each other.** Before you reach out, your agent asks teammates' agents (visible tool calls) who already knows the founder, e.g. "Ben met Maya 46 days ago and never followed up: Ben re-intros, you take the call."

Settings (top-right corner) holds your agent profile, connected platforms, the team's agents and "add a person by username".

## Run
```bash
pip install -r requirements.txt
flask --app app run      # http://127.0.0.1:5000
```
No keys needed: everything runs on mock data in `data/`. Optional keys in `.env.example` (Nicrron, Akta, PRISM, Roundtable).

## Deploy
Vercel: import the repo; `app.py` exports the Flask `app`, no framework preset needed.

## Files
- `app.py`: the MVP (Flask server, `/api/build` scores founders with the interview's weights, single-page chat UI)
- `engine.py`: triage, scoring, routing and follow-up rules
- `notes.py`: reads a dictated or typed note (founder, company, event, next step and due date)
- `integrations.py`: Nicrron, Akta, PRISM, Roundtable clients with demo fallbacks
- `data/`: mock firm, inbox, firm memory, Akta fixtures, interview script and conversations (`mvp.json`)
- `streamlit_app.py`: the earlier multi-view Streamlit prototype

All people, funds and startups in `data/` are fictional. Akta fixture comparables list real companies by name only.
