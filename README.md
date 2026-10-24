# Last Touch (Deal Flow hackathon, Track 1: sourcing)

**Vision:** every person at a VC firm gets a personal AI agent. The agent interviews them to learn how they invest, then works the way they do. The agents share one firm memory and talk to each other, so the firm always knows who is talking to which founder and what happens next.

**One data source: the event notes people record.** No inbox or calendar integrations and no mock data. After an event or a meeting, you tap the mic and say who you met. The agent does the rest.

1. **Sign in.** Enter your name and a username. The username box checks availability as you type.
2. **Build your agent.** A three-question interview (role, sectors and stages, the signal you trust). Your answers become your agent's scoring weights.
3. **Record a note.** The mic fills the chat box live and has no time limit. It works during the interview too, and you can also type. For example: "Met Jonah Weiss from Patchbay at the Deal Flow hackathon. Ex-Stripe infra, webhook API, 1,800 GitHub stars, raising $1.2M pre-seed. Next step: intro to Cara by Monday." The agent reads out the founder, company, event, traction and next step. Nicrron does this when configured, keyword rules otherwise.
4. **Who have I been talking to?** Every founder in your notes, where you met, the latest next step, and what's due or overdue.
5. **Rank my founders.** Scores founders with your interview weights, using only what your notes say. "Draft follow-up" writes a message for you to copy; the agent never sends anything.
6. **Team.** Add people by username (it checks the username exists). Once both of you have added each other, your agents share notes. When you record a note about someone a teammate already met, their agent tells you. You can also ask "who knows Patchbay?" or "what is my team working on?".

## Safety: every write is guarded
Reading is always allowed. The agent's only write is saving a note to the firm memory. Each user picks a mode in Settings:
- **Ask for permission** (default): every note shows a Save / Don't save card with what the agent read from it. Nothing is sent to PRISM.
- **Auto, guarded by PRISM**: PRISM watches the agent. Model calls, agent tool calls and errors are traced to PRISM under one session per sign-in, and each proposed note is reviewed by PRISM. A clear pass saves it and a block stops it. If PRISM is unreachable or unclear, the agent asks you instead (fail closed).

In both modes the agent never saves secrets (API keys, private keys, passwords, card numbers). Every action is logged and listed under Settings → Recent agent actions. Settings also shows PRISM's credential and live-trace status from the PRISM setup doctor.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env     # fill in what you have
flask --app app run      # http://127.0.0.1:5000
```
Without `BLOB_READ_WRITE_TOKEN`, data is stored in `runtime/store/` (local development only).

## Deploy (Vercel)
1. Import the repo. `app.py` exports the Flask `app`, so no framework preset is needed.
2. Storage → create a **private** Blob store and connect it to the project (this adds `BLOB_READ_WRITE_TOKEN`).
3. Set `SECRET_KEY` and `PRISMTRACE_API_KEY` (`PRISM_API_KEY` also works), and optionally `NICRRON_API_KEY`. Redeploy.

## Files
- `app.py`: Flask server: accounts, note reading, founders, team, the write gate, PRISM tracing hooks
- `page.html`: the single-page chat UI (sign-in, interview, recording, approval cards, settings)
- `guard.py`: hard rules plus the PRISM review used in Auto mode
- `engine.py`: note extraction rules, follow-up dates and scoring
- `integrations.py`: PRISM (tracing, sessions, setup doctor) and Nicrron
- `store.py`: firm memory on Vercel Blob (`users/`, `notes/`, `actions/`)
